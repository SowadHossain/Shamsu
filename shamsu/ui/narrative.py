"""Human-readable narrative log - "what actually happened" for one turn.

Tier 2 of SHAMSU's logging. Where `.shamsu/runs/<run-id>/` holds the machine
evidence (the full prompt, the full chain-of-thought, every tool payload), this
renders the same turn as a short markdown story: the prompt that came in, the
route taken, each tool call and what it did, then the answer.

It exists because that story was previously only ever printed to the console by
`/trace verbose` and then lost. Two consequences shape this module:

* Detail is NOT tied to console verbosity. `emit_trace` gates *printing* on the
  workspace trace mode; the narrative is always written in full. `/trace quiet`
  silences the terminal, never the file.
* Two destinations, both inside the workspace's own `.shamsu/`::

      runs/<run-id>/narrative.md          this turn
      sessions/<session-id>/narrative.md  every turn, appended when it closes

  The session roll-up is appended once, on close, rather than incrementally -
  concurrent runs then cannot interleave half-written turns into it.

Everything is best-effort: a logging failure must never break a run. Text is
redacted through shamsu.safety.commands.redact, the project's single
secret-pattern source. Like the rest of the run log, nothing written here is
ever read back into a model prompt.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shamsu.safety.commands import redact
from shamsu.ui.trace import EVENT_LABELS

NARRATIVE_FILE = "narrative.md"

# The narrative is a summary, not a payload dump - the deep log already holds
# the untruncated text, and this stays readable.
_MAX_PROMPT_CHARS = 4000
_MAX_ANSWER_CHARS = 4000
_MAX_MESSAGE_CHARS = 600
_MAX_VALUE_CHARS = 200

# Trace events that add nothing to a human narrative, plus the tool events:
# those are recorded from the ActionLedger instead (see append_tool_call), which
# every execution path funnels through - the trace events only fire on the chat
# loop, so a composite/scaffold run would otherwise show no tools at all.
_SKIP_EVENTS = frozenset(
    {"assistant.content", "context.sent", "tool.started", "tool.finished", "tool.failed"}
)

# Tool arguments worth naming in the story: which file, which command.
_TOOL_ARG_KEYS = ("filepath", "path", "command", "query", "url", "pattern")

# Payload keys worth showing inline, in this order. Everything else is dropped:
# the point is "which tool, on what", not the whole argument object.
_INTERESTING_KEYS = (
    "tool", "tool_name", "filepath", "path", "command", "route", "intent",
    "model", "exit_code", "ok", "files", "chars", "thinking_chars", "round",
)


class NarrativeWriter:
    """Appends the readable story of one run to `narrative.md`.

    Path-based on purpose: it never imports the ActionLedger or SessionLogger,
    so it can be pointed at any run/session directory (and tested) without
    building either.
    """

    def __init__(
        self,
        run_dir: Path,
        session_dir: Path | None = None,
        run_id: str = "",
    ) -> None:
        self.run_dir = Path(run_dir)
        self.session_dir = Path(session_dir) if session_dir is not None else None
        self.run_id = run_id or self.run_dir.name

    @property
    def path(self) -> Path:
        return self.run_dir / NARRATIVE_FILE

    @property
    def session_path(self) -> Path | None:
        if self.session_dir is None:
            return None
        return self.session_dir / NARRATIVE_FILE

    # -- turn lifecycle ----------------------------------------------------

    def open_turn(self, prompt: str, route: str = "") -> None:
        """Start this run's narrative with the prompt that triggered it."""
        lines = [
            f"## {_now()} - {self.run_id}",
            "",
            "**Prompt**",
            "",
            _blockquote(_clip(redact(str(prompt or "")), _MAX_PROMPT_CHARS)),
            "",
        ]
        if route:
            lines.extend([f"**Route**: {redact(str(route))}", ""])
        lines.extend(["**What SHAMSU did**", ""])
        self._write("\n".join(lines) + "\n")

    def append(
        self, event_type: str, message: str, payload: dict[str, Any] | None = None
    ) -> None:
        """Add one step. Called for every trace event, at every level."""
        if event_type in _SKIP_EVENTS:
            return
        line = _render_step(event_type, message, payload)
        if line:
            self._append(line + "\n")

    def append_tool_call(self, name: str, arguments: dict[str, Any] | None = None) -> None:
        """Record a tool being used, and on what.

        Driven from the ActionLedger rather than the trace, because every
        execution path logs its tools there - the chat loop, the composite
        runner, the scaffold pipeline."""
        target = _tool_target(arguments)
        suffix = f" - {target}" if target else ""
        self._append(f"- **Tool** `{redact(str(name))}`{_clip(suffix, _MAX_VALUE_CHARS)}\n")

    def append_tool_result(self, name: str, ok: bool, message: str = "") -> None:
        """Record what the tool actually did."""
        outcome = "ok" if ok else "FAILED"
        text = " ".join(_clip(redact(str(message or "")), _MAX_MESSAGE_CHARS).split())
        detail = f": {text}" if text else ""
        self._append(f"  - {outcome}{detail}\n")

    def close_turn(self, final: str = "", status: str = "") -> None:
        """Finish the turn with the answer, then fold it into the session
        roll-up. Safe to call more than once per run - the marker keeps the
        roll-up from gaining duplicate copies of the same turn."""
        if self._is_closed():
            return
        lines = ["", "**Answer**", ""]
        answer = _clip(redact(str(final or "")), _MAX_ANSWER_CHARS).strip()
        lines.append(answer if answer else "_(no final answer recorded)_")
        lines.extend(
            [
                "",
                f"_Status: {status or 'unknown'} - full detail: "
                f"runs/{self.run_id}/ (prompts/, cot/, responses/)_",
                "",
                _CLOSED_MARKER,
                "",
            ]
        )
        self._append("\n".join(lines))
        self._roll_up_to_session()

    # -- io (best-effort throughout) ---------------------------------------

    def _write(self, text: str) -> None:
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text, encoding="utf-8")
        except OSError:
            pass

    def _append(self, text: str) -> None:
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            # A trace event can land before the run formally opens; keep the
            # step rather than dropping it on the floor.
            if not self.path.exists():
                self._write(f"## {_now()} - {self.run_id}\n\n**What SHAMSU did**\n\n")
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass

    def _is_closed(self) -> bool:
        try:
            return _CLOSED_MARKER in self.path.read_text(encoding="utf-8")
        except OSError:
            return False

    def _roll_up_to_session(self) -> None:
        session_path = self.session_path
        if session_path is None:
            return
        try:
            if not self.session_dir.is_dir():
                # No such session (headless run without a session logger);
                # the per-run narrative is still complete on its own.
                return
            block = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        if not block.strip():
            return
        try:
            with session_path.open("a", encoding="utf-8") as handle:
                handle.write(block.replace(_CLOSED_MARKER, "").rstrip() + "\n\n---\n\n")
        except OSError:
            pass


_CLOSED_MARKER = "<!-- shamsu:turn-closed -->"


_LAYOUT_README = """# .shamsu/

SHAMSU's workspace-local state and logs. Safe to delete: nothing here is needed
to run SHAMSU again, and none of it is ever fed back into a model prompt.

Two logs are written for every request.

## Narrative log - the readable story

    runs/<run-id>/narrative.md          one request
    sessions/<session-id>/narrative.md  the whole conversation

The prompt that came in, the route taken, each tool call and what it did, then
the answer. Start here. Written in full even when `/trace quiet` silences the
console.

## Deep log - everything the model saw and produced

    runs/<run-id>/
      prompts/model_NNNN.txt    the full prompt as sent (system + messages + tool schemas)
      cot/model_NNNN.txt        the full chain-of-thought, untruncated
      responses/model_NNNN.txt  the full model response
      model-calls.jsonl         one row per call, with paths to the three above
      tool-calls.jsonl          every tool call, its arguments and its result
      tool-results/             full result payloads too large to inline
      commands/                 stdout and stderr of every command run
      mutations/mutations.jsonl file changes, with rollback transaction ids
      contexts/                 the context packs built for each call
      decisions.jsonl           routing and planning decisions, with reasons
      events.jsonl              the machine-readable timeline
      summary.json, manifest.json, final-output.md

Run ids sort chronologically, so the newest run is the last folder.

## Reading it from the REPL

    /logs              where everything is, and the current detail level
    /run narrative     the story of the last run
    /run prompt        what the model was actually sent
    /run cot           what the model was thinking
    /runs              list recent runs
    /run export <id>   zip one run up to share

## Detail level

Full capture is the default. `SHAMSU_LOG_LEVEL=compact` keeps inline previews
but writes no `prompts/`, `cot/` or `responses/` files; the same value can be
set as `log_level` in `action_ledger/config.json`. Runs older than
`retention_days` (30) are removed by `/run clean`.

## Secrets

Everything is redacted on the way to disk through a single shared
secret-pattern list. Redaction is pattern-based, so treat these files as
sensitive anyway - they contain your prompts and your source code.

## Other folders

    sessions/     conversation transcripts, summaries and resume state
    audit/        a separate per-session event trail (see /audit-log)
    plans/        saved plans from `plan <task>`
    action_ledger/config.json   logging configuration
"""


def write_layout_readme(workspace: Path) -> Path | None:
    """Explain the .shamsu folder, in the folder itself.

    Someone who opens `.shamsu/` in a file browser should not have to guess what
    `cot/` or `model-calls.jsonl` are. Written once and then left alone, so a
    user's own edits survive. Best-effort; returns the path, or None."""
    root = Path(workspace).resolve() / ".shamsu"
    path = root / "README.md"
    try:
        if path.exists():
            return path
        root.mkdir(parents=True, exist_ok=True)
        path.write_text(_LAYOUT_README, encoding="utf-8")
        return path
    except OSError:
        return None


def _render_step(event_type: str, message: str, payload: dict[str, Any] | None) -> str:
    label = EVENT_LABELS.get(event_type, event_type)
    text = _clip(redact(str(message or "")).strip(), _MAX_MESSAGE_CHARS)
    # Newlines would break the bullet list; the deep log keeps the real shape.
    text = " ".join(text.split())
    extras = _render_extras(payload)
    if not text and not extras:
        return f"- **{label}**"
    parts = [f"- **{label}**"]
    if text:
        parts.append(f": {text}")
    if extras:
        parts.append(f" ({extras})")
    return "".join(parts)


def _render_extras(payload: dict[str, Any] | None) -> str:
    if not payload:
        return ""
    pairs = []
    for key in _INTERESTING_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if value is None or value == "" or value == []:
            continue
        pairs.append(f"{key}={_clip(redact(_flatten(value)), _MAX_VALUE_CHARS)}")
    return ", ".join(pairs)


def _tool_target(arguments: dict[str, Any] | None) -> str:
    """The one thing a reader wants next to a tool name: the file it touched or
    the command it ran."""
    if not isinstance(arguments, dict):
        return ""
    for key in _TOOL_ARG_KEYS:
        value = arguments.get(key)
        if value:
            return f"{key}={redact(_flatten(value))}"
    return ""


def _flatten(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in list(value)[:5]]
        if len(value) > 5:
            items.append(f"... (+{len(value) - 5})")
        return ", ".join(items)
    return " ".join(str(value).split())


def _blockquote(text: str) -> str:
    lines = (text or "").splitlines() or [""]
    return "\n".join(f"> {line}" if line else ">" for line in lines)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
