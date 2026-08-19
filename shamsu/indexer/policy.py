"""Shared policy for files that belong to a user's workspace.

Every discovery and retrieval path should use this module instead of carrying
its own approximation of SHAMSU's internal, dependency, cache, and build dirs.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable

POLICY_VERSION = 1

DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".cache",
        ".codebase-memory",
        ".git",
        ".hg",
        ".mypy_cache",
        ".next",
        ".nuxt",
        ".pytest_cache",
        ".ruff_cache",
        ".shamsu",
        ".svn",
        ".tox",
        ".turbo",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "env",
        "node_modules",
        "site-packages",
        "target",
        "venv",
    }
)

DEFAULT_EXCLUDED_FILES = frozenset(
    {
        ".cbmignore",
        ".codebase-memory.json",
        ".DS_Store",
        # Verifier artifacts SHAMSU writes into the project it is checking.
        # They are its own scaffolding, not the user's code: indexing them put
        # `.shamsu_probe.py exports: src, m, declared, failures` into a
        # Codebase-Memory brief about the user's views, and made the agent's
        # own probe a candidate for editing.
        ".shamsu_probe.py",
        ".shamsu_probe.mjs",
        ".shamsu_template_probe.py",
    }
)

DEFAULT_EXCLUDED_PATTERNS = frozenset(
    {
        "*.bak",
        "*.bmp",
        "*.db",
        "*.egg-info",
        "*.doc",
        "*.docx",
        "*.gif",
        "*.ico",
        "*.jpeg",
        "*.jpg",
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
)

SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cpp",
        ".cs",
        ".css",
        ".go",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".md",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vue",
        ".yaml",
        ".yml",
    }
)

_CBM_BEGIN = "# BEGIN SHAMSU MANAGED EXCLUSIONS"
_CBM_END = "# END SHAMSU MANAGED EXCLUSIONS"


def relative_path(path: Path, workspace_root: Path) -> Path | None:
    try:
        return Path(path).resolve().relative_to(Path(workspace_root).resolve())
    except (OSError, ValueError):
        return None


def is_workspace_path(path: Path, workspace_root: Path) -> bool:
    """Whether a path is user-owned rather than internal/generated/vendor data."""
    relative = relative_path(path, workspace_root)
    if relative is None or relative == Path("."):
        return relative == Path(".")
    return (
        path.name not in DEFAULT_EXCLUDED_FILES
        and not any(part in DEFAULT_EXCLUDED_DIRS for part in relative.parts)
    )


def is_indexable_file(path: Path, workspace_root: Path) -> bool:
    """Whether a regular workspace file belongs in code retrieval indexes."""
    if not is_workspace_path(path, workspace_root) or path.name in DEFAULT_EXCLUDED_FILES:
        return False
    if any(fnmatch.fnmatch(path.name, pattern) for pattern in DEFAULT_EXCLUDED_PATTERNS):
        return False
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def walk_workspace_files(
    workspace_root: Path,
    *,
    limit: int | None = None,
    suffixes: Iterable[str] | None = None,
    indexable_only: bool = False,
) -> list[Path]:
    """Walk files with in-place directory pruning and deterministic ordering."""
    root = Path(workspace_root).resolve()
    allowed_suffixes = {suffix.lower() for suffix in suffixes} if suffixes is not None else None
    found: list[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in DEFAULT_EXCLUDED_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if name in DEFAULT_EXCLUDED_FILES:
                    continue
                if allowed_suffixes is not None and path.suffix.lower() not in allowed_suffixes:
                    continue
                if indexable_only:
                    if not is_indexable_file(path, root):
                        continue
                else:
                    try:
                        if not path.is_file() or path.is_symlink():
                            continue
                    except OSError:
                        continue
                found.append(path)
                if limit is not None and len(found) >= limit:
                    return found
    except OSError:
        return []
    return found


def walk_workspace_paths(workspace_root: Path, *, limit: int | None = None) -> list[Path]:
    """Walk visible user-owned files and directories without entering exclusions."""
    root = Path(workspace_root).resolve()
    found: list[Path] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(name for name in dirnames if name not in DEFAULT_EXCLUDED_DIRS)
            for name in [*dirnames, *sorted(filenames)]:
                path = Path(dirpath) / name
                if not is_workspace_path(path, root):
                    continue
                found.append(path)
                if limit is not None and len(found) >= limit:
                    return found
    except OSError:
        return []
    return found


def workspace_manifest(workspace_root: Path) -> dict[str, int | str]:
    """Stable signature of the exact file set used for code indexing."""
    root = Path(workspace_root).resolve()
    digest = hashlib.sha256()
    count = 0
    max_mtime_ns = 0
    for path in walk_workspace_files(root, indexable_only=True):
        try:
            stat = path.stat()
            relative = path.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        digest.update(f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode("utf-8"))
        count += 1
        max_mtime_ns = max(max_mtime_ns, stat.st_mtime_ns)
    return {
        "policy_version": POLICY_VERSION,
        "file_count": count,
        "max_mtime_ns": max_mtime_ns,
        "manifest_hash": digest.hexdigest(),
    }


# How deep to look for a repository nested inside the workspace. Vendored code
# lives one or two levels down (`reference/smallcode`, `other peoples work/
# SmallCTL`); deeper than that and the scan costs more than it saves.
NESTED_REPO_SCAN_DEPTH = 3


def nested_repository_dirs(workspace_root: Path) -> list[str]:
    """Directories inside the workspace that are their own repository.

    A git repository inside your repository is somebody else's code - a clone,
    a vendored dependency, a reference copy - and indexing it means the graph
    answers questions about it as though it were yours.

    Live on this repo:

        graph_search("message_tokens")
        -> _estimate_emitted_message_tokens
           other peoples work/SmallCTL/src/smallctl/context/assembler.py

    Wrong function, wrong project; the real `message_tokens` in
    `shamsu/context/budget.py` was missed entirely. All three offending trees -
    `reference/smallcode`, `other peoples work/SmallCTL` and `.../little-coder` -
    turned out to be clones carrying their own `.git`.

    Detected rather than named, on purpose. The obvious fix is to add
    `reference/` to `DEFAULT_EXCLUDED_DIRS`, but that set is matched against
    EVERY path part, so it would silently drop a directory called `reference`
    from every other workspace SHAMSU is ever pointed at. "Is its own
    repository" is a fact about the tree, and it is true wherever it is found.
    """
    root = Path(workspace_root).resolve()
    found: list[str] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > NESTED_REPO_SCAN_DEPTH:
            return
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                continue
            if entry.name in DEFAULT_EXCLUDED_DIRS:
                continue
            if (entry / ".git").exists():
                found.append(entry.relative_to(root).as_posix() + "/")
                continue          # its contents go with it
            walk(entry, depth + 1)

    walk(root, 1)
    return found


def cbm_ignore_rules(workspace_root: Path | None = None) -> list[str]:
    """Rules for Codebase-Memory's supported project-local `.cbmignore`.

    Given a workspace, nested repositories are excluded too - see
    `nested_repository_dirs`. Without one the static rules come back unchanged,
    so every existing caller keeps the behaviour it had.
    """
    directory_rules = [f"{name}/" for name in sorted(DEFAULT_EXCLUDED_DIRS)]
    nested = nested_repository_dirs(workspace_root) if workspace_root is not None else []
    return [
        *directory_rules,
        *nested,
        *sorted(DEFAULT_EXCLUDED_FILES),
        *sorted(DEFAULT_EXCLUDED_PATTERNS),
    ]


def ensure_cbm_ignore(workspace_root: Path) -> dict[str, object]:
    """Install/update SHAMSU's block while preserving user-authored rules."""
    path = Path(workspace_root).resolve() / ".cbmignore"
    managed = "\n".join([_CBM_BEGIN, *cbm_ignore_rules(path.parent), _CBM_END])
    try:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        if _CBM_BEGIN in existing and _CBM_END in existing:
            before, remainder = existing.split(_CBM_BEGIN, 1)
            _old, after = remainder.split(_CBM_END, 1)
            updated = f"{before}{managed}{after}"
        else:
            separator = "" if not existing else ("\n" if existing.endswith("\n") else "\n\n")
            updated = f"{existing}{separator}{managed}\n"
        if updated != existing:
            path.write_text(updated, encoding="utf-8")
        return {
            "ok": True,
            "path": str(path),
            "changed": updated != existing,
            "rules_hash": hashlib.sha256(managed.encode("utf-8")).hexdigest(),
        }
    except OSError as exc:
        return {"ok": False, "path": str(path), "changed": False, "error": str(exc)}


def manifest_json(workspace_root: Path) -> str:
    return json.dumps(workspace_manifest(workspace_root), sort_keys=True)
