"""Local JSONL audit trail for safety-sensitive actions."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shamsu.safety.commands import redact
from shamsu.safety.sandbox import Sandbox, SecurityError

AUDIT_LOG_NAME = "audit.jsonl"


class AuditLogger:
    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.log_path = self.sandbox.validate(Path(".shamsu") / AUDIT_LOG_NAME)

    def log(
        self,
        action_type: str,
        result: str,
        affected_paths: list[str | Path] | None = None,
        details: dict[str, Any] | None = None,
    ) -> Path:
        safe_paths = self._safe_relative_paths(affected_paths or [])
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action_type": action_type,
            "result": result,
            "affected_paths": safe_paths,
            "details": _redact_data(details or {}),
        }
        self.log_path = self.sandbox.validate(self.log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
        return self.log_path

    def _safe_relative_paths(self, paths: list[str | Path]) -> list[str]:
        safe_paths: list[str] = []
        for path in paths:
            try:
                validated = self.sandbox.validate(path)
            except SecurityError:
                continue
            safe_paths.append(validated.relative_to(self.workspace_root).as_posix())
        return safe_paths


def log_generated_files(workspace_root: Path, paths: list[str | Path]) -> Path:
    return AuditLogger(workspace_root).log(
        action_type="generated_files",
        result="success",
        affected_paths=paths,
    )


def _redact_data(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_data(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_data(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_data(item) for key, item in value.items()}
    return value
