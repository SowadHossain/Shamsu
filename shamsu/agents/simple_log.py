"""Human-readable transcript of what SHAMSU actually sent and got back.

Simple mode was built without this and it was a real loss: when a turn goes
wrong the only questions that matter are "what did the model actually see?" and
"what did it actually say?", and neither was answerable. Structured event logs
answer neither - they record that a call happened, not its content.

So this writes plain markdown, one file per user turn, with the FULL prompt and
the RAW response verbatim - no truncation of the model's own words, because the
whole point is to read what it really produced.

Layout, under the workspace:

    .shamsu/chat-logs/
        2026-08-18-142201-turn-003.md
        latest.md            <- copy of the newest turn, for `cat latest.md`

Headings are ASCII: Windows consoles are cp1252 and choke on arrows and dashes.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_DIR = ".shamsu/chat-logs"

# The model's own output is never trimmed. Tool RESULTS are, because a file read
# can be megabytes and would bury the exchange being debugged.
_TOOL_RESULT_LIMIT = 4000


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


class SimpleTurnLog:
    """One markdown file per user turn; rounds append to it."""

    def __init__(self, workspace: Path, turn_number: int, model: str) -> None:
        self.dir = Path(workspace) / LOG_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.path = self.dir / f"{stamp}-turn-{turn_number:03d}.md"
        self.model = model
        self._started = time.perf_counter()
        self._round = 0

    def _append(self, text: str) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass  # logging must never break a turn

    def open_turn(self, user_message: str) -> None:
        when = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._append(
            f"# Turn - {when}\n\n"
            f"**model** `{self.model}`\n\n"
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
        if len(body) > _TOOL_RESULT_LIMIT:
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
        try:
            shutil.copyfile(self.path, self.dir / "latest.md")
        except OSError:
            pass


def next_turn_number(workspace: Path) -> int:
    directory = Path(workspace) / LOG_DIR
    if not directory.is_dir():
        return 1
    return sum(1 for p in directory.glob("*-turn-*.md")) + 1
