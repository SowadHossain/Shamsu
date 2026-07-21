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
from shamsu.indexer.policy import workspace_manifest
from shamsu.tools.codebase_memory import CodebaseMemoryAdapter

REQUIRED_TOOL_MESSAGE = (
    "Codebase-Memory MCP is not available. SHAMSU will continue in degraded "
    "local-retrieval mode with exact file tools and semantic search when available.\n\n"
    "Run:\n"
    "  /abstract setup\n\n"
    "or:\n"
    "  shamsu doctor\n\n"
    "Structural graph search will remain unavailable until local code memory is ready."
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
        return workspace_manifest(self.workspace)

    def index_status(self) -> IndexStatus:
        last = self._read_json(self._last_index_path())
        generation = int(last.get("workspace_generation", 0))
        indexed_generation = int(last.get("indexed_generation", 0))
        if not last.get("indexed"):
            return IndexStatus(
                exists=False,
                stale=True,
                message="Code memory: no index yet.",
                policy_version=int(last.get("policy_version", 0)),
                workspace_generation=generation,
                indexed_generation=indexed_generation,
            )
        if last.get("forced_stale"):
            return IndexStatus(
                exists=True,
                stale=True,
                message="Code memory: refreshing needed after a workspace mutation.",
                manifest_hash=str(last.get("manifest_hash", "")),
                policy_version=int(last.get("policy_version", 0)),
                workspace_generation=generation,
                indexed_generation=indexed_generation,
            )
        current = self._snapshot()
        stale = (
            current["manifest_hash"] != last.get("manifest_hash")
            or current["policy_version"] != last.get("policy_version")
            or generation != indexed_generation
        )
        message = "Code memory: refreshing needed." if stale else "Code memory: ready."
        return IndexStatus(
            exists=True,
            stale=stale,
            message=message,
            manifest_hash=str(last.get("manifest_hash", "")),
            policy_version=int(last.get("policy_version", 0)),
            workspace_generation=generation,
            indexed_generation=indexed_generation,
        )

    def _mark_indexed(self) -> None:
        previous = self._read_json(self._last_index_path())
        generation = int(previous.get("workspace_generation", 0))
        payload = {
            **self._snapshot(),
            "indexed": True,
            "forced_stale": False,
            "workspace_generation": generation,
            "indexed_generation": generation,
            "indexed_at": time.time(),
        }
        self._write_json(self._last_index_path(), payload)

    def mark_stale(self) -> None:
        """Mark code memory stale after a successful write/patch. Idempotent -
        repeated calls within one task still trigger only one refresh, at the
        next `ensure_ready()` call (a natural debounce)."""
        last = self._read_json(self._last_index_path())
        if not last.get("forced_stale"):
            last["workspace_generation"] = int(last.get("workspace_generation", 0)) + 1
        last["forced_stale"] = True
        last.setdefault("indexed", last.get("indexed", False))
        self._write_json(self._last_index_path(), last)
        status = self._read_json(self._status_path())
        if status:
            status["index"] = {
                **dict(status.get("index") or {}),
                "stale": True,
                "message": "Code memory: refreshing needed after a workspace mutation.",
                "workspace_generation": int(last.get("workspace_generation", 0)),
                "indexed_generation": int(last.get("indexed_generation", 0)),
            }
            self._write_json(self._status_path(), status)
        self._log_event(
            "mark_stale",
            {"workspace_generation": int(last.get("workspace_generation", 0))},
        )

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
            normal_mode_allowed=True,
            degraded=not health.ok or index.stale,
            retrieval_mode="external" if health.ok and not index.stale else "local",
        )
        self._write_json(self._status_path(), result.to_dict())
        return result

    def index_metadata(self) -> dict[str, Any]:
        index = self.index_status()
        return {
            "exists": index.exists,
            "stale": index.stale,
            "manifest_hash": index.manifest_hash,
            "policy_version": index.policy_version,
            "workspace_generation": index.workspace_generation,
            "indexed_generation": index.indexed_generation,
        }

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

    def build(self) -> dict[str, Any]:
        result = self.adapter.index_workspace(self.workspace)
        self._log_event("index", result)
        if result.get("ok", False):
            self._mark_indexed()
            self.status()
        return result

    def refresh(self) -> dict[str, Any]:
        result = self.adapter.refresh_workspace(self.workspace)
        self._log_event("refresh", result)
        if result.get("ok", False):
            self._mark_indexed()
            self.status()
        return result

    # -- auto build/refresh + gate --------------------------------------------

    def ensure_ready(self, auto_build: bool = True) -> GateResult:
        """The startup/pre-workflow health gate.

        Builds or refreshes the external index when available. If the external
        tool is unavailable, local file tools and semantic retrieval continue
        in an explicit degraded mode.
        """
        health = self.adapter.healthcheck(self.workspace)
        if not health.ok:
            status = self.status()
            return GateResult(allowed=True, reason=REQUIRED_TOOL_MESSAGE, status=status)

        if auto_build:
            index = self.index_status()
            if not index.exists:
                result = self.build()
            elif index.stale:
                result = self.refresh()
            else:
                result = {"ok": True}
            if not result.get("ok", False):
                reason = (
                    "Codebase-Memory refresh failed; SHAMSU is continuing with "
                    "local retrieval. Run /abstract repair for structural search."
                )
                return GateResult(allowed=True, reason=reason, status=self.status())

        return GateResult(allowed=True, status=self.status())
