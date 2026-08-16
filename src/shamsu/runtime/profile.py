"""The project's stack, derived once and pinned for the whole session.

A live PRD build asked for a *command-line bookmark manager, standard library
only, JSON storage on disk*. The plan that came back was:

    1. Set up the virtual environment
    2. Install dependencies
    3. Configure the database
    4. Set up environment variables
    5. Implement routes and services
    6. Run the application

A generic web application, invented whole. Nothing in the workspace suggested a
database or a route; the model had simply never been told what kind of project
this was, and filled the gap with the most common shape it knows.

That is what this module removes. `project.inspect` could already answer the
question, but it is a *tool* — the model has to think to call it, the answer
lives in one observation, and the next turn starts guessing again. The stack
then changes in the middle of a conversation, which is the one thing a project
profile must never do.

So the profile is:

**Derived, not asked for.** `RepositoryManifestGenerator` reads manifests,
entry points and test configuration off the filesystem. Invariant 8 — structural
facts come from parsers. A model may summarise the stack; it may not be the
source of it.

**Written as facts, not held in a variable.** `project_facts` gives durability,
confidence, and — the part that matters here — contradiction handling. A fact
recorded `DERIVED` cannot be overwritten by a model `ASSERTED`ing something
else mid-run, because `MemoryStore.learn` refuses to let a weaker origin
replace a stronger one. The stack is pinned in the strongest sense available:
not by being immutable, but by outranking anything that would change it.

**Re-derived only when the evidence changes.** Each fact carries the hash of
the manifest files it came from, so `revalidate` retires it when they move and
leaves it alone when they do not. Freshness without drift.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from shamsu.artifacts.generators import (
    RepositoryContext,
    RepositoryManifestGenerator,
    RepositoryMapGenerator,
)
from shamsu.artifacts.hashing import scan_repository
from shamsu.interfaces.enums import FactKind, FactOrigin
from shamsu.memory.store import MemoryStore, combined_hash

#: Manifest field → the subject a fact about it is filed under. Subjects are
#: stable because `learn` reconciles by `(kind, subject)`: a second pass over an
#: unchanged repository must confirm the existing fact rather than add a rival.
#:
#: Ordered by how much it constrains the work. `languages` decides what a file
#: even looks like; `major_directories` only says where to put it.
_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("languages", "language", "The project is written in {value}."),
    ("package_managers", "package manager", "Dependencies are managed with {value}."),
    ("test_frameworks", "test framework", "Tests are written for {value}."),
    ("test_commands", "test command", "Tests are run with: {value}"),
    ("run_commands", "run command", "The project is run with: {value}"),
    ("build_commands", "build command", "The project is built with: {value}"),
    ("entry_points", "entry point", "Execution starts at {value}."),
    ("major_directories", "layout", "Source is organised under {value}."),
)

#: How much of the repository map to keep. Enough to see the shape of a real
#: project, short of turning the frame into a file listing.
MAP_LINE_LIMIT = 60

#: How many values to keep per field. A repository with forty directories does
#: not need forty named; the point is orientation, not an inventory.
_MAX_VALUES = 6


def stack_facts(workspace: Path, *, use_git: bool = True) -> dict[str, str]:
    """Read the project's stack off the filesystem.

    Returns `subject -> statement`, empty for a workspace with nothing to say.
    Separated from the writing so the derivation can be tested without a store,
    and so a caller that only wants to *show* the profile need not record it.
    """
    context = RepositoryContext(Path(workspace).resolve(), use_git=use_git)
    generated = RepositoryManifestGenerator(context).generate(RepositoryManifestGenerator.KEY)

    try:
        payload: Any = json.loads(generated.content)
    except json.JSONDecodeError:  # pragma: no cover - the generator emits JSON
        return {}
    if not isinstance(payload, dict):  # pragma: no cover - defensive
        return {}

    facts: dict[str, str] = {}
    for field, subject, template in _FIELDS:
        value = _render(payload.get(field))
        if value:
            facts[subject] = template.format(value=value)

    # An empty workspace is a real state, not a failure, and saying so beats
    # saying nothing: a plan for a project with no files should look different
    # from a plan for one whose stack merely could not be determined.
    if not facts and not payload.get("file_count"):
        facts["layout"] = "The workspace is empty; nothing has been created yet."

    return facts


def pin_stack(workspace: Path, memory: MemoryStore | None, *, use_git: bool = True) -> int:
    """Record the derived stack as project facts. Returns how many were written.

    Safe to call on every session: `learn` reconciles by subject, so a repeat
    pass over an unchanged repository confirms what is stored — raising its
    confidence — rather than accumulating duplicates.
    """
    if memory is None:
        return 0

    facts = stack_facts(workspace, use_git=use_git)

    # The manifests the profile is read off. Recorded as evidence so
    # `MemoryStore.revalidate` can mark these facts unverified when one of them
    # changes — without evidence paths a fact is *unfalsifiable*, which is how
    # a memory layer ends up asserting a stack the project moved off a year
    # ago. `combined_hash` treats a missing path as `<missing>`, so deleting a
    # manifest invalidates too.
    present = [name for name in MANIFESTS if (workspace / name).exists()]
    hashes = scan_repository(workspace, use_git=use_git)

    for subject, statement in facts.items():
        memory.learn(
            FactKind.STACK,
            subject,
            statement,
            # DERIVED, not OBSERVED: no single tool event produced this, a
            # parser did. It still outranks anything a model asserts, which is
            # what stops the stack being talked out of itself mid-run.
            origin=FactOrigin.DERIVED,
            evidence_paths=present,
            evidence_hash=combined_hash(present, hashes),
        )
    return len(facts)


#: Files whose contents decide what the stack profile says. Deliberately a
#: fixed list rather than "every file": a fact about the project's build system
#: does not go stale because a source file changed, and invalidating on every
#: edit would make the label meaningless by making it universal.
MANIFESTS: tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "package.json",
    "pnpm-workspace.yaml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "composer.json",
    "manage.py",
    "Dockerfile",
    "docker-compose.yml",
    "Makefile",
)


def render_stack(workspace: Path, *, use_git: bool = True) -> str:
    """The profile as a frame section, most constraining fact first.

    Rendered from the filesystem rather than from `recall()` so it is always
    present and never competes for a slot. `recall` caps at a handful of facts
    ordered by confidence, and the stack losing that contest is exactly the
    failure this module exists to prevent.
    """
    facts = stack_facts(workspace, use_git=use_git)
    return "\n".join(f"- {statement}" for statement in facts.values())


def render_map(workspace: Path, *, use_git: bool = True, limit: int = MAP_LINE_LIMIT) -> str:
    """The repository's shape: which directories exist and what is in them.

    The other half of planning blind. The stack profile says *what kind of
    project* this is; this says *what is already in it* — and without it a plan
    cannot name a file that exists, so it names one it invented. A live PRD
    build put an entire four-feature system into a single `bookmarks_manager.py`
    because nothing in the frame suggested a project has more than one file.

    **Generated live, not read from the artifact registry.** `ArtifactRegistry`
    exists, tracks freshness, and is constructed nowhere in `src/` — the whole
    layer is dead code in production. Wiring its persistence in would buy
    caching and cost a staleness bug; the map is one pass over the file hashes
    the context already holds, so it is cheaper to be always right than to be
    occasionally stale.

    Truncated by lines rather than summarised: a partial map is still true, and
    a map "summarised" by anything other than a parser stops being a structural
    fact (invariant 8).
    """
    context = RepositoryContext(Path(workspace).resolve(), use_git=use_git)
    generated = RepositoryMapGenerator(context).generate(RepositoryMapGenerator.KEY)

    lines = generated.content.splitlines()
    if len(lines) > limit:
        lines = [*lines[:limit], f"… ({len(lines) - limit} more line(s) of structure)"]

    # The generator counts files per directory and names none of them, which
    # answers "how big is this project" and not "which file do I modify" — and
    # for a two-file project it says nothing whatsoever. The paths are the point,
    # so they are listed explicitly.
    paths = sorted(context.hashes)
    shown = paths[:limit]
    if shown:
        lines += ["", "## Files", *shown]
        if len(paths) > len(shown):
            lines.append(f"… ({len(paths) - len(shown)} more; use file.list to see them)")

    return "\n".join(lines)


def _render(value: object) -> str:
    """One manifest field as a readable phrase."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence):
        items = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(items[:_MAX_VALUES])
    return ""


__all__ = ["MAP_LINE_LIMIT", "pin_stack", "render_map", "render_stack", "stack_facts"]
