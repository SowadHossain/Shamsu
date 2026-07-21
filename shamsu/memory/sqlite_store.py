"""Local SQLite long-term memory - the always-available fallback for Graphiti.

Graphiti is a heavy, optional backend. Requiring it to start blocks SHAMSU on
the exact low-resource machines it targets. This store gives every workspace a
working long-term memory out of the box (stdlib sqlite3, no server, no extra
dependency), so remember/recall function even when Graphiti is absent. When
Graphiti is later set up, MemoryService prefers it; this stays as the floor.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from shamsu.memory.types import LongTermMemory

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
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)")

    def remember(
        self, text: str, kind: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "error": "empty memory text"}
        metadata = dict(metadata or {})
        # Dedupe on identical normalized content from the same source run.
        norm = _norm(text)
        source_run_id = str(metadata.get("source_run_id") or metadata.get("run_id") or "")
        for existing in self._all():
            existing_source = str(
                existing.metadata.get("source_run_id") or existing.metadata.get("run_id") or ""
            )
            same_source = source_run_id == existing_source or not source_run_id or not existing_source
            if existing.kind == kind and _norm(existing.text) == norm and same_source:
                return {"ok": True, "deduped": True, "memory_id": existing.memory_id}
        memory_id = uuid.uuid4().hex[:16]
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO memories (id, kind, text, metadata, created_at) VALUES (?, ?, ?, ?, ?)",
                (memory_id, kind, text, json.dumps(metadata), time.time()),
            )
        return {"ok": True, "memory_id": memory_id}

    def get_relevant(
        self, query: str, task_type: str | None = None, limit: int = 8
    ) -> list[LongTermMemory]:
        rows = self._all()
        if not rows:
            return []
        query_tokens = _tokens(query)
        scored: list[tuple[float, LongTermMemory]] = []
        for memory in rows:
            score = _overlap_score(query_tokens, _tokens(memory.text))
            if task_type and memory.metadata.get("task_type") == task_type:
                score += 0.5
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
            cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id_or_query,))
            if cursor.rowcount:
                return {"ok": True, "deleted": cursor.rowcount}
            # Fall back to deleting by substring match on text.
            cursor = conn.execute(
                "DELETE FROM memories WHERE text LIKE ?", (f"%{memory_id_or_query}%",)
            )
            return {"ok": bool(cursor.rowcount), "deleted": cursor.rowcount}

    def _all(self) -> list[LongTermMemory]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, kind, text, metadata, created_at FROM memories ORDER BY created_at DESC"
            ).fetchall()
        result: list[LongTermMemory] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            result.append(
                LongTermMemory(kind=row["kind"], text=row["text"], memory_id=row["id"], metadata=metadata)
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
