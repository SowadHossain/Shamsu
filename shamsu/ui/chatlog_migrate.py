"""Bring `.shamsu/chat-logs/` forward into the session log, redacted.

`shamsu/agents/simple_log.py` wrote one markdown file per session holding the
exact prompt, the raw reply and every tool result. It was deleted on 2026-08-21
because it contained no calls to `redact` - the one path in the project that
put model text on disk without going through the shared secret-pattern list.
Deleting the writer stopped new leaks; it did nothing about the files already
written, and those files are also the only readable record of every session
that ran before the session log existed.

This reads them back and replays them through `TurnLogWriter`, which means the
migrated output is not a copy: it is the same rendering path a live turn uses,
so it comes out redacted, with oversized payloads spilled to `attachments/`,
and identical in shape to a log written today.

## The format being parsed

    # Session <id>
    **model** `<name>` - started <when>

    # Turn <n> - <when>
    ## What you asked
    ```<prompt>```
    ## Round <n> - prompt sent to the model
    ### [1] system
    ```<text>```
    ## Round <n> - raw response  (<t>s)
    **thinking channel**
    ```<trace>```
    **content**
    ```<reply>```
    **tool calls requested**
    ```json ... ```
    ### tool `<name>` -> ok|FAILED
    *arguments*
    ```json ... ```
    *result*
    ```<text>```
    ## Final answer
    ```<answer>```

## Two traps, both load-bearing

**Headings inside content.** The model's own replies contain markdown - a
survey of the real files turns up `## Project Review Summary`,
`### **Priority Fixes Needed**` and `# Original content:`. A parser that
treats any `#` line as structure will cut a turn in half at the model's own
prose. Only the exact prefixes above are structural, and only outside a fence.

**Fences that grow.** The writer picked a fence long enough to survive the
content: ```` ``` ```` became ```` ```` ```` when the body contained backticks.
So a closer has to be at least as long as the opener that started the block,
and a shorter run of backticks inside is content.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

#: The pointer file the old writer kept beside the logs. Not a transcript.
POINTER_FILE = "latest.md"

_TURN_RE = re.compile(r"^# Turn (\d+)(?: - (.*))?$")
_ROUND_RE = re.compile(r"^## Round (\d+) - ?(.*)$")
_MESSAGE_RE = re.compile(r"^### \[(\d+)\] (\w+)$")
_TOOL_RE = re.compile(r"^### tool `([^`]+)` -> (\w+)$")
_FENCE_RE = re.compile(r"^(`{3,})\s*(\w*)\s*$")


@dataclass
class Round:
    """One model call and whatever it asked for."""

    number: int = 0
    prompt: str = ""
    thinking: str = ""
    content: str = ""
    error: str = ""
    #: Calls that came back with a result - a `### tool ... -> ok` section.
    tools: list[dict[str, Any]] = field(default_factory=list)
    #: What the model ASKED for, from the `tool calls requested` block. Kept
    #: separate because the same call appears in both places: once as the
    #: request and once as the result. Only the requests with no matching
    #: result are worth replaying, and those are exactly the calls a turn died
    #: in the middle of.
    requested: list[dict[str, Any]] = field(default_factory=list)

    def replayable(self) -> list[dict[str, Any]]:
        """Resolved calls, then any request that never got an answer."""
        return self.tools + self.requested[len(self.tools):]


@dataclass
class Turn:
    number: int = 0
    when: str = ""
    prompt: str = ""
    final: str = ""
    rounds: list[Round] = field(default_factory=list)


@dataclass
class ParsedLog:
    session_id: str = ""
    model: str = ""
    turns: list[Turn] = field(default_factory=list)


@dataclass
class MigrationResult:
    source: Path
    session_id: str = ""
    destination: Path | None = None
    turns: int = 0
    skipped: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.skipped and not self.error


def _blocks(lines: list[str], start: int) -> Iterator[tuple[int, str, str]]:
    """Yield ``(index, kind, text)`` for each fenced block or heading.

    One scanner for the whole file, because fence state is the thing every
    other decision depends on: a `#` inside a fence is content, and a fence is
    only closed by a run of backticks at least as long as the one that opened
    it.
    """
    index = start
    while index < len(lines):
        line = lines[index].rstrip("\n")
        fence = _FENCE_RE.match(line)
        if fence is not None:
            opener = len(fence.group(1))
            body: list[str] = []
            index += 1
            while index < len(lines):
                closer = _FENCE_RE.match(lines[index].rstrip("\n"))
                if closer is not None and len(closer.group(1)) >= opener:
                    index += 1
                    break
                body.append(lines[index].rstrip("\n"))
                index += 1
            yield (index, "fence", "\n".join(body))
            continue
        yield (index + 1, "line", line)
        index += 1


def parse_chat_log(text: str) -> ParsedLog:
    """Turn one old `chat-logs/*.md` into structured turns."""
    lines = text.splitlines()
    parsed = ParsedLog()
    turn: Turn | None = None
    round_: Round | None = None
    # What the NEXT fence belongs to. The format is heading-then-block
    # throughout, so one variable carries the whole state machine.
    expect = ""
    tool: dict[str, Any] | None = None

    for _, kind, payload in _blocks(lines, 0):
        if kind == "fence":
            if expect == "prompt" and turn is not None:
                turn.prompt = payload
            elif expect == "final" and turn is not None:
                turn.final = payload
            elif expect == "message" and round_ is not None:
                round_.prompt += (payload + "\n\n") if payload else ""
            elif expect == "thinking" and round_ is not None:
                round_.thinking = payload
            elif expect == "content" and round_ is not None:
                round_.content = "" if payload.strip() == "*(empty)*" else payload
            elif expect == "tool_calls" and round_ is not None:
                round_.requested.extend(_tool_calls(payload))
            elif expect == "tool_arguments" and tool is not None:
                tool["arguments"] = _loads(payload)
            elif expect == "tool_result" and tool is not None:
                tool["result"] = payload
            expect = ""
            continue

        line = payload
        if line.startswith("# Session "):
            parsed.session_id = line.removeprefix("# Session ").strip()
            continue
        if line.startswith("**model** `"):
            parsed.model = line.split("`")[1] if "`" in line else ""
            continue

        match = _TURN_RE.match(line)
        if match is not None:
            turn = Turn(number=int(match.group(1)), when=(match.group(2) or "").strip())
            parsed.turns.append(turn)
            round_ = None
            tool = None
            continue
        if turn is None:
            # Anything before the first turn header is the file preamble.
            continue

        if line == "## What you asked":
            expect = "prompt"
            continue
        if line == "## Final answer":
            expect = "final"
            continue

        match = _ROUND_RE.match(line)
        if match is not None:
            number = int(match.group(1))
            label = match.group(2).strip().lower()
            if label.startswith("prompt sent"):
                round_ = Round(number=number)
                turn.rounds.append(round_)
            elif round_ is None or round_.number != number:
                # A response with no `prompt sent` header before it. The writer
                # always emitted both, so this is a file that was cut off while
                # being written - and the half that survived is still a record.
                # Opening a round here keeps it instead of dropping it on the
                # floor for want of a heading.
                round_ = Round(number=number)
                turn.rounds.append(round_)
            if label.startswith("error"):
                round_.error = "the model call failed"
            tool = None
            continue

        if _MESSAGE_RE.match(line) is not None:
            expect = "message"
            continue

        match = _TOOL_RE.match(line)
        if match is not None and round_ is not None:
            tool = {"name": match.group(1), "ok": match.group(2).lower() == "ok"}
            round_.tools.append(tool)
            continue

        if line == "**thinking channel**":
            expect = "thinking"
        elif line == "**content**":
            expect = "content"
        elif line == "**tool calls requested**":
            expect = "tool_calls"
        elif line == "*arguments*":
            expect = "tool_arguments"
        elif line == "*result*":
            expect = "tool_result"

    return parsed


def _loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _tool_calls(text: str) -> list[dict[str, Any]]:
    """The `tool calls requested` block, as names and arguments.

    Only used when no `### tool` section followed - a call the model asked for
    and never got a result to, which is exactly the shape of a turn that died
    mid-round and worth keeping.
    """
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        return []
    calls: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        function = (item or {}).get("function") or {}
        name = str(function.get("name") or "")
        if not name:
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            arguments = _loads(arguments)
        calls.append({"name": name, "requested": True, "arguments": arguments or {}})
    return calls


def migrate_file(
    source: Path, session_dir: Path, *, model: str = "", append: bool = False
) -> MigrationResult:
    """Replay one old log into `session_dir`, redacted, and report what happened.

    Never overwrites. A session that already has a `log-summary.md` is left
    alone and reported as skipped: the live log is the authoritative one, and
    interleaving a replayed history into a document being appended to would put
    turns out of order.

    *append* is for the case where THIS migration created that file a moment
    ago. The old writer started a new file when a session gained a title, so
    one conversation can be spread over several - `test1` has two files for
    session `20260820-012727-edc0`, holding turn 1 and turn 2. Without this the
    second file is read as a collision and its turn is dropped.
    """
    from shamsu.ui.turnlog import TurnLogWriter

    result = MigrationResult(source=source)
    try:
        text = source.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        result.error = str(exc)
        return result

    parsed = parse_chat_log(text)
    result.session_id = parsed.session_id or source.stem.split("--")[0]
    if not parsed.turns:
        result.skipped = "no turns found"
        return result

    summary = session_dir / "log-summary.md"
    if summary.exists() and not append:
        result.skipped = "already has a session log"
        return result

    session_dir.mkdir(parents=True, exist_ok=True)
    model = parsed.model or model
    for turn in parsed.turns:
        writer = TurnLogWriter(
            session_dir,
            session_dir,
            run_id=f"migrated-{result.session_id}",
            turn_id=f"migrated-t{turn.number}",
            log_level="verbose",
        )
        writer.open_turn(turn.prompt, when=turn.when)
        for round_ in turn.rounds:
            if round_.prompt:
                writer.append_model_call("", "coder", model, round_.prompt)
            if round_.thinking:
                writer.append_model_reasoning("", "coder", model, round_.thinking)
            writer.append_model_result(
                "", "coder", model, round_.content, round_.error, None
            )
            for tool in round_.replayable():
                writer.append_tool_call(tool["name"], tool.get("arguments") or {})
                if "result" in tool:
                    writer.append_tool_result(
                        tool["name"], bool(tool.get("ok")), str(tool.get("result") or "")
                    )
        writer.close_turn(turn.final, "migrated", "replayed from chat-logs")
    result.destination = session_dir
    result.turns = len(parsed.turns)
    return result


def legacy_logs(workspace: Path) -> list[Path]:
    """Every old transcript in *workspace*, newest last. Pointers excluded."""
    folder = Path(workspace) / ".shamsu" / "chat-logs"
    try:
        return sorted(
            path
            for path in folder.glob("*.md")
            if path.is_file() and path.name != POINTER_FILE
        )
    except OSError:
        return []


def _first_turn_number(source: Path) -> tuple[int, str]:
    """Sort key: where this file sits in its conversation.

    By the turn number the file itself records, not by filename or mtime. The
    filename carries a title slug that sorts alphabetically ("okay-fix..."
    before "untitled-session") and would put turn 2 ahead of turn 1, and mtime
    is when the file was last APPENDED to, which overlaps between files of the
    same session.
    """
    try:
        for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            match = _TURN_RE.match(line)
            if match is not None:
                return (int(match.group(1)), source.name)
    except OSError:
        pass
    return (10**6, source.name)


def migrate_workspace(workspace: Path) -> list[MigrationResult]:
    """Migrate every old log in *workspace*. Originals are never touched.

    The session id is the filename up to `--`; the old writer appended a slug
    of the title. A file whose session no longer has a directory still gets
    one, because the alternative is leaving the only readable record of that
    run in the folder we are telling people to delete.

    Grouped by session, because one conversation can span several files - the
    old writer opened a new one when a session gained a title.
    """
    sessions = Path(workspace) / ".shamsu" / "sessions"
    grouped: dict[str, list[Path]] = {}
    for source in legacy_logs(workspace):
        grouped.setdefault(source.stem.split("--")[0], []).append(source)

    results: list[MigrationResult] = []
    for session_id, sources in sorted(grouped.items()):
        wrote = False
        for source in sorted(sources, key=_first_turn_number):
            result = migrate_file(
                source, sessions / session_id, append=wrote
            )
            wrote = wrote or result.ok
            results.append(result)
    return results
