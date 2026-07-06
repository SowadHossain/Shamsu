"""Workspace service for required Graphiti long-term memory."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from shamsu.memory.graphiti_adapter import GraphitiAdapter
from shamsu.memory.policy import MemoryPolicy
from shamsu.memory.types import GraphitiHealth, LongTermMemory, MemoryGate, MemoryKind, MemoryStatus

REQUIRED_MEMORY_MESSAGE = (
    "Graphiti memory backend is required but not available.\n\n"
    "Run: /memory setup\n\n"
    "or: shamsu doctor\n\n"
    "SHAMSU will not start normal agent mode until local Graphiti memory is ready."
)


class MemoryService:
    def __init__(
        self,
        workspace: Path,
        adapter: GraphitiAdapter | None = None,
        policy: MemoryPolicy | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.adapter = adapter or GraphitiAdapter()
        self.policy = policy or MemoryPolicy()
        self.memory_dir = self.workspace / ".shamsu" / "memory"

    def _status_path(self) -> Path:
        return self.memory_dir / "status.json"

    def _config_path(self) -> Path:
        return self.memory_dir / "config.json"

    def _last_sync_path(self) -> Path:
        return self.memory_dir / "last-sync.json"

    def _events_path(self) -> Path:
        return self.memory_dir / "memory-events.jsonl"

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _log_event(self, event: str, detail: dict[str, Any]) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"event": event, "ts": time.time(), **detail}, ensure_ascii=True)
        with self._events_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def is_available(self) -> bool:
        return self.healthcheck().ok

    def healthcheck(self) -> GraphitiHealth:
        return self.adapter.healthcheck(self.workspace)

    def status(self) -> MemoryStatus:
        health = self.healthcheck()
        status = MemoryStatus(
            workspace=str(self.workspace),
            health=health,
            memory_path=str(self.memory_dir),
            normal_mode_allowed=health.ok,
        )
        self._write_json(self._status_path(), status.to_dict())
        return status

    def ensure_ready(self) -> MemoryGate:
        status = self.status()
        if not status.health.ok:
            return MemoryGate(False, REQUIRED_MEMORY_MESSAGE, status)
        return MemoryGate(True, status=status)

    def setup(self) -> dict[str, Any]:
        result = self.adapter.setup(self.workspace)
        self._log_event("setup", result)
        self.status()
        return result

    def repair(self) -> dict[str, Any]:
        result = self.adapter.repair(self.workspace)
        self._log_event("repair", result)
        self.status()
        return result

    def add_episode(self, text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.adapter.add_episode(self.workspace, text, metadata)
        self._log_event("add_episode", {"ok": bool(result.get("ok")), "error": result.get("error", "")})
        return result

    def remember(self, text: str, kind: MemoryKind | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        decision = self.policy.decide(text, kind, metadata)
        if not decision.should_store:
            result = {"ok": False, "skipped": True, "reason": decision.reason}
            self._log_event("remember_skipped", result)
            return result
        existing = self.get_relevant(decision.text, limit=5)
        if any(memory.kind == decision.kind and _norm(memory.text) == _norm(decision.text) for memory in existing):
            result = {"ok": True, "deduped": True, "message": "memory already exists"}
            self._log_event("remember_deduped", result)
            return result
        result = self.adapter.remember(self.workspace, decision.text, decision.kind, metadata)
        self._log_event("remember", {"ok": bool(result.get("ok")), "kind": decision.kind, "error": result.get("error", "")})
        if result.get("ok"):
            self._write_json(self._last_sync_path(), {"ts": time.time(), "kind": decision.kind})
        return result

    def search(self, query: str, limit: int = 8, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.adapter.search(self.workspace, query, limit, filters)
        self._log_event("search", {"ok": bool(result.get("ok")), "limit": limit, "error": result.get("error", "")})
        return result

    def get_relevant(self, user_prompt: str, task_type: str | None = None, limit: int = 8) -> list[LongTermMemory]:
        gate = self.ensure_ready()
        if not gate.allowed:
            return []
        memories = self.adapter.get_relevant(self.workspace, user_prompt, task_type, limit)
        return _dedupe_memories(memories)[:limit]

    def render_relevant(self, user_prompt: str, task_type: str | None = None, limit: int = 8) -> str:
        memories = self.get_relevant(user_prompt, task_type, limit)
        if not memories:
            return ""
        lines = ["Relevant long-term memory:"]
        for memory in memories:
            lines.append(f"[{memory.kind}] {memory.text}")
        return "\n".join(lines)

    def forget(self, memory_id_or_query: str) -> dict[str, Any]:
        result = self.adapter.forget(self.workspace, memory_id_or_query)
        self._log_event("forget", {"ok": bool(result.get("ok")), "query": memory_id_or_query, "error": result.get("error", "")})
        return result

    def summarize_session(self, session_id: str) -> dict[str, Any]:
        session_events = self.workspace / ".shamsu" / "sessions" / session_id / "events.jsonl"
        if not session_events.exists():
            return {"ok": False, "error": f"Session not found: {session_id}"}
        summaries: list[str] = []
        for line in session_events.read_text(encoding="utf-8").splitlines()[-80:]:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event_type") in {"workflow.finished", "assistant.message"}:
                summary = str(event.get("summary") or "").strip()
                if summary:
                    summaries.append(summary)
        if not summaries:
            return {"ok": False, "error": "No durable session summary candidates found."}
        text = f"Task summary for session {session_id}: " + " ".join(summaries[-5:])
        return self.remember(text, "task_summary", {"session_id": session_id})


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _dedupe_memories(memories: list[LongTermMemory]) -> list[LongTermMemory]:
    seen: set[tuple[str, str]] = set()
    result: list[LongTermMemory] = []
    for memory in memories:
        key = (memory.kind, _norm(memory.text))
        if key in seen:
            continue
        seen.add(key)
        result.append(memory)
    return result
