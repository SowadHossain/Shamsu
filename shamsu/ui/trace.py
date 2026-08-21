"""Structured, user-visible working trace for SHAMSU.

This is the single source of truth for the trace *mode* (persisted per
workspace) and for emitting structured trace events. It deliberately never
carries hidden model reasoning: callers pass a short, sanitized message and an
optional payload of already-safe fields. Long values are truncated and secrets
are redacted before anything is printed or logged.

Trace modes:
  quiet   - print nothing (only final answers / approvals, handled elsewhere)
  normal  - print route, plan summary, tool start/result summaries, blockers
  verbose - additionally print sanitized args, candidates, workflow ids, etc.
  raw     - like verbose, plus raw model-visible content / tool payloads (debug)

`emit_trace` both logs the event to the session (always, for the audit trail)
and prints it to the console when the current mode allows the event's level.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rich.console import Console

from shamsu.safety.commands import redact
from shamsu.safety.sandbox import Sandbox
from shamsu.session.manager import SessionLogger

TRACE_MODES = ("quiet", "normal", "verbose", "raw")
DEFAULT_TRACE_MODE = "normal"

# Higher number = more detail required before it prints. "raw" is the most
# verbose debug mode (shows raw model-visible content / tool payloads).
_MODE_RANK = {"quiet": 0, "normal": 1, "verbose": 2, "raw": 3}
_LEVEL_RANK = {"quiet": 0, "normal": 1, "verbose": 2, "raw": 3}

# Keep any single trace value short; the trace is a glanceable process view,
# not a place to dump file contents or full tool payloads.
_MAX_VALUE_CHARS = 300
_MAX_MESSAGE_CHARS = 600


def trace_config_path(workspace: Path) -> Path:
    return Sandbox(workspace).validate(Path(".shamsu") / "trace.json")


def read_trace_mode(workspace: Path) -> str:
    path = trace_config_path(workspace)
    if not path.exists():
        return DEFAULT_TRACE_MODE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return DEFAULT_TRACE_MODE
    mode = str(data.get("mode", DEFAULT_TRACE_MODE)).lower()
    return mode if mode in TRACE_MODES else DEFAULT_TRACE_MODE


def write_trace_mode(workspace: Path, mode: str) -> None:
    if mode not in TRACE_MODES:
        raise ValueError(f"Unknown trace mode: {mode}")
    path = trace_config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": mode}, indent=2), encoding="utf-8")


def should_emit(mode: str, level: str) -> bool:
    """True when console output at `level` is allowed under the current `mode`.

    quiet suppresses everything; normal shows normal-level events; verbose shows
    both normal and verbose events.
    """
    return _MODE_RANK.get(mode, 1) >= _LEVEL_RANK.get(level, 1) and mode != "quiet"


def sanitize_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return {str(key): _sanitize_value(value) for key, value in payload.items()}


def format_trace_line(event_type: str, message: str, payload: dict[str, Any] | None, level: str) -> str:
    """Build the styled console line for a trace event. Kept separate so tests
    can assert on the plain text without a live console."""
    label = _EVENT_LABELS.get(event_type, event_type)
    safe_message = _truncate(redact(message), _MAX_MESSAGE_CHARS)
    line = f"{label}: {safe_message}" if safe_message else label
    if level in ("verbose", "raw") and payload:
        safe = sanitize_payload(payload)
        if safe:
            extras = " ".join(f"{key}={value}" for key, value in safe.items())
            line = f"{line} [{extras}]"
    return line


def emit_trace(
    console: Console | None,
    session_logger: SessionLogger | None,
    workspace: Path,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
    level: str = "normal",
) -> None:
    """Log a trace event to the session and print it when the mode allows.

    `event_type` is one of the documented trace events (route.detected,
    plan.created, step.started, tool.started, tool.finished, tool.failed,
    clarification.needed, clarification.answered, workflow.blocked,
    workflow.finished, ...). Unknown types are printed with their raw name.
    """
    safe_payload = sanitize_payload(payload)
    if session_logger is not None:
        session_logger.log(
            f"trace.{event_type}",
            {"level": level, "message": _truncate(redact(message), _MAX_MESSAGE_CHARS), **safe_payload},
            _truncate(redact(message), _MAX_MESSAGE_CHARS),
            workflow_id="trace",
        )
    # The narrative log records every event at full detail, independently of the
    # console mode below: `/trace quiet` must silence the terminal, not the file.
    _append_to_narrative(event_type, message, payload)
    mode = read_trace_mode(workspace)
    if console is None or not should_emit(mode, level):
        return
    style = _EVENT_STYLES.get(event_type, "dim")
    console.print(f"[{style}]{format_trace_line(event_type, message, payload, mode)}[/{style}]")


EVENT_LABELS = {
    "route.detected": "Route",
    "plan.created": "Plan",
    "assistant.content": "Model",
    "assistant.thinking": "Reasoning",
    "assistant.thinking.delta": "Thinking",
    "context.sent": "Context",
    "context.search": "Search",
    "verify.result": "Verify",
    "tool.salvaged": "Salvaged",
    "llm.timeout": "Timeout",
    "step.started": "Step",
    "tool.started": "Tool",
    "tool.finished": "Result",
    "tool.failed": "Tool failed",
    "clarification.needed": "Question",
    "clarification.answered": "Answer",
    "workflow.blocked": "Blocked",
    "workflow.finished": "Done",
}

# Shared with shamsu.ui.turnlog so the session log and the console use the
# same wording for the same event.
_EVENT_LABELS = EVENT_LABELS

_EVENT_STYLES = {
    "route.detected": "cyan",
    "plan.created": "cyan",
    # Reasoning is surfaced but de-emphasized: the answer is what matters, the
    # trace just proves the model reasoned and gives a glimpse.
    "assistant.thinking": "dim",
    "assistant.thinking.delta": "dim",
    "context.search": "cyan",
    "verify.result": "cyan",
    "tool.salvaged": "magenta",
    "clarification.needed": "yellow",
    "clarification.answered": "green",
    "tool.failed": "yellow",
    "workflow.blocked": "yellow",
    "workflow.finished": "green",
}


def narrative_for_current_run() -> "Any | None":
    """The turn-log writer for the run in flight, or None when there isn't one
    (no active run, or the ledger is disabled).

    Imported lazily: shamsu.ui.turnlog imports this module for EVENT_LABELS,
    so a module-level import here would cycle.
    """
    from shamsu.action_ledger.context import get_current_run
    from shamsu.ui.turnlog import writer_for

    ledger = get_current_run()
    if ledger is None or not getattr(ledger, "enabled", False):
        return None
    session_dir = None
    session_id = str(getattr(ledger, "session_id", "") or "")
    if session_id:
        session_dir = Path(ledger.workspace) / ".shamsu" / "sessions" / session_id
    return writer_for(
        ledger.run_dir,
        session_dir,
        run_id=ledger.run_id,
        log_level=getattr(ledger, "log_level", "essential"),
        turn_id=str(getattr(ledger, "turn_id", "") or ""),
        source=str(getattr(ledger, "source", "") or ""),
    )


def _append_to_narrative(
    event_type: str, message: str, payload: dict[str, Any] | None
) -> None:
    try:
        writer = narrative_for_current_run()
        if writer is not None:
            writer.append(event_type, message, payload)
    except Exception:
        # The narrative is an observability nicety; never break a run for it.
        pass


def _sanitize_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        rendered = ", ".join(str(item) for item in list(value)[:8])
        if len(value) > 8:
            rendered += f", ... (+{len(value) - 8})"
        return _truncate(redact(rendered), _MAX_VALUE_CHARS)
    return _truncate(redact(str(value)), _MAX_VALUE_CHARS)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"
