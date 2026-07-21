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

from pathlib import Path

from shamsu.indexer.policy import (
    DEFAULT_EXCLUDED_DIRS,
    DEFAULT_EXCLUDED_PATTERNS,
    is_indexable_file,
    walk_workspace_files,
)

DEFAULT_IGNORE_DIRS = DEFAULT_EXCLUDED_DIRS
DEFAULT_IGNORE_PATTERNS = DEFAULT_EXCLUDED_PATTERNS


def should_ignore(path: Path, workspace_root: Path) -> bool:
    return not is_indexable_file(path, workspace_root)


class FileWalker:
    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root.resolve()

    def discover(self) -> list[Path]:
        return walk_workspace_files(self.workspace_root, indexable_only=True)
