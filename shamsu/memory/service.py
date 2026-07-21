"""Workspace service for required Graphiti long-term memory."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from shamsu.memory.graphiti_adapter import GraphitiAdapter
from shamsu.memory.policy import MemoryPolicy
from shamsu.memory.sqlite_store import SQLiteMemoryStore
from shamsu.memory.types import GraphitiHealth, LongTermMemory, MemoryGate, MemoryKind, MemoryStatus

REQUIRED_MEMORY_MESSAGE = (
    "Graphiti memory backend is required but not available.\n\n"
    "Run: /memory setup\n\n"
    "or: shamsu doctor\n\n"
    "SHAMSU will not start normal agent mode until local Graphiti memory is ready."
)

DEGRADED_MEMORY_MESSAGE = (
    "Graphiti is not available; using local SQLite memory (degraded). Long-term "
    "recall still works. Run `/memory setup` to enable the richer Graphiti backend."
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
        self._fallback: SQLiteMemoryStore | None = None

    @property
    def fallback(self) -> SQLiteMemoryStore:
        """Lazily-created local SQLite store, used whenever Graphiti is down."""
        if self._fallback is None:
            self._fallback = SQLiteMemoryStore(self.memory_dir / "memory.db")
        return self._fallback

    def _use_fallback(self) -> bool:
        return not self.healthcheck().ok

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

    # -- tombstones (forget/exclude-from-recall) -----------------------------

    def _tombstones_path(self) -> Path:
        return self.memory_dir / "tombstones.json"

    def _load_tombstones(self) -> dict[str, set[str]]:
        try:
            data = json.loads(self._tombstones_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return {
            "ids": {str(x) for x in data.get("ids", []) if str(x).strip()},
            "texts": {str(x) for x in data.get("texts", []) if str(x).strip()},
        }

    def _save_tombstones(self, data: dict[str, set[str]]) -> None:
        self._write_json(
            self._tombstones_path(),
            {"ids": sorted(data["ids"]), "texts": sorted(data["texts"])},
        )

    def _add_tombstone(self, value: str) -> None:
        value = str(value or "").strip()
        if not value:
            return
        data = self._load_tombstones()
        data["ids"].add(value)          # exact id/text match
        data["texts"].add(_norm(value))  # normalized text/phrase match
        self._save_tombstones(data)

    def _remove_tombstone(self, value: str) -> None:
        value = str(value or "").strip()
        if not value:
            return
        data = self._load_tombstones()
        if value in data["ids"] or _norm(value) in data["texts"]:
            data["ids"].discard(value)
            data["texts"].discard(_norm(value))
            self._save_tombstones(data)

    def _is_tombstoned(self, memory: LongTermMemory, tombstones: dict[str, set[str]] | None = None) -> bool:
        data = tombstones if tombstones is not None else self._load_tombstones()
        if not data["ids"] and not data["texts"]:
            return False
        memory_id = str(getattr(memory, "memory_id", "") or "")
        if memory_id and memory_id in data["ids"]:
            return True
        text = _norm(getattr(memory, "text", "") or "")
        if not text:
            return False
        if text in data["texts"]:
            return True
        # A distinctive forgotten phrase excludes any memory that contains it
        # (min length guards against a short token matching everything).
        return any(len(term) >= 8 and term in text for term in data["texts"])

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
            normal_mode_allowed=True,
            local_available=True,
            degraded=not health.ok,
            storage_mode="local+graphiti" if health.ok else "local",
        )
        self._write_json(self._status_path(), status.to_dict())
        return status

    def ensure_ready(self) -> MemoryGate:
        status = self.status()
        if not status.health.ok:
            return MemoryGate(False, REQUIRED_MEMORY_MESSAGE, status)
        return MemoryGate(True, status=status)

    def ensure_ready_degraded(self) -> MemoryGate:
        """Non-blocking readiness: allowed via Graphiti when healthy, otherwise
        allowed via the always-available local SQLite store (degraded). This is
        what lets SHAMSU run without Graphiti instead of hard-blocking startup."""
        status = self.status()
        if status.health.ok:
            return MemoryGate(True, status=status)
        return MemoryGate(True, DEGRADED_MEMORY_MESSAGE, status)

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

    def ensure_backend_started(self) -> dict[str, Any] | None:
        """Start this session's local FalkorDB container if memory is already
        configured but the backend isn't running yet. Returns None (no-op) when
        memory has not been set up, so we never auto-provision for users who
        haven't run `/memory setup`. Best-effort and idempotent — safe to call
        on every session start. SHAMSU never auto-stops this container on its
        own (see `shamsu.runtime.ollama.shutdown_if_last_session`) - once
        started it stays running until stopped manually via `docker stop`."""
        if not self.adapter.config_path(self.workspace).exists():
            return None
        result = self.adapter.ensure_backend_running(self.workspace)
        if result is not None:
            self._log_event("backend_start", {"ok": bool(result.get("ok")), "error": result.get("error", "")})
        return result

    def add_episode(self, text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        result = self.adapter.add_episode(self.workspace, text, metadata)
        self._log_event("add_episode", {"ok": bool(result.get("ok")), "error": result.get("error", "")})
        return result

    def remember_local(
        self,
        text: str,
        kind: MemoryKind | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Policy-check and commit to the always-available local store."""
        decision = self.policy.decide(text, kind, metadata)
        if not decision.should_store:
            result = {"ok": False, "skipped": True, "reason": decision.reason}
            self._log_event("remember_skipped", result)
            return result
        full_metadata = _memory_metadata(metadata)
        # Deliberately storing a fact un-forgets it (clears a prior tombstone) so
        # a wrong-then-corrected fact can come back.
        self._remove_tombstone(decision.text)
        result = self.fallback.remember(decision.text, decision.kind, full_metadata)
        self._log_event(
            "remember_local",
            {
                "ok": bool(result.get("ok")),
                "kind": decision.kind,
                "deduped": bool(result.get("deduped")),
                "source_run_id": full_metadata.get("source_run_id", ""),
            },
        )
        return {
            **result,
            "text": decision.text,
            "kind": decision.kind,
            "metadata": full_metadata,
        }

    def mirror_to_graphiti(
        self,
        text: str,
        kind: MemoryKind | str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Best-effort external mirror; local persistence must happen first."""
        if self._is_tombstoned(LongTermMemory(kind=kind, text=text)):  # type: ignore[arg-type]
            result = {"ok": False, "skipped": True, "reason": "tombstoned"}
            self._log_event("mirror_skipped", result)
            return result
        try:
            health = self.healthcheck()
        except Exception as exc:
            result = {"ok": False, "error": f"healthcheck failed: {exc}"}
            self._log_event("mirror_failed", {"kind": kind, "error": result["error"]})
            return result
        if not health.ok:
            result = {"ok": False, "skipped": True, "reason": health.message or "Graphiti unavailable"}
            self._log_event("mirror_unavailable", {"kind": kind, "reason": result["reason"]})
            return result
        try:
            existing = self.adapter.get_relevant(self.workspace, text, None, 8)
        except Exception as exc:
            result = {"ok": False, "error": f"dedup lookup failed: {exc}"}
            self._log_event("mirror_failed", {"kind": kind, "error": result["error"]})
            return result
        source_run_id = str((metadata or {}).get("source_run_id") or (metadata or {}).get("run_id") or "")
        if any(memory.kind == kind and _norm(memory.text) == _norm(text) for memory in existing):
            result = {"ok": True, "deduped": True, "message": "memory already exists"}
            self._log_event("mirror_deduped", {**result, "source_run_id": source_run_id})
            return result
        try:
            result = self.adapter.remember(self.workspace, text, kind, metadata)
        except Exception as exc:
            result = {"ok": False, "error": f"mirror write failed: {exc}"}
        self._log_event("mirror", {"ok": bool(result.get("ok")), "kind": kind, "error": result.get("error", "")})
        if result.get("ok"):
            self._write_json(self._last_sync_path(), {"ts": time.time(), "kind": kind, "source_run_id": source_run_id})
        return result

    def remember(self, text: str, kind: MemoryKind | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Synchronous compatibility API: local commit, then Graphiti mirror."""
        local = self.remember_local(text, kind, metadata)
        if not local.get("ok"):
            return local
        mirror = self.mirror_to_graphiti(
            str(local.get("text") or text),
            str(local.get("kind") or kind or "task_summary"),
            dict(local.get("metadata") or metadata or {}),
        )
        return {
            **local,
            "ok": True,
            "local": True,
            "mirror": mirror,
            "degraded": not bool(mirror.get("ok")),
        }

    def search(self, query: str, limit: int = 8, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        local = self.fallback.search(query, limit)
        tombstones = self._load_tombstones()
        local = [item for item in local if not self._is_tombstoned(item, tombstones)]
        if self._use_fallback():
            return {"ok": True, "degraded": True, "results": [m.text for m in local]}
        result = self.adapter.search(self.workspace, query, limit, filters)
        self._log_event("search", {"ok": bool(result.get("ok")), "limit": limit, "error": result.get("error", "")})
        external = list(result.get("results") or [])
        local_rows = [
            {"id": item.memory_id, "text": item.text, "kind": item.kind, "metadata": item.metadata}
            for item in local
        ]
        result["results"] = [
            item
            for item in _dedupe_search_results(local_rows + external)
            if not _search_result_tombstoned(self, item, tombstones)
        ][:limit]
        result["local_included"] = True
        return result

    def get_relevant(self, user_prompt: str, task_type: str | None = None, limit: int = 8) -> list[LongTermMemory]:
        tombstones = self._load_tombstones()
        # Over-fetch a little when tombstones exist so filtering forgotten facts
        # doesn't shrink the result below `limit`.
        extra = len(tombstones["ids"]) + len(tombstones["texts"])
        fetch = min(limit + extra, limit + 50) if extra else limit
        memories = self.fallback.get_relevant(user_prompt, task_type, fetch)
        if not self._use_fallback():
            try:
                memories.extend(self.adapter.get_relevant(self.workspace, user_prompt, task_type, fetch))
            except Exception as exc:
                self._log_event("recall_mirror_failed", {"error": str(exc)})
        memories = [memory for memory in memories if not self._is_tombstoned(memory, tombstones)]
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
        """Evict a wrong fact so it is never recalled again.

        SHAMSU owns a tombstone list (`.shamsu/memory/tombstones.json`) that is
        applied on every recall, so forgetting works even when the backend can't
        hard-delete (Graphiti's adapter doesn't expose deletion). We also attempt
        a best-effort backend delete; either way the fact is excluded from recall.
        """
        value = str(memory_id_or_query or "").strip()
        if not value:
            return {"ok": False, "error": "forget needs a memory id or a query text."}
        self._add_tombstone(value)
        local = self.fallback.forget(value)
        if self._use_fallback():
            backend = {"ok": False, "error": "Graphiti unavailable"}
        else:
            try:
                backend = self.adapter.forget(self.workspace, value)
            except Exception as exc:
                backend = {"ok": False, "error": str(exc)}
        backend_ok = bool(backend.get("ok"))
        self._log_event(
            "forget",
            {"ok": True, "query": value, "backend_deleted": backend_ok, "backend_error": backend.get("error", "")},
        )
        message = "Removed from recall (tombstoned)."
        message += (
            " Also hard-deleted from the backend store."
            if backend_ok
            else " The backend copy (if any) will be filtered out of every future recall."
        )
        # Preserve backend fields (e.g. an id echo) but always report ok=True: the
        # fact will not be recalled again regardless of backend deletion support.
        return {
            **backend,
            "ok": True,
            "tombstoned": True,
            "local_deleted": int(local.get("deleted", 0)),
            "backend_deleted": backend_ok,
            "message": message,
        }

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

    def log_queue_event(self, event: str, detail: dict[str, Any]) -> None:
        self._log_event(event, detail)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _memory_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    result = dict(metadata or {})
    source_run_id = str(result.get("source_run_id") or result.get("run_id") or "")
    result["source_run_id"] = source_run_id
    try:
        confidence = float(result.get("confidence", 1.0 if result.get("explicit") else 0.85))
    except (TypeError, ValueError):
        confidence = 0.85
    result["confidence"] = max(0.0, min(1.0, confidence))
    return result


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


def _dedupe_search_results(results: list[Any]) -> list[Any]:
    seen: set[str] = set()
    output: list[Any] = []
    for item in results:
        text = str(item.get("text", "")) if isinstance(item, dict) else str(item)
        key = _norm(text)
        if not key or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _search_result_tombstoned(
    service: MemoryService,
    item: Any,
    tombstones: dict[str, set[str]],
) -> bool:
    if isinstance(item, dict):
        memory = LongTermMemory(
            kind=item.get("kind", "task_summary"),
            text=str(item.get("text") or item.get("fact") or ""),
            memory_id=str(item.get("id") or item.get("memory_id") or ""),
        )
    else:
        memory = LongTermMemory(kind="task_summary", text=str(item))
    return service._is_tombstoned(memory, tombstones)
