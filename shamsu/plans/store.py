"""Workspace-local store for reviewable plan files under ``.shamsu/plans/``.

Mirrors the ``.shamsu/tasks/`` layout in ``tasks/state.py``: each plan is a
timestamped markdown file the user can open and edit before approving it. The
step parser is deliberately the same shape as the REPL's milestone-list parsing
so the "proceed" path re-reads the (possibly edited) file and honours it.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from shamsu.safety.sandbox import Sandbox

PLANS_DIRNAME = "plans"

# Written into a plan file whose planner produced nothing. Prose, not a list
# item, so `parse_plan_steps` cannot mistake it for work to execute.
PLAN_NO_STEPS_MARKER = (
    "_No steps were produced._ Edit this file to add them, then run `/proceed`."
)


def plan_has_no_steps(markdown: str) -> bool:
    """True when the planner produced nothing and the file says so."""
    return PLAN_NO_STEPS_MARKER in (markdown or "")


def plans_dir(workspace: Path) -> Path:
    directory = Sandbox(workspace).validate(Path(".shamsu") / PLANS_DIRNAME)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def new_plan_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"plan-{stamp}-{uuid.uuid4().hex[:6]}"


def plan_path(workspace: Path, plan_id: str) -> Path:
    return plans_dir(workspace) / f"{plan_id}.md"


def write_plan(workspace: Path, plan_id: str, markdown: str) -> Path:
    path = plan_path(workspace, plan_id)
    path.write_text(markdown, encoding="utf-8")
    return path


def read_plan(workspace: Path, plan_id: str) -> str:
    return plan_path(workspace, plan_id).read_text(encoding="utf-8")


def list_plan_ids(workspace: Path) -> list[str]:
    return sorted(p.stem for p in plans_dir(workspace).glob("*.md"))


def parse_plan_steps(markdown: str) -> list[str]:
    """Pull the ordered items under a ``## Steps`` heading out of a plan file.

    Collects numbered (``1.`` / ``1)``) or bulleted (``-`` / ``*``) lines until
    the next heading, so a user can edit/reorder steps and the executor honours
    the edited file. Returns ``[]`` when nothing parses (the caller then runs a
    single agent pass over the whole plan instead of failing).
    """
    steps: list[str] = []
    in_steps = False
    for line in markdown.splitlines():
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if heading:
            in_steps = heading.group(1).strip().lower().startswith("steps")
            continue
        if not in_steps:
            continue
        item = re.match(r"^\s*(?:\d+[.)]|[-*])\s+(.*)$", line)
        if item:
            text = item.group(1).strip()
            if text:
                steps.append(text)
    return steps
