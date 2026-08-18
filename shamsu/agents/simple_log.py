"""Human-readable transcript of what SHAMSU actually sent and got back.

Simple mode was built without this and it was a real loss: when a turn goes
wrong the only questions that matter are "what did the model actually see?" and
"what did it actually say?", and neither was answerable. Structured event logs
answer neither - they record that a call happened, not its content.

So this writes plain markdown, one file per SESSION, with the FULL prompt and
the RAW response verbatim. Nothing is truncated: a record that clips is not a
record, and this is the file you open to find out why a turn went wrong.

Layout, under the workspace:

    .shamsu/chat-logs/
        20260818-142201-a1b2--asteroids-game.md   <- one thread, every turn
        latest.md                                 <- names the current thread

Headings are ASCII: Windows consoles are cp1252 and choke on arrows and dashes.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_DIR = ".shamsu/chat-logs"

# Nothing here is trimmed. This is a record of what happened, and a record that
# clips is not one - a 4000-char cap silently cut every file read out of the
# very log you would open to find out why a turn went wrong. Text is cheap: a
# whole project's sessions measured 684 KB on 2026-08-18.
_TOOL_RESULT_LIMIT = 0  # 0 = no limit


def _fence(text: str, lang: str = "") -> str:
    """Wrap in a fence that survives text containing backticks."""
    body = text if text.endswith("\n") else text + "\n"
    ticks = "```"
    while ticks in body:
        ticks += "`"
    return ticks + lang + "\n" + body + ticks + "\n"


def _get(obj: Any, key: str) -> Any:
    """Read *key* from a dict OR a pydantic model, whichever the client returned.

    The Ollama client returns a ``ChatResponse`` object, not a dict. Reading it
    with ``.get`` only logged every response as "(empty)" while the turn plainly
    produced text - and a log that lies is worse than no log at all.
    """
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _plain(value: Any) -> Any:
    """Best-effort plain-data view of pydantic objects, for JSON dumping."""
    for attr in ("model_dump", "dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return method()
            except Exception:
                pass
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _slug(text: str, limit: int = 40) -> str:
    kept = [c.lower() if c.isalnum() else "-" for c in (text or "").strip()]
    slug = re.sub(r"-+", "-", "".join(kept)).strip("-")
    return slug[:limit] or "session"


class SimpleTurnLog:
    """One markdown file per SESSION; every turn appends to it.

    It used to be one file per TURN, named from a count of the files already in
    the directory - so the numbering was workspace-global, carried no session
    id, and two threads interleaved into one folder as `turn-001 ... turn-011`
    with no way to tell which conversation each belonged to. `latest.md` was
    whichever turn ran last in ANY session.

    A chat log should be the thread, so it is now the thread.
    """

    def __init__(
        self,
        workspace: Path,
        turn_number: int,
        model: str,
        session_id: str = "",
        session_title: str = "",
    ) -> None:
        self.dir = Path(workspace) / LOG_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{self.session_id}--{_slug(session_title)}" if session_title else self.session_id
        self.path = self.dir / f"{name}.md"
        self.model = model
        self.turn_number = turn_number
        self._started = time.perf_counter()
        self._round = 0
        self._new_file = not self.path.exists()

    def _append(self, text: str) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass  # logging must never break a turn

    def open_turn(self, user_message: str) -> None:
        when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self._new_file:
            self._append(
                f"# Session {self.session_id}\n\n"
                f"**model** `{self.model}` - started {when}\n\n"
                "Every turn of this thread, in order: the exact prompt sent to the\n"
                "model, its raw reply, and each tool call with its real result.\n"
            )
            self._new_file = False
        self._append(
            f"\n\n---\n\n# Turn {self.turn_number} - {when}\n\n"
            f"## What you asked\n\n{_fence(user_message)}\n"
        )

    def log_call(self, messages: list[dict[str, Any]], num_ctx: int, tokens: int) -> None:
        """The exact prompt handed to the model, message by message."""
        self._round += 1
        self._append(
            f"\n---\n\n## Round {self._round} - prompt sent to the model\n\n"
            f"`num_ctx {num_ctx:,}` - `{len(messages)} messages` - `~{tokens:,} tokens`\n\n"
        )
        for index, message in enumerate(messages, start=1):
            role = str(_get(message, "role") or "?")
            content = _get(message, "content") or ""
            calls = _get(message, "tool_calls") or []
            self._append(f"### [{index}] {role}\n\n")
            if content:
                self._append(_fence(str(content)))
            if calls:
                self._append(
                    "*assistant asked for tools:*\n"
                    + _fence(json.dumps(_plain(calls), indent=2, default=str), "json")
                )
            if not content and not calls:
                self._append("*(empty)*\n")
            self._append("\n")

    def log_response(self, raw: Any, seconds: float) -> None:
        """The model's reply, verbatim - including the thinking channel."""
        self._append(f"\n## Round {self._round} - raw response  ({seconds:.1f}s)\n\n")
        message = _get(raw, "message") or raw
        content = str(_get(message, "content") or "")
        thinking = str(_get(message, "thinking") or _get(message, "reasoning") or "")
        calls = _get(message, "tool_calls") or []

        if thinking:
            self._append("**thinking channel**\n\n" + _fence(thinking))
        self._append("**content**\n\n" + (_fence(content) if content else "*(empty)*\n"))
        if calls:
            self._append(
                "\n**tool calls requested**\n\n"
                + _fence(json.dumps(_plain(calls), indent=2, default=str), "json")
            )
        self._append("\n")

    def log_error(self, error: str) -> None:
        self._append(f"\n## Round {self._round} - ERROR\n\n{_fence(error)}\n")

    def log_tool_result(self, name: str, arguments: Any, ok: bool, result: str) -> None:
        mark = "ok" if ok else "FAILED"
        body = result or ""
        if _TOOL_RESULT_LIMIT and len(body) > _TOOL_RESULT_LIMIT:
            body = body[:_TOOL_RESULT_LIMIT] + f"\n... [{len(result) - _TOOL_RESULT_LIMIT} more chars]"
        self._append(
            f"\n### tool `{name}` -> {mark}\n\n"
            "*arguments*\n\n"
            + _fence(json.dumps(_plain(arguments), indent=2, default=str), "json")
            + "\n*result*\n\n"
            + _fence(body)
        )

    def close_turn(self, final: str, rounds: int, stopped: bool) -> None:
        elapsed = time.perf_counter() - self._started
        note = ", STOPPED early" if stopped else ""
        self._append(
            f"\n---\n\n## Final answer\n\n{_fence(final)}\n"
            f"*{rounds} round(s), {elapsed:.1f}s{note}*\n"
        )
        # A POINTER, not a copy. The session log is lossless and can reach
        # megabytes; duplicating it on every turn would double the writes and
        # the disk for nothing.
        try:
            (self.dir / "latest.md").write_text(
                "# Latest thread\n\nThe active conversation is in "
                f"[`{self.path.name}`](./{self.path.name}).\n",
                encoding="utf-8",
            )
        except OSError:
            pass


def next_turn_number(workspace: Path, session_id: str = "") -> int:
    """Which turn of THIS session is about to run.

    Counting files in the directory made the number workspace-global: two
    threads shared one counter and neither's numbering meant anything. Counting
    the turn headings inside this session's own file makes it the thread's turn
    number, and it survives a restart because the file does.
    """
    directory = Path(workspace) / LOG_DIR
    if not directory.is_dir() or not session_id:
        return 1
    for path in directory.glob(f"{session_id}*.md"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 1
        return sum(1 for line in text.splitlines() if line.startswith("# Turn ")) + 1
    return 1
