"""Detailed per-session audit trail (JSONL) under .shamsu/audit/.

On-disk layout::

    .shamsu/audit/events.jsonl                 # every event, all sessions
    .shamsu/audit/sessions/<session-id>.jsonl  # one session's events
    .shamsu/audit/artifacts/                   # large generated artifacts
    .shamsu/audit/context-packs/               # context packs sent to the model

Every record is one JSON object with at least ``timestamp``, ``session_id`` and
``event_type``. Secret-looking values are redacted. Writing is best-effort: an
audit failure must never break the agent loop.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shamsu.safety.commands import redact

AUDIT_DIR_NAME = "audit"
EVENTS_FILE = "events.jsonl"
SESSIONS_DIR = "sessions"
ARTIFACTS_DIR = "artifacts"
CONTEXT_PACKS_DIR = "context-packs"

# Cap the size of any single logged string so a giant file write or command dump
# cannot bloat the JSONL. Full artifacts can still be written to artifacts/.
_MAX_FIELD_CHARS = 20_000


class SessionAuditLog:
    """Append-only JSONL audit log for a single session's run."""

    def __init__(self, workspace_root: Path, session_id: str | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.root = self.workspace_root / ".shamsu" / AUDIT_DIR_NAME
        self.events_path = self.root / EVENTS_FILE
        self.session_path = self.root / SESSIONS_DIR / f"{self.session_id}.jsonl"
        self.artifacts_dir = self.root / ARTIFACTS_DIR
        self.context_packs_dir = self.root / CONTEXT_PACKS_DIR
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        try:
            self.session_path.parent.mkdir(parents=True, exist_ok=True)
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            self.context_packs_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # -- core --------------------------------------------------------------

    def log(self, event_type: str, payload: dict[str, Any] | None = None, message: str = "") -> None:
        """Append one event to both the global and per-session JSONL files."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": self.session_id,
            "event_type": event_type,
            "message": _clip(message),
            **_redact(payload or {}),
        }
        line = json.dumps(record, default=str, ensure_ascii=False)
        for path in (self.events_path, self.session_path):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # Audit is best-effort; never break the run on a write failure.
                pass

    # -- convenience wrappers ---------------------------------------------

    def log_prompt(self, prompt: str) -> None:
        self.log("user.prompt", {"prompt": prompt}, "User prompt")

    def log_route(self, route: str, workflow: str = "", model: str = "", tier: str = "") -> None:
        self.log(
            "route.selected",
            {"route": route, "workflow": workflow or route, "model": model, "tier": tier},
            f"Route selected: {route}",
        )

    def log_context_pack(self, name: str, text: str) -> None:
        """Record a context pack sent to the model (truncated inline; full copy
        saved under context-packs/ for later inspection)."""
        summary = _clip(text)
        self.log("context.pack", {"name": name, "context_pack": summary}, "Context pack sent to model")
        try:
            (self.context_packs_dir / f"{self.session_id}-{name}.txt").write_text(text, encoding="utf-8")
        except OSError:
            pass

    def log_planner(self, plan_text: str) -> None:
        self.log("planner.output", {"planner_output": plan_text}, "Planner output")

    def log_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        self.log("tool.call", {"tool": name, "arguments": arguments}, f"Tool call: {name}")

    def log_tool_result(self, name: str, ok: bool, message: str, data: Any = None) -> None:
        self.log(
            "tool.result",
            {"tool": name, "ok": bool(ok), "result_message": message, "data": data},
            f"Tool result: {name}",
        )

    def log_file_change(
        self,
        action: str,
        filepath: str,
        content: str = "",
        diff: str = "",
        transaction_id: str = "",
    ) -> None:
        """Record a file create/edit/delete, including the written content and a
        before/after diff plus the rollback (transaction) id when present."""
        self.log(
            "file.change",
            {
                "action": action,
                "filepath": filepath,
                "content": content,
                "diff": diff,
                "transaction_id": transaction_id,
            },
            f"File {action}: {filepath}",
        )

    def log_command(self, command: str, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self.log(
            "command.output",
            {"command": command, "exit_code": exit_code, "stdout": stdout, "stderr": stderr},
            f"Command exited {exit_code}: {command}",
        )

    def log_approval(self, description: str, decision: bool) -> None:
        self.log(
            "approval.decision",
            {"description": description, "approved": bool(decision)},
            f"Approval {'granted' if decision else 'denied'}",
        )

    def log_error(self, category: str, message: str) -> None:
        self.log("error", {"category": category, "error": message}, f"Error: {category}")

    def log_final(self, answer: str) -> None:
        self.log("assistant.final", {"final": answer}, "Final assistant response")


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD_CHARS:
        return value[:_MAX_FIELD_CHARS] + f"\n... [truncated {len(value) - _MAX_FIELD_CHARS} chars]"
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact(_clip(value))
    if isinstance(value, dict):
        return {str(key): _redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value
