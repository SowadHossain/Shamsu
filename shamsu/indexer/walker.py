"""
Recursive project file discovery for SHAMSU.

The walker is intentionally deterministic and low-memory: it streams file
hashes in chunks, records metadata in SQLite, and does not keep file contents.
"""
from __future__ import annotations

import fnmatch
import hashlib
import sqlite3
from pathlib import Path

from shamsu.indexer.parser import build_line_windows, parse_python_symbols, read_text_file
from shamsu.session.manager import SessionLogger
from shamsu.storage.schema import init_db
from shamsu.types import IndexEntry

DEFAULT_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".shamsu",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "venv",
}

DEFAULT_IGNORE_PATTERNS = {
    "*.bmp",
    "*.bak",
    "*.db",
    "*.egg-info",
    "*.gif",
    "*.ico",
    "*.jpg",
    "*.jpeg",
    "*.lock",
    "*.mp3",
    "*.mp4",
    "*.pdf",
    "*.png",
    "*.pyc",
    "*.pyo",
    "*.sqlite",
    "*.sqlite3",
    "*.ttf",
    "*.woff",
    "*.woff2",
    "*.zip",
}

LANGUAGE_BY_EXTENSION = {
    ".css": "css",
    ".go": "go",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".md": "markdown",
    ".py": "python",
    ".rs": "rust",
    ".sh": "shell",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".txt": "text",
    ".yaml": "yaml",
    ".yml": "yaml",
}

HASH_CHUNK_SIZE = 64 * 1024


def detect_language(path: Path) -> str:
    return LANGUAGE_BY_EXTENSION.get(path.suffix.lower(), "text")


def should_ignore(path: Path, workspace_root: Path) -> bool:
    try:
        relative = path.relative_to(workspace_root)
    except ValueError:
        return True

    if any(part in DEFAULT_IGNORE_DIRS for part in relative.parts):
        return True

    name = path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in DEFAULT_IGNORE_PATTERNS)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FileWalker:
    def __init__(
        self,
        workspace_root: Path,
        db_path: Path | None = None,
        session_logger: SessionLogger | None = None,
    ):
        self.workspace_root = workspace_root.resolve()
        self.db_path = db_path or self.workspace_root / ".shamsu" / "index.db"
        self.session_logger = session_logger

    def discover(self) -> list[Path]:
        files: list[Path] = []
        for path in self.workspace_root.rglob("*"):
            if should_ignore(path, self.workspace_root):
                continue
            if path.is_file() and not path.is_symlink():
                files.append(path)
        return sorted(files, key=lambda p: p.relative_to(self.workspace_root).as_posix())

    def index(self, full: bool = False) -> list[IndexEntry]:
        """Index the workspace.

        Only rehashes and rebuilds symbols/snippets for files whose size or
        mtime changed since the last index (and only rebuilds symbols/snippets
        if the hash actually changed) — a plain `touch` or unrelated file
        doesn't pay the parse/rebuild cost. Pass full=True to force a
        complete rebuild of every file regardless of stored state.
        """
        conn = init_db(self.db_path)
        entries: list[IndexEntry] = []
        files_changed = 0
        files_skipped = 0
        try:
            discovered = self.discover()
            seen_paths = {
                path.relative_to(self.workspace_root).as_posix()
                for path in discovered
            }
            self._remove_stale_files(conn, seen_paths)
            previous_stats = {} if full else self._existing_file_stats(conn)

            for path in discovered:
                stat = path.stat()
                relative_path = path.relative_to(self.workspace_root).as_posix()
                language = detect_language(path)
                previous = previous_stats.get(relative_path)

                if (
                    previous is not None
                    and previous["size"] == stat.st_size
                    and previous["last_modified"] == stat.st_mtime
                ):
                    files_skipped += 1
                    entries.append(
                        IndexEntry(
                            file_id=previous["id"],
                            path=relative_path,
                            language=previous["language"],
                            hash=previous["hash"],
                            symbol_count=previous["symbol_count"],
                            last_modified=stat.st_mtime,
                        )
                    )
                    continue

                file_hash = sha256_file(path)
                rebuild_needed = full or previous is None or previous["hash"] != file_hash
                conn.execute(
                    """
                    INSERT INTO files (path, language, size, hash, last_modified)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        language = excluded.language,
                        size = excluded.size,
                        hash = excluded.hash,
                        last_modified = excluded.last_modified
                    """,
                    (relative_path, language, stat.st_size, file_hash, stat.st_mtime),
                )
                file_id = conn.execute(
                    "SELECT id FROM files WHERE path = ?",
                    (relative_path,),
                ).fetchone()[0]
                if rebuild_needed:
                    self._replace_file_index(conn, file_id, path, language)
                    files_changed += 1
                else:
                    files_skipped += 1
                symbol_count = conn.execute(
                    "SELECT COUNT(*) FROM symbols WHERE file_id = ?",
                    (file_id,),
                ).fetchone()[0]
                entries.append(
                    IndexEntry(
                        file_id=file_id,
                        path=relative_path,
                        language=language,
                        hash=file_hash,
                        symbol_count=symbol_count,
                        last_modified=stat.st_mtime,
                    )
                )
            conn.commit()
        finally:
            conn.close()
        if self.session_logger:
            self.session_logger.log(
                "index.updated",
                {
                    "files_scanned": len(entries),
                    "files_changed": files_changed,
                    "files_skipped": files_skipped,
                },
                f"Indexed {len(entries)} file(s): {files_changed} changed, {files_skipped} unchanged",
                workflow_id="index",
            )
        return entries

    @staticmethod
    def _existing_file_stats(conn) -> dict[str, dict]:
        rows = conn.execute(
            """
            SELECT f.id, f.path, f.language, f.size, f.hash, f.last_modified,
                   COUNT(s.id) AS symbol_count
            FROM files f
            LEFT JOIN symbols s ON s.file_id = f.id
            GROUP BY f.id
            """
        ).fetchall()
        return {
            row[1]: {
                "id": row[0],
                "language": row[2],
                "size": row[3],
                "hash": row[4],
                "last_modified": row[5],
                "symbol_count": row[6],
            }
            for row in rows
        }

    @staticmethod
    def _remove_stale_files(conn, seen_paths: set[str]) -> None:
        rows = conn.execute("SELECT id, path FROM files").fetchall()
        for file_id, file_path in rows:
            if file_path in seen_paths:
                continue
            conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM snippets WHERE file_id = ?", (file_id,))
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    @staticmethod
    def _replace_file_index(conn, file_id: int, path: Path, language: str) -> None:
        conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM snippets WHERE file_id = ?", (file_id,))

        source = read_text_file(path)
        if source is None:
            return

        for snippet in build_line_windows(source):
            conn.execute(
                """
                INSERT INTO snippets (file_id, content, line_start, line_end, chunk_index)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    snippet.content,
                    snippet.line_start,
                    snippet.line_end,
                    snippet.chunk_index,
                ),
            )

        if language != "python":
            return

        for symbol in parse_python_symbols(source):
            conn.execute(
                """
                INSERT INTO symbols
                    (file_id, name, kind, line_start, line_end, signature, docstring)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    symbol.name,
                    symbol.kind,
                    symbol.line_start,
                    symbol.line_end,
                    symbol.signature,
                    symbol.docstring,
                ),
            )


def ensure_index(workspace_root: Path, session_logger: SessionLogger | None = None) -> None:
    """Index the workspace transparently and best-effort.

    Cheap after the first run (FileWalker.index() only rehashes/rebuilds
    files that actually changed), so callers can call this unconditionally
    before search/QA/context-building instead of requiring a manual `/index`
    step. If indexing fails (e.g. a read-only workspace), this silently
    no-ops; callers fall back to checking whether an index actually exists.
    """
    try:
        FileWalker(workspace_root, session_logger=session_logger).index()
    except (OSError, sqlite3.Error):
        pass


if __name__ == "__main__":
    walker = FileWalker(Path.cwd())
    for entry in walker.index():
        print(f"{entry.language:10} {entry.hash} {entry.path}")
