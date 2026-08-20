"""Which workspaces this installation has been used in.

Not the full registry from `PRD_REMOTE_UX.md` §7.2 - that one is P3 and comes
with `/projects`, `/use`, `/where` and per-project opt-in for remote control.
This is the smaller fact underneath it: **every workspace SHAMSU has opened**,
recorded as it opens, so any surface can list them without guessing.

It lives in `runtime/` rather than in `webui/` because it is not a web concept.
The first version recorded a workspace only when the web portal started in it,
which made the portal's own list almost always empty - you had to have already
opened the portal somewhere for it to know that somewhere existed. The REPL is
what actually knows which projects you work in, so the REPL is what records.

Nothing here enumerates your disk on its own. A workspace appears because
SHAMSU ran there, or because you pointed `--scan` at a directory.
"""
from __future__ import annotations

import json
from pathlib import Path

from shamsu.runtime.home import shamsu_home

REGISTRY_FILE = "workspaces.json"


def registry_path() -> Path:
    return shamsu_home() / REGISTRY_FILE


def known_workspaces() -> list[Path]:
    """Every remembered workspace that still exists, in the order first seen.

    A directory that has since been deleted or moved is dropped rather than
    returned: the pane must not offer a project that cannot be opened. It is
    not rewritten out of the file, because a workspace on an unmounted drive
    should come back when the drive does.
    """
    path = registry_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A hand-edited or half-written file costs the list, never the portal.
        return []
    entries = raw.get("workspaces") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    seen: list[Path] = []
    for entry in entries:
        try:
            candidate = Path(str(entry)).resolve()
        except (OSError, ValueError):
            continue
        if candidate.is_dir() and candidate not in seen:
            seen.append(candidate)
    return seen


def remember_workspace(workspace: Path | str) -> list[Path]:
    """Record a workspace, keeping the file's order and its other entries."""
    resolved = Path(workspace).resolve()
    existing = _stored()
    if str(resolved) not in existing:
        existing.append(str(resolved))
    path = registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"workspaces": existing}, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        # Losing the note is a smaller failure than refusing to start.
        pass
    return known_workspaces()


def _stored() -> list[str]:
    """The raw list as written, including entries that are not present today."""
    path = registry_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = raw.get("workspaces") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    return [str(entry) for entry in entries]


#: How deep `discover_workspaces` will look. Deep enough to find
#: `~/projects/<org>/<repo>`, shallow enough that pointing it at a home
#: directory does not turn into a full-disk walk.
SCAN_MAX_DEPTH = 4

#: Directories never worth descending into. `node_modules` alone can hide tens
#: of thousands of paths that cannot possibly be a SHAMSU workspace.
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
        "env", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
        ".tox", ".idea", ".vscode", "site-packages", "target", ".next",
    }
)


def looks_like_workspace(path: Path) -> bool:
    """Has SHAMSU actually held a conversation here?

    `.shamsu/sessions` rather than `.shamsu`, deliberately: plenty of
    directories pick up a `.shamsu` for an index or a config without ever
    having a thread in them, and a workspace with nothing to read is noise in
    the list.
    """
    return (Path(path) / ".shamsu" / "sessions").is_dir()


def discover_workspaces(root: Path | str, max_depth: int = SCAN_MAX_DEPTH) -> list[Path]:
    """Find workspaces under `root`. Explicit, bounded, and opt-in.

    Opt-in because scanning somebody's disk uninvited is not something a chat
    view should do. Bounded because the honest failure of a recursive walk is
    that it takes minutes and finds nothing.
    """
    start = Path(root).expanduser()
    try:
        start = start.resolve()
    except OSError:
        return []
    if not start.is_dir():
        return []
    found: list[Path] = []
    _walk(start, 0, max_depth, found)
    return found


def _walk(directory: Path, depth: int, max_depth: int, found: list[Path]) -> None:
    if depth > max_depth:
        return
    if looks_like_workspace(directory):
        found.append(directory)
        # A workspace inside a workspace is a build artefact, not a project.
        return
    try:
        entries = list(directory.iterdir())
    except (OSError, PermissionError):
        return
    for entry in entries:
        try:
            if not entry.is_dir() or entry.is_symlink():
                continue
        except OSError:
            continue
        if entry.name in SKIP_DIRS or entry.name.startswith("."):
            continue
        _walk(entry, depth + 1, max_depth, found)


def remember_all(paths: list[Path] | list[str]) -> list[Path]:
    for path in paths:
        remember_workspace(path)
    return known_workspaces()
