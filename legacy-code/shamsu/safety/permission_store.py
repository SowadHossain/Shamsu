"""Remembers per-workspace 'always allow' approval decisions.

Session-scoped choices live only in memory and vanish when the process
exits. Workspace-scoped choices persist to `.shamsu/permissions.json` so
they survive across restarts, the same way session/index state does.
"""
from __future__ import annotations

import json
from pathlib import Path

from shamsu.safety.sandbox import Sandbox

PERMISSIONS_FILENAME = "permissions.json"
PERMISSIONS_SCHEMA_VERSION = 2


class PermissionMemory:
    def __init__(self, workspace_root: Path):
        self.workspace_root = Path(workspace_root).resolve()
        self._sandbox = Sandbox(self.workspace_root)
        self._session_remembered: set[str] = set()
        self._workspace_remembered: set[str] = self._load()

    def is_remembered(self, action_type: str) -> bool:
        return action_type in self._session_remembered or action_type in self._workspace_remembered

    def remember(self, action_type: str, scope: str) -> None:
        if scope == "session":
            self._session_remembered.add(action_type)
        elif scope == "workspace":
            self._workspace_remembered.add(action_type)
            self._save()

    def forget_all(self) -> None:
        self._session_remembered.clear()
        self._workspace_remembered.clear()
        self._save()

    def list_remembered(self) -> dict[str, str]:
        result = {action: "workspace" for action in self._workspace_remembered}
        for action in self._session_remembered:
            result.setdefault(action, "session")
        return result

    def _path(self) -> Path:
        return self._sandbox.validate(Path(".shamsu") / PERMISSIONS_FILENAME)

    def _load(self) -> set[str]:
        path = self._path()
        if not path.exists():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        remembered = data.get("always_allow", []) if isinstance(data, dict) else []
        return {str(action) for action in remembered if isinstance(action, str)}

    def _save(self) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": PERMISSIONS_SCHEMA_VERSION,
                    "always_allow": sorted(self._workspace_remembered),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
