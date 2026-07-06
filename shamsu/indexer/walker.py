"""
Workspace file discovery for SHAMSU.

This is deliberately *just* discovery (respecting the ignore list) - there is
no SHAMSU-owned parsing, symbol extraction, or search index here. Structural
code facts and search come from the real Codebase-Memory MCP tool
(see shamsu/tools/codebase_memory.py, shamsu/retriever/search.py); this
module only backs `AbstractService`'s cheap file-count/mtime staleness
snapshot.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

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


def should_ignore(path: Path, workspace_root: Path) -> bool:
    try:
        relative = path.relative_to(workspace_root)
    except ValueError:
        return True

    if any(part in DEFAULT_IGNORE_DIRS for part in relative.parts):
        return True

    name = path.name
    return any(fnmatch.fnmatch(name, pattern) for pattern in DEFAULT_IGNORE_PATTERNS)


class FileWalker:
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root.resolve()

    def discover(self) -> list[Path]:
        files: list[Path] = []
        for path in self.workspace_root.rglob("*"):
            if should_ignore(path, self.workspace_root):
                continue
            try:
                is_file = path.is_file() and not path.is_symlink()
            except OSError:
                # Broken/inaccessible reparse points (e.g. HuggingFace hub cache
                # symlinks on Windows without Developer Mode) raise instead of
                # returning False here - treat them as unreadable and skip.
                continue
            if is_file:
                files.append(path)
        return sorted(files, key=lambda p: p.relative_to(self.workspace_root).as_posix())
