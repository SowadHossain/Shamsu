"""
shamsu/storage/schema.py — Dev A owns this file.

SQLite schema. WAL mode for concurrent reads. FTS5 virtual table gives
free full-text search with BM25 ranking built in — no extra dependency,
no extra RAM (see ENGINEERING_HARNESS.md §3).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,
    language      TEXT,
    size          INTEGER,
    hash          TEXT,
    last_modified REAL,
    summary       TEXT
);

CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER REFERENCES files(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT,             -- function | class | method | import | variable
    line_start  INTEGER,
    line_end    INTEGER,
    signature   TEXT,
    docstring   TEXT
);

CREATE TABLE IF NOT EXISTS snippets (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER REFERENCES files(id) ON DELETE CASCADE,
    content     TEXT NOT NULL,
    line_start  INTEGER,
    line_end    INTEGER,
    chunk_index INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS snippets_fts USING fts5(
    content,
    content='snippets',
    content_rowid='id'
);

-- Keep FTS5 index in sync with snippets table automatically
CREATE TRIGGER IF NOT EXISTS snippets_ai AFTER INSERT ON snippets BEGIN
    INSERT INTO snippets_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS snippets_ad AFTER DELETE ON snippets BEGIN
    INSERT INTO snippets_fts(snippets_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS snippets_au AFTER UPDATE ON snippets BEGIN
    INSERT INTO snippets_fts(snippets_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO snippets_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_files_path   ON files(path);

-- Episodic memory: long-term facts extracted from conversation history.
-- Searchable via FTS5 alongside code — see context/memory.py (Dev B/C, Day 5+)
CREATE TABLE IF NOT EXISTS episodic_facts (
    id          INTEGER PRIMARY KEY,
    session_id  TEXT NOT NULL,
    fact_type   TEXT,             -- decision | requirement | file_ref | preference
    content     TEXT NOT NULL,
    created_at  REAL
);

CREATE VIRTUAL TABLE IF NOT EXISTS episodic_fts USING fts5(
    content,
    content='episodic_facts',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS episodic_ai AFTER INSERT ON episodic_facts BEGIN
    INSERT INTO episodic_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


if __name__ == "__main__":
    # Quick manual check: `python -m shamsu.storage.schema`
    test_db = Path(".shamsu/index.db")
    conn = init_db(test_db)
    print(f"Schema initialized at {test_db}")
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    print("Tables:", [t[0] for t in tables])
    conn.close()
