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
from contextlib import contextmanager
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from shamsu.memory.types import LongTermMemory, MemoryFreshness, MemoryRecordStatus, NOTE_KINDS

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "a", "an",
    "of", "to", "on", "in", "it", "is", "be", "should", "must", "will",
}


class SQLiteMemoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
            self._migrate_schema(conn)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status, freshness)")
            self._migrate_legacy_notes(conn)
            self._migrate_legacy_tombstones(conn)

    def _migrate_legacy_tombstones(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS memory_tombstones (value TEXT PRIMARY KEY, normalized TEXT NOT NULL)")
        if conn.execute("SELECT 1 FROM memory_migrations WHERE name = 'tombstones-v1'").fetchone():
            return
        path = self.db_path.parent / "tombstones.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            for value in set(raw.get("ids", [])) | set(raw.get("texts", [])):
                conn.execute("INSERT OR IGNORE INTO memory_tombstones VALUES (?, ?)", (str(value), _norm(str(value))))
        conn.execute("INSERT INTO memory_migrations VALUES ('tombstones-v1')")

    def load_tombstones(self) -> dict[str, set[str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT value, normalized FROM memory_tombstones").fetchall()
        return {"ids": {row["value"] for row in rows}, "texts": {row["normalized"] for row in rows}}

    def add_tombstone(self, value: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO memory_tombstones VALUES (?, ?)", (value, _norm(value)))

    def remove_tombstone(self, value: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM memory_tombstones WHERE value = ? OR normalized = ?", (value, _norm(value)))

    def _migrate_legacy_notes(self, conn: sqlite3.Connection) -> None:
        """Import the JSON note store once, atomically, preserving old IDs/files."""
        conn.execute("CREATE TABLE IF NOT EXISTS memory_migrations (name TEXT PRIMARY KEY)")
        index = self.db_path.parent / "notes" / "index.json"
        if not index.exists():
            return
        try:
            raw = json.loads(index.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict) or not isinstance(raw.get("notes"), list):
                raise ValueError("invalid note index")
        except (ValueError, UnicodeError):
            # Preserve damaged input; never overwrite an earlier recovery copy.
            spoiled = index.with_suffix(".json.corrupt")
            if spoiled.exists():
                spoiled = index.with_suffix(f".json.{uuid.uuid4().hex}.corrupt")
            index.rename(spoiled)
            return
        if conn.execute("SELECT 1 FROM memory_migrations WHERE name = 'notes-v1'").fetchone():
            return
        for entry in raw["notes"]:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            kind = NOTE_KINDS.get(str(entry.get("type") or "context"), "project_fact")
            title, content = str(entry.get("title") or ""), str(entry.get("content") or "")
            if not content.strip():
                continue
            try:
                created = datetime.fromisoformat(str(entry.get("created_at"))).timestamp()
            except (ValueError, TypeError):
                created = time.time()
            text = f"{title}: {content}" if title else content
            conn.execute(
                """INSERT OR IGNORE INTO memories
                (id, kind, text, content, project_id, source, confidence, metadata,
                 created_at, last_verified_at, freshness, status)
                VALUES (?, ?, ?, ?, ?, 'legacy_notes', 0.85, ?, ?, 0, 'UNKNOWN', 'ACTIVE')""",
                (str(entry["id"]), kind, text, text, self.db_path.parent.parent.parent.name,
                 json.dumps({"note": entry, "migrated_from": "notes/index.json"}), created),
            )
        conn.execute("INSERT INTO memory_migrations VALUES ('notes-v1')")

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
        # The read/dedupe/update/insert operation is one write transaction.
        # Separate sessions can no longer race between lookup and commit.
        norm = _norm(text)
        source_run_id = str(metadata.get("source_run_id") or metadata.get("run_id") or "")
        note = metadata.get("note")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM memory_tombstones WHERE value = ? OR normalized = ?", (text, norm))
            for existing in self._all(conn):
                existing_source = str(existing.metadata.get("source_run_id") or existing.metadata.get("run_id") or "")
                same_source = source_run_id == existing_source or not source_run_id or not existing_source
                old_note = existing.metadata.get("note")
                same_note = (
                    isinstance(note, dict) and isinstance(old_note, dict)
                    and _norm(str(note.get("title", ""))) == _norm(str(old_note.get("title", "")))
                    and note.get("type") == old_note.get("type")
                )
                same_text = _norm(existing.text) == norm
                if (
                    existing.kind == kind and existing.project_id == project_id
                    and (same_note or same_text) and same_source
                    and _enum_value(existing.status) == MemoryRecordStatus.ACTIVE.value
                ):
                    if isinstance(note, dict):
                        if isinstance(old_note, dict):
                            note["created_at"] = old_note.get("created_at") or note.get("created_at", "")
                        conn.execute(
                            """UPDATE memories SET text = ?, content = ?, metadata = ?,
                               last_verified_at = ?, freshness = ?, confidence = ?, source = ? WHERE id = ?""",
                            (text, text, json.dumps({**existing.metadata, **metadata}), now,
                             freshness_value, confidence_value, source, existing.memory_id),
                        )
                    return {"ok": True, "deduped": same_text, "updated": not same_text,
                            "memory_id": existing.memory_id}
            if source in {"repository_evidence", "file.read", "tool_result", "evidence"}:
                self.mark_conflicting_stale(kind, text, project_id=project_id,
                                            reason=f"fresh evidence from {source}", _conn=conn)
            memory_id = uuid.uuid4().hex[:16]
            conn.execute(
                """INSERT INTO memories
                (id, kind, text, content, project_id, source, confidence, metadata,
                 created_at, last_verified_at, freshness, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (memory_id, kind, text, text, project_id, source, confidence_value,
                 json.dumps(metadata), now,
                 now if freshness_value == MemoryFreshness.FRESH.value else 0.0,
                 freshness_value, status_value),
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
            replace(m, score=s)
            for s, m in scored[:limit]
        ]

    def search(self, query: str, limit: int = 8) -> list[LongTermMemory]:
        return self.get_relevant(query, None, limit)

    def forget(self, memory_id_or_query: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO memory_tombstones VALUES (?, ?)",
                         (memory_id_or_query, _norm(memory_id_or_query)))
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
        _conn: sqlite3.Connection | None = None,
    ) -> int:
        if _conn is None:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                return self.mark_conflicting_stale(kind, fresh_text, project_id=project_id,
                                                   reason=reason, _conn=conn)
        fresh_tokens = _tokens(fresh_text)
        if not fresh_tokens:
            return 0
        updated = 0
        for memory in self._all(_conn):
            if memory.kind != kind or _enum_value(memory.status) != MemoryRecordStatus.ACTIVE.value:
                continue
            if project_id and memory.project_id and memory.project_id != project_id:
                continue
            if _norm(memory.text) == _norm(fresh_text):
                continue
            if _overlap_score(fresh_tokens, _tokens(memory.text)) < 0.35:
                continue
            cursor = _conn.execute(
                "UPDATE memories SET status = ?, freshness = ? WHERE id = ?",
                (MemoryRecordStatus.SUPERSEDED.value, MemoryFreshness.STALE.value, memory.memory_id),
            )
            updated += int(cursor.rowcount)
        return updated

    def list_active(self) -> list[LongTermMemory]:
        return self._active()

    def touch_notes(self, memory_ids: list[str]) -> None:
        """Update only recalled notes, without rewriting JSON/Markdown mirrors."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for memory_id in memory_ids:
                row = conn.execute("SELECT metadata FROM memories WHERE id = ? AND status = 'ACTIVE'",
                                   (memory_id,)).fetchone()
                if row is None:
                    continue
                metadata = json.loads(row["metadata"])
                note = metadata.get("note")
                if isinstance(note, dict):
                    stamp = datetime.now().astimezone().isoformat()
                    note["last_used_at"] = stamp
                    note["updated_at"] = stamp
                    note["tier"] = "hot"
                    conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata), memory_id))

    def update_note_metadata(self, memory_id: str, note: dict[str, Any], expected_updated_at: str) -> bool:
        """Update note usage/tier only if a concurrent writer has not changed it."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT metadata FROM memories WHERE id = ? AND status = 'ACTIVE'",
                               (memory_id,)).fetchone()
            if row is None:
                return False
            metadata = json.loads(row["metadata"])
            previous = metadata.get("note")
            if not isinstance(previous, dict) or str(previous.get("updated_at") or "") != expected_updated_at:
                return False
            # This API cannot alter the fact itself, only its retrieval metadata.
            for key in ("tier", "last_used_at", "updated_at"):
                if key in note:
                    previous[key] = note[key]
            metadata["note"] = previous
            conn.execute("UPDATE memories SET metadata = ? WHERE id = ?", (json.dumps(metadata), memory_id))
            return True

    def _active(self) -> list[LongTermMemory]:
        return [
            memory
            for memory in self._all()
            if _enum_value(memory.status) == MemoryRecordStatus.ACTIVE.value
            and _enum_value(memory.freshness) != MemoryFreshness.STALE.value
        ]

    def _all(self, conn: sqlite3.Connection | None = None) -> list[LongTermMemory]:
        if conn is None:
            with self._connect() as connection:
                return self._all(connection)
        rows = conn.execute(
            """SELECT id, kind, text, content, project_id, source, confidence,
               metadata, created_at, last_verified_at, freshness, status
               FROM memories ORDER BY created_at DESC"""
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
