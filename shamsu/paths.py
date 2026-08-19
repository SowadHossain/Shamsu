"""Every location SHAMSU writes inside a workspace, named in one place.

`.shamsu/` had grown to 35 top-level entries. Nobody decided that; it happened
because thirty modules each built their own path inline, so no single file ever
showed what the directory contained and nothing stopped it growing. Compare
smallcode's `.smallcode/`: three entries - `memory/`, `plugins/`, `rag/`.

This module is the fix for the cause rather than the symptom. A new location
has to be added here to exist, which means the layout is reviewable in one
diff.

## The layout

    .shamsu/
      cache/          regenerable. Safe to delete at any time; SHAMSU rebuilds
                      it. Web pages, OCR output, the semantic index, the
                      per-model token calibration, first-run reports.
      config/         user and machine settings. Small, hand-editable, durable.
      memory/         what SHAMSU knows about this project - the SQLite store,
                      and `notes/` for the typed notes the model writes itself.
      sessions/       conversations. The append-only transcripts resume reads.
      runs/           per-prompt action-ledger records.
      audit/          the evidence trail.
      plans/          reviewable plans awaiting `proceed`.
      mutations/      patch journal and transactions.
      trash/          soft-deleted files, recoverable.
      abstract/       code-graph index state.
      tools/          managed tool installs and their state.
      skills/         workspace skills.
      taskmaster/     task state.
      documents/      extracted document text (PRDs and the like).

## What is NOT moved, and why

`sessions/`, `runs/`, `audit/`, `mutations/`, `trash/`, `plans/` and `memory/`
hold work that cannot be regenerated. Relocating them would mean every existing
workspace silently losing its history the first time a new build opened it -
a tidier tree is not worth that. They keep their paths; this module simply
names them so they stop being invisible.

Only regenerable state moves, and even that migrates rather than vanishing.
"""
from __future__ import annotations

import shutil
from pathlib import Path

SHAMSU_DIRNAME = ".shamsu"

# Regenerable. The whole directory can be deleted without losing anything.
CACHE_DIR = "cache"
# Settings. Small, durable, meant to be read and edited by a person.
CONFIG_DIR = "config"


def shamsu_dir(workspace: Path) -> Path:
    return Path(workspace) / SHAMSU_DIRNAME


def cache_dir(workspace: Path) -> Path:
    return shamsu_dir(workspace) / CACHE_DIR


def config_dir(workspace: Path) -> Path:
    return shamsu_dir(workspace) / CONFIG_DIR


def ensure(path: Path) -> Path:
    """Create *path* as a directory and return it. Best-effort."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


def migrated(workspace: Path, old_name: str, new_path: Path) -> Path:
    """Return *new_path*, moving a pre-existing ``.shamsu/<old_name>`` onto it.

    Called at the point of use rather than by a migration step at startup, so a
    workspace that is never opened again is never touched, and one opened by an
    older build still finds its files where it expects them until this runs.

    Deliberately forgiving: if anything at all goes wrong the old path is
    returned unchanged. A tidier directory is not worth a failed run, and a
    half-completed move is worse than no move.
    """
    old = shamsu_dir(workspace) / old_name
    try:
        if new_path.exists() or not old.exists():
            return new_path
        ensure(new_path.parent)
        shutil.move(str(old), str(new_path))
    except OSError:
        return old
    return new_path


# -- regenerable ---------------------------------------------------------
# Each of these was a top-level entry. They are caches: the only cost of
# getting the move wrong is that something is rebuilt once.


def web_cache_db(workspace: Path) -> Path:
    return migrated(workspace, "web_cache.db", cache_dir(workspace) / "web.db")


def web_dir(workspace: Path) -> Path:
    return migrated(workspace, "web", cache_dir(workspace) / "web")


def browser_dir(workspace: Path) -> Path:
    return migrated(workspace, "browser", cache_dir(workspace) / "browser")


def ocr_dir(workspace: Path) -> Path:
    return migrated(workspace, "ocr", cache_dir(workspace) / "ocr")


def prd_cache_dir(workspace: Path) -> Path:
    return migrated(workspace, "cache", cache_dir(workspace) / "prd")


def semantic_index(workspace: Path) -> Path:
    return migrated(
        workspace, "semantic_index.json", cache_dir(workspace) / "semantic_index.json"
    )


def context_calibration(workspace: Path) -> Path:
    return migrated(
        workspace,
        "context_calibration.json",
        cache_dir(workspace) / "context_calibration.json",
    )


def first_run_report(workspace: Path) -> Path:
    return migrated(
        workspace, "first-run-report.json", cache_dir(workspace) / "first-run-report.json"
    )


# -- durable data, named but not moved -----------------------------------


def sessions_dir(workspace: Path) -> Path:
    return shamsu_dir(workspace) / "sessions"


def runs_dir(workspace: Path) -> Path:
    return shamsu_dir(workspace) / "runs"


def memory_dir(workspace: Path) -> Path:
    return shamsu_dir(workspace) / "memory"


def memory_notes_dir(workspace: Path) -> Path:
    """Typed notes the model writes for itself, beside the SQLite store."""
    return memory_dir(workspace) / "notes"


def code_graph_dir(workspace: Path) -> Path:
    """Where this workspace's code graph lives, INSIDE the workspace.

    Codebase-Memory defaults to one global cache (`~/.cache/codebase-memory-mcp`)
    keyed by a mangled absolute path, so every directory anything was ever
    pointed at accumulates forever: 243 projects and 619 MB on 2026-08-19, of
    which 129 were temp directories that had not existed for weeks.

    smallcode keeps its graph at `.code-graph/graph.db` inside the project, and
    that one decision makes the whole class of problem impossible - delete the
    project and the index goes with it. `CBM_CACHE_DIR` lets SHAMSU do the same.

    Under `.shamsu/` rather than a second top-level dotfolder: it is already
    git-ignored, already excluded from indexing, and one dotfolder per tool is
    tidier than smallcode's three. The locality is what matters, not the name.
    """
    return shamsu_dir(workspace) / "code-graph"
