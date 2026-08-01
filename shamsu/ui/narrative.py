"""Human-readable run report - "what actually happened" for one turn.

Every run exposes one `report.md` as its primary log. In essential mode it is a
concise account of the prompt, approach, tools, mutations, verification, and
answer. Verbose mode expands the same report with model exchanges, reasoning
traces, context, tool payloads, and command output. Machine evidence lives in
the sibling `.evidence/` directory rather than crowding the run root.

It exists because that story was previously only ever printed to the console by
`/trace verbose` and then lost. Two consequences shape this module:

* Detail is NOT tied to console verbosity. `emit_trace` gates *printing* on the
  workspace trace mode; the configured log mode controls the report. `/trace
  quiet` silences the terminal, never the file.
* Two destinations, both inside the workspace's own `.shamsu/`::

      runs/<run-id>/report.md          this turn
      sessions/<session-id>/report.md  every turn, appended when it closes

  The session roll-up is appended once, on close, rather than incrementally -
  concurrent runs then cannot interleave half-written turns into it.

Everything is best-effort: a logging failure must never break a run. Text is
redacted through shamsu.safety.commands.redact, the project's single
secret-pattern source. Like the rest of the run log, nothing written here is
ever read back into a model prompt.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shamsu.action_ledger.config import resolve_log_level
from shamsu.safety.commands import redact
from shamsu.ui.trace import EVENT_LABELS

NARRATIVE_FILE = "report.md"

# Keep inline prose readable. Verbose payloads use collapsible detail blocks and
# remain bounded; the redacted artifact files retain the full text.
_MAX_PROMPT_CHARS = 4000
_MAX_ANSWER_CHARS = 4000
_MAX_MESSAGE_CHARS = 600
_MAX_VALUE_CHARS = 200
_MAX_VERBOSE_BLOCK_CHARS = 50000

_ESSENTIAL_EVENTS = frozenset(
    {
        "route.detected",
        "plan.created",
        "clarification.needed",
        "clarification.answered",
        "tool.salvaged",
        "verify.result",
        "workflow.blocked",
        "workflow.finished",
        "run.timed_out",
        "run.cancelled",
    }
)

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
    """Appends the readable story of one run to `report.md`.

    Path-based on purpose: it never imports the ActionLedger or SessionLogger,
    so it can be pointed at any run/session directory (and tested) without
    building either.
    """

    def __init__(
        self,
        run_dir: Path,
        session_dir: Path | None = None,
        run_id: str = "",
        log_level: str = "essential",
    ) -> None:
        self.run_dir = Path(run_dir)
        self.session_dir = Path(session_dir) if session_dir is not None else None
        self.run_id = run_id or self.run_dir.name
        self.log_level = resolve_log_level({"log_level": log_level})
        self.verbose = self.log_level == "verbose"

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
            "# SHAMSU Run Report",
            "",
            f"- **Run:** `{self.run_id}`",
            f"- **Started:** {_now()}",
            f"- **Mode:** {self.log_level}",
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
        if not self.verbose and event_type not in _ESSENTIAL_EVENTS:
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
        if self.verbose and arguments:
            self._append(_details("Arguments", arguments))

    def append_tool_result(
        self,
        name: str,
        ok: bool,
        message: str = "",
        data: Any = None,
    ) -> None:
        """Record what the tool actually did."""
        outcome = "ok" if ok else "FAILED"
        text = " ".join(_clip(redact(str(message or "")), _MAX_MESSAGE_CHARS).split())
        detail = f": {text}" if text else ""
        self._append(f"  - {outcome}{detail}\n")
        if self.verbose and data not in (None, {}, [], ""):
            self._append(_details("Result data", data))

    def append_decision(self, record: dict[str, Any]) -> None:
        decision = redact(str(record.get("decision", "decision")))
        chosen = redact(str(record.get("chosen_action", "")))
        reason = redact(str(record.get("reason_summary", "")))
        line = f"- **Decision** `{decision}`"
        if chosen:
            line += f" -> `{chosen}`"
        if reason:
            line += f": {_clip(' '.join(reason.split()), _MAX_MESSAGE_CHARS)}"
        self._append(line + "\n")
        if self.verbose:
            self._append(_details("Decision evidence", record))

    def append_model_call(
        self,
        call_id: str,
        role: str,
        model: str,
        request_text: str,
    ) -> None:
        if not self.verbose:
            return
        self._append(
            f"- **Model call** `{redact(call_id)}` - role `{redact(role)}`, "
            f"model `{redact(model)}`\n"
        )
        self._append(_details_text("Prompt sent to model", request_text))

    def append_model_reasoning(
        self,
        call_id: str,
        role: str,
        model: str,
        reasoning: str,
    ) -> None:
        if self.verbose and reasoning:
            self._append(_details_text(f"Reasoning trace for {call_id}", reasoning))

    def append_model_result(
        self,
        call_id: str,
        role: str,
        model: str,
        response: str,
        error: str,
        duration_ms: float | None,
    ) -> None:
        if not self.verbose:
            return
        outcome = "FAILED" if error else "completed"
        duration = f" in {duration_ms:.0f} ms" if isinstance(duration_ms, (int, float)) else ""
        self._append(f"  - Model `{redact(call_id)}` {outcome}{duration}\n")
        if error:
            self._append(_details_text("Model error", error))
        elif response:
            self._append(_details_text("Model response", response))

    def append_context(self, record: dict[str, Any]) -> None:
        if not self.verbose:
            return
        context_id = redact(str(record.get("context_id", "context")))
        tokens = record.get("token_estimate", "unknown")
        snippets = len(record.get("snippets") or [])
        self._append(
            f"- **Context** `{context_id}` - {tokens} estimated tokens, "
            f"{snippets} snippet(s)\n"
        )
        self._append(_details("Context payload", record))

    def append_command(
        self,
        command: str,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        outcome = "passed" if exit_code == 0 else "FAILED"
        self._append(
            f"- **Command** `{redact(command)}` - {outcome} (exit {exit_code})\n"
        )
        if self.verbose and stdout:
            self._append(_details_text("stdout", stdout))
        if self.verbose and stderr:
            self._append(_details_text("stderr", stderr))

    def append_mutation(
        self,
        status: str,
        files: list[str],
        error: str,
        transaction_id: str,
    ) -> None:
        rendered_files = ", ".join(f"`{redact(str(path))}`" for path in files) or "none"
        self._append(f"- **Files changed** ({redact(status)}): {rendered_files}\n")
        if error:
            self._append(f"  - Error: {_clip(redact(error), _MAX_MESSAGE_CHARS)}\n")
        if self.verbose and transaction_id:
            self._append(f"  - Transaction: `{redact(transaction_id)}`\n")

    def append_verification(
        self,
        passed: bool,
        command: str,
        summary: str,
        exit_code: int | None,
        files: list[str],
    ) -> None:
        outcome = "passed" if passed else "FAILED"
        target = f" `{redact(command)}`" if command else ""
        exit_text = f" (exit {exit_code})" if exit_code is not None else ""
        detail = f": {_clip(redact(summary), _MAX_MESSAGE_CHARS)}" if summary else ""
        self._append(f"- **Verification**{target} - {outcome}{exit_text}{detail}\n")
        if self.verbose and files:
            self._append(f"  - Files: {', '.join(f'`{redact(str(path))}`' for path in files)}\n")

    def close_turn(self, final: str = "", status: str = "") -> None:
        """Finish the turn with the answer, then fold it into the session
        roll-up. Safe to call more than once per run - the marker keeps the
        roll-up from gaining duplicate copies of the same turn."""
        if self._is_closed():
            return
        lines = ["", "## Result", "", f"- **Status:** {status or 'unknown'}", "", "**Answer**", ""]
        answer = _clip(redact(str(final or "")), _MAX_ANSWER_CHARS).strip()
        lines.append(answer if answer else "_(no final answer recorded)_")
        lines.extend(
            [
                "",
                (
                    "_Verbose human detail and raw redacted evidence are available "
                    f"under `runs/{self.run_id}/.evidence/`._"
                    if self.verbose
                    else "_Essential mode omits per-model raw artifacts._"
                ),
                "",
                _CLOSED_MARKER,
                "",
            ]
        )
        self._append("\n".join(lines))
        self._roll_up_to_session(answer, status)

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

    def _roll_up_to_session(self, answer: str, status: str) -> None:
        session_path = self.session_path
        if session_path is None:
            return
        try:
            if not self.session_dir.is_dir():
                # No such session (headless run without a session logger);
                # the per-run narrative is still complete on its own.
                return
            report = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        if not report.strip():
            return
        prompt_section = report.split("**What SHAMSU did**", 1)[0].rstrip()
        block = "\n".join(
            [
                prompt_section,
                "",
                f"- **Status:** {status or 'unknown'}",
                f"- **Report:** `runs/{self.run_id}/report.md`",
                "",
                "**Answer**",
                "",
                _clip(answer, 1200) if answer else "_(no final answer recorded)_",
            ]
        )
        try:
            with session_path.open("a", encoding="utf-8") as handle:
                handle.write(block.rstrip() + "\n\n---\n\n")
        except OSError:
            pass


_CLOSED_MARKER = "<!-- shamsu:turn-closed -->"


_LAYOUT_README = """# .shamsu/

SHAMSU's workspace-local state and logs. Safe to delete: nothing here is needed
to run SHAMSU again, and none of it is ever fed back into a model prompt.

Every request produces one human-readable report.

## Human-readable reports

    runs/<run-id>/report.md          one request
    sessions/<session-id>/report.md  concise conversation roll-up

The report contains the prompt, approach, tools, changed files, verification,
errors, and final answer. Start here. It is written even when `/trace quiet`
silences the console.

## Machine evidence

    runs/<run-id>/
      report.md               human-readable report
      .evidence/              internal redacted machine records

Run ids sort chronologically, so the newest run is the last folder.

## Reading it from the REPL

    /logs              where everything is, and the current detail level
    /run report        the story of the last run
    /runs              list recent runs
    /run export <id>   zip one run up to share

## Detail level

`essential` is the default: a concise report and compact evidence without
per-model prompt, reasoning, response, or context files. `verbose` expands the
same report with model calls, reasoning traces, tool payloads, command output,
decisions, and context details while retaining full redacted evidence.

Change it with `/logs mode essential|verbose` or `SHAMSU_LOG_LEVEL`.
The legacy names `compact` and `full` remain accepted as aliases. Runs older
than `retention_days` (30) are removed by `/run clean`.

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
    the report and evidence folders are. Written once and then left alone, so a
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


def _details(title: str, value: Any) -> str:
    rendered = json.dumps(value, indent=2, ensure_ascii=True, default=str)
    return _details_text(title, rendered, language="json")


def _details_text(title: str, text: str, language: str = "text") -> str:
    safe_title = redact(str(title))
    safe_text = _clip(redact(str(text or "")), _MAX_VERBOSE_BLOCK_CHARS)
    return (
        f"  <details>\n"
        f"  <summary>{safe_title}</summary>\n\n"
        f"  ```{language}\n{safe_text}\n  ```\n"
        f"  </details>\n"
    )


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
