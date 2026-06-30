"""
Recursive project file discovery for SHAMSU.

The walker is intentionally deterministic and low-memory: it streams file
hashes in chunks, records metadata in SQLite, and does not keep file contents.
"""
from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path

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
    def __init__(self, workspace_root: Path, db_path: Path | None = None):
        self.workspace_root = workspace_root.resolve()
        self.db_path = db_path or self.workspace_root / ".shamsu" / "index.db"

    def discover(self) -> list[Path]:
        files: list[Path] = []
        for path in self.workspace_root.rglob("*"):
            if should_ignore(path, self.workspace_root):
                continue
            if path.is_file() and not path.is_symlink():
                files.append(path)
        return sorted(files, key=lambda p: p.relative_to(self.workspace_root).as_posix())

    def index(self) -> list[IndexEntry]:
        conn = init_db(self.db_path)
        entries: list[IndexEntry] = []
        try:
            for path in self.discover():
                stat = path.stat()
                relative_path = path.relative_to(self.workspace_root).as_posix()
                file_hash = sha256_file(path)
                language = detect_language(path)
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
        return entries


if __name__ == "__main__":
    walker = FileWalker(Path.cwd())
    for entry in walker.index():
        print(f"{entry.language:10} {entry.hash} {entry.path}")
