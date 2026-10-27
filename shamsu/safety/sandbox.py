"""
shamsu/safety/sandbox.py — Dev C owns this file.

Path sandbox. The single most important safety primitive in SHAMSU —
every file read/write/delete across every other module must go through
Sandbox.validate() before touching disk. Path.resolve() handles symlink
escapes, ../../../ traversal, and mixed separators in one call; never
use string startswith() for this, it's trivially bypassable.
"""
from __future__ import annotations

import os
from pathlib import Path


class SecurityError(Exception):
    pass


class Sandbox:
    def __init__(self, workspace: Path):
        self.root = _comparison_path(workspace.resolve())

    def validate(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        target = _comparison_path(candidate.resolve())
        if not target.is_relative_to(self.root):
            raise SecurityError(
                f"Access denied: {target} is outside workspace {self.root}"
            )
        return target


def _comparison_path(path: Path) -> Path:
    """Windows can resolve the same file using either spelling of its drive.

    Normalize only ordinary drive/UNC aliases after resolving symlinks. Device
    namespaces remain distinct and still fail the containment check.
    """
    if os.name != "nt":
        return path
    text = str(path)
    prefix = "\\\\?\\"
    if text.lower().startswith(prefix + "unc\\"):
        return Path("\\\\" + text[len(prefix + "UNC\\"):])
    if text.startswith(prefix):
        drive = text[len(prefix):]
        if len(drive) >= 3 and drive[0].isalpha() and drive[1:3] == ":\\":
            return Path(drive)
    return path
