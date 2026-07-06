"""
Workspace-level orchestration around the Codebase-Memory MCP adapter.

Owns the `.shamsu/abstract/` metadata files, the startup health gate, and
auto build/refresh bookkeeping. Contains no parsing/graph logic of its own -
all structural facts come from `CodebaseMemoryAdapter`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from shamsu.abstract.types import AbstractStatus, GateResult, IndexStatus
from shamsu.indexer.walker import FileWalker
from shamsu.tools.codebase_memory import CodebaseMemoryAdapter

REQUIRED_TOOL_MESSAGE = (
    "Codebase-Memory MCP is required for SHAMSU codebase mode but is not available.\n\n"
    "Run:\n"
    "  /abstract setup\n\n"
    "or:\n"
    "  shamsu doctor\n\n"
    "SHAMSU will not run normal code-agent workflows in this workspace until "
    "local code memory is ready."
)


class AbstractService:
    def __init__(self, workspace: Path, adapter: CodebaseMemoryAdapter | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.adapter = adapter or CodebaseMemoryAdapter()
        self.abstract_dir = self.workspace / ".shamsu" / "abstract"

    # -- paths --------------------------------------------------------------

    def _status_path(self) -> Path:
        return self.abstract_dir / "status.json"

    def _config_path(self) -> Path:
        return self.abstract_dir / "config.json"

    def _last_index_path(self) -> Path:
        return self.abstract_dir / "last-index.json"

    def _events_path(self) -> Path:
        return self.abstract_dir / "code-memory-events.jsonl"

    # -- json helpers ---------------------------------------------------------

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self.abstract_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _log_event(self, event: str, detail: dict[str, Any]) -> None:
        self.abstract_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"event": event, "ts": time.time(), **detail})
        with self._events_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # -- snapshot/staleness -----------------------------------------------------

    def _snapshot(self) -> dict[str, Any]:
        files = FileWalker(self.workspace).discover()
        max_mtime = max((path.stat().st_mtime for path in files), default=0.0)
        return {"file_count": len(files), "max_mtime": max_mtime}

    def index_status(self) -> IndexStatus:
        last = self._read_json(self._last_index_path())
        if not last.get("indexed"):
            return IndexStatus(exists=False, stale=True, message="Code memory: no index yet.")
        if last.get("forced_stale"):
            return IndexStatus(exists=True, stale=True, message="Code memory: refreshing needed.")
        current = self._snapshot()
        stale = (
            current["file_count"] != last.get("file_count")
            or current["max_mtime"] > last.get("max_mtime", 0.0)
        )
        message = "Code memory: refreshing needed." if stale else "Code memory: ready."
        return IndexStatus(exists=True, stale=stale, message=message)

    def _mark_indexed(self) -> None:
        payload = {**self._snapshot(), "indexed": True, "forced_stale": False}
        self._write_json(self._last_index_path(), payload)

    def mark_stale(self) -> None:
        """Mark code memory stale after a successful write/patch. Idempotent -
        repeated calls within one task still trigger only one refresh, at the
        next `ensure_ready()` call (a natural debounce)."""
        last = self._read_json(self._last_index_path())
        last["forced_stale"] = True
        last.setdefault("indexed", last.get("indexed", False))
        self._write_json(self._last_index_path(), last)
        self._log_event("mark_stale", {})

    def queue_refresh(self) -> None:
        self.mark_stale()

    # -- health/status ------------------------------------------------------

    def status(self) -> AbstractStatus:
        health = self.adapter.healthcheck(self.workspace)
        index = self.index_status()
        result = AbstractStatus(
            workspace=str(self.workspace),
            health=health,
            index=index,
            normal_mode_allowed=health.ok,
        )
        self._write_json(self._status_path(), result.to_dict())
        return result

    def setup(self) -> dict[str, Any]:
        result = self.adapter.setup(self.workspace)
        self._write_json(self._config_path(), {"setup_result": result})
        self._log_event("setup", result)
        return result

    def repair(self) -> dict[str, Any]:
        result = self.adapter.repair(self.workspace)
        self._log_event("repair", result)
        if result.get("ok"):
            self.ensure_ready()
        return result

    # -- auto build/refresh + gate --------------------------------------------

    def ensure_ready(self, auto_build: bool = True) -> GateResult:
        """The startup/pre-workflow health gate.

        Blocks (allowed=False) when Codebase-Memory MCP is unavailable.
        Otherwise builds the index if missing, refreshes it if stale, and
        allows normal code-agent workflows to proceed.
        """
        health = self.adapter.healthcheck(self.workspace)
        if not health.ok:
            status = AbstractStatus(
                workspace=str(self.workspace),
                health=health,
                index=self.index_status(),
                normal_mode_allowed=False,
            )
            return GateResult(allowed=False, reason=REQUIRED_TOOL_MESSAGE, status=status)

        if auto_build:
            index = self.index_status()
            if not index.exists:
                result = self.adapter.index_workspace(self.workspace)
                self._log_event("index", result)
                if result.get("ok", True):
                    self._mark_indexed()
            elif index.stale:
                result = self.adapter.refresh_workspace(self.workspace)
                self._log_event("refresh", result)
                if result.get("ok", True):
                    self._mark_indexed()

        return GateResult(allowed=True, status=self.status())
