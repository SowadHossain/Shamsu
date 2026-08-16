"""Local SQLite project memory - the authoritative SHAMSU memory store.

Runtime state lives in ``shamsu.runtime.task_state`` and code intelligence lives
under ``shamsu.artifacts``. This module stores durable project memory: facts,
decisions, task history, failure lessons, constraints, environment notes,
checkpoints, and evidence references. Graphiti can mirror this data, but it is
not required for normal operation.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from shamsu.memory.types import LongTermMemory, MemoryFreshness, MemoryRecordStatus

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "a", "an",
    "of", "to", "on", "in", "it", "is", "be", "should", "must", "will",
}


class SQLiteMemoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'unknown',
                    confidence REAL NOT NULL DEFAULT 0.85,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    last_verified_at REAL NOT NULL DEFAULT 0,
                    freshness TEXT NOT NULL DEFAULT 'UNKNOWN',
                    status TEXT NOT NULL DEFAULT 'ACTIVE'
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status, freshness)")
            self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        additions = {
            "content": "TEXT NOT NULL DEFAULT ''",
            "project_id": "TEXT NOT NULL DEFAULT ''",
            "source": "TEXT NOT NULL DEFAULT 'unknown'",
            "confidence": "REAL NOT NULL DEFAULT 0.85",
            "last_verified_at": "REAL NOT NULL DEFAULT 0",
            "freshness": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
            "status": "TEXT NOT NULL DEFAULT 'ACTIVE'",
        }
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")
        conn.execute("UPDATE memories SET content = text WHERE content = ''")

    def remember(
        self,
        text: str,
        kind: str,
        metadata: dict[str, Any] | None = None,
        *,
        project_id: str = "",
        source: str = "unknown",
        confidence: float | None = None,
        freshness: MemoryFreshness | str = MemoryFreshness.FRESH,
        status: MemoryRecordStatus | str = MemoryRecordStatus.ACTIVE,
    ) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty memory text"}
        metadata = dict(metadata or {})
        project_id = str(project_id or metadata.get("project_id") or metadata.get("workspace") or "")
        source = str(source or metadata.get("source") or "unknown")
        confidence_value = _confidence(confidence if confidence is not None else metadata.get("confidence"))
        freshness_value = _enum_value(freshness)
        status_value = _enum_value(status)
        # Dedupe on identical normalized content from the same source run.
        norm = _norm(text)
        source_run_id = str(metadata.get("source_run_id") or metadata.get("run_id") or "")
        for existing in self._all():
            existing_source = str(
                existing.metadata.get("source_run_id") or existing.metadata.get("run_id") or ""
            )
            same_source = source_run_id == existing_source or not source_run_id or not existing_source
            if (
                existing.kind == kind
                and existing.project_id == project_id
                and _norm(existing.text) == norm
                and same_source
                and _enum_value(existing.status) == MemoryRecordStatus.ACTIVE.value
            ):
                return {"ok": True, "deduped": True, "memory_id": existing.memory_id}
        if source in {"repository_evidence", "file.read", "tool_result", "evidence"}:
            self.mark_conflicting_stale(kind, text, project_id=project_id, reason=f"fresh evidence from {source}")
        memory_id = uuid.uuid4().hex[:16]
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memories (
                    id, kind, text, content, project_id, source, confidence,
                    metadata, created_at, last_verified_at, freshness, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    kind,
                    text,
                    text,
                    project_id,
                    source,
                    confidence_value,
                    json.dumps(metadata),
                    now,
                    now if freshness_value == MemoryFreshness.FRESH.value else 0.0,
                    freshness_value,
                    status_value,
                ),
            )
        return {"ok": True, "memory_id": memory_id}

    def get_relevant(
        self, query: str, task_type: str | None = None, limit: int = 8
    ) -> list[LongTermMemory]:
        rows = self._active()
        if not rows:
            return []
        query_tokens = _tokens(query)
        scored: list[tuple[float, LongTermMemory]] = []
        for memory in rows:
            score = _overlap_score(query_tokens, _tokens(memory.text))
            if task_type and memory.metadata.get("task_type") == task_type:
                score += 0.5
            if _enum_value(memory.freshness) == MemoryFreshness.FRESH.value:
                score += 0.15
            score += max(0.0, min(1.0, memory.confidence)) * 0.1
            if score > 0:
                scored.append((score, memory))
        if not scored:
            # No lexical overlap: fall back to most-recent memories so recall is
            # never empty just because the wording differs.
            return rows[:limit]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [
            LongTermMemory(kind=m.kind, text=m.text, memory_id=m.memory_id, score=s, metadata=m.metadata)
            for s, m in scored[:limit]
        ]

    def search(self, query: str, limit: int = 8) -> list[LongTermMemory]:
        return self.get_relevant(query, None, limit)

    def forget(self, memory_id_or_query: str) -> dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET status = ?, freshness = ?
                WHERE id = ?
                """,
                (MemoryRecordStatus.FORGOTTEN.value, MemoryFreshness.STALE.value, memory_id_or_query),
            )
            if cursor.rowcount:
                return {"ok": True, "deleted": cursor.rowcount}
            # Fall back to deleting by substring match on text.
            cursor = conn.execute(
                """
                UPDATE memories
                SET status = ?, freshness = ?
                WHERE text LIKE ? OR content LIKE ?
                """,
                (
                    MemoryRecordStatus.FORGOTTEN.value,
                    MemoryFreshness.STALE.value,
                    f"%{memory_id_or_query}%",
                    f"%{memory_id_or_query}%",
                ),
            )
            return {"ok": bool(cursor.rowcount), "deleted": cursor.rowcount}

    def mark_stale(self, memory_id_or_query: str, *, reason: str = "stale") -> dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET freshness = ?
                WHERE id = ? OR text LIKE ? OR content LIKE ?
                """,
                (
                    MemoryFreshness.STALE.value,
                    memory_id_or_query,
                    f"%{memory_id_or_query}%",
                    f"%{memory_id_or_query}%",
                ),
            )
            return {"ok": bool(cursor.rowcount), "updated": cursor.rowcount}

    def verify(self, memory_id: str, *, source: str = "fresh_evidence") -> dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET freshness = ?, status = ?, last_verified_at = ?, source = ?
                WHERE id = ?
                """,
                (
                    MemoryFreshness.FRESH.value,
                    MemoryRecordStatus.ACTIVE.value,
                    now,
                    source,
                    memory_id,
                ),
            )
            return {"ok": bool(cursor.rowcount), "updated": cursor.rowcount}

    def remember_evidence(
        self,
        text: str,
        kind: str = "project_fact",
        metadata: dict[str, Any] | None = None,
        *,
        project_id: str = "",
        source: str = "repository_evidence",
        confidence: float = 1.0,
    ) -> dict[str, Any]:
        return self.remember(
            text,
            kind,
            metadata,
            project_id=project_id,
            source=source,
            confidence=confidence,
            freshness=MemoryFreshness.FRESH,
            status=MemoryRecordStatus.ACTIVE,
        )

    def mark_conflicting_stale(
        self,
        kind: str,
        fresh_text: str,
        *,
        project_id: str = "",
        reason: str = "superseded by fresh evidence",
    ) -> int:
        fresh_tokens = _tokens(fresh_text)
        if not fresh_tokens:
            return 0
        updated = 0
        for memory in self._active():
            if memory.kind != kind:
                continue
            if project_id and memory.project_id and memory.project_id != project_id:
                continue
            if _norm(memory.text) == _norm(fresh_text):
                continue
            overlap = _overlap_score(fresh_tokens, _tokens(memory.text))
            if overlap < 0.35:
                continue
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    UPDATE memories
                    SET status = ?, freshness = ?
                    WHERE id = ?
                    """,
                    (
                        MemoryRecordStatus.SUPERSEDED.value,
                        MemoryFreshness.STALE.value,
                        memory.memory_id,
                    ),
                )
                updated += int(cursor.rowcount)
        return updated

    def _active(self) -> list[LongTermMemory]:
        return [
            memory
            for memory in self._all()
            if _enum_value(memory.status) == MemoryRecordStatus.ACTIVE.value
            and _enum_value(memory.freshness) != MemoryFreshness.STALE.value
        ]

    def _all(self) -> list[LongTermMemory]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    id, kind, text, content, project_id, source, confidence,
                    metadata, created_at, last_verified_at, freshness, status
                FROM memories
                ORDER BY created_at DESC
                """
            ).fetchall()
        result: list[LongTermMemory] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            result.append(
                LongTermMemory(
                    kind=row["kind"],
                    text=row["content"] or row["text"],
                    memory_id=row["id"],
                    metadata=metadata,
                    project_id=row["project_id"],
                    source=row["source"],
                    confidence=float(row["confidence"]),
                    created_at=float(row["created_at"]),
                    last_verified_at=float(row["last_verified_at"]),
                    freshness=row["freshness"],
                    status=row["status"],
                )
            )
        return result


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _tokens(text: str) -> set[str]:
    return {w for w in _norm(text).replace(".", " ").replace(",", " ").split() if w not in _STOPWORDS and len(w) > 2}


def _overlap_score(query_tokens: set[str], doc_tokens: set[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    return len(query_tokens & doc_tokens) / len(query_tokens)


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.85
    return max(0.0, min(1.0, number))
