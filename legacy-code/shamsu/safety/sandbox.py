"""
shamsu/safety/sandbox.py — Dev C owns this file.

Path sandbox. The single most important safety primitive in SHAMSU —
every file read/write/delete across every other module must go through
Sandbox.validate() before touching disk. Path.resolve() handles symlink
escapes, ../../../ traversal, and mixed separators in one call; never
use string startswith() for this, it's trivially bypassable.
"""
from __future__ import annotations

from pathlib import Path


class SecurityError(Exception):
    pass


class Sandbox:
    def __init__(self, workspace: Path):
        self.root = workspace.resolve()

    def validate(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        target = candidate.resolve()
        if not target.is_relative_to(self.root):
            raise SecurityError(
                f"Access denied: {target} is outside workspace {self.root}"
            )
        return target
