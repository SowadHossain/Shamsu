"""Git as the recovery mechanism: what a checkpoint points at, and how to undo.

`CheckpointRecord` has carried a `git_ref` field since the schema was written
and nothing ever set it, so a checkpoint recorded a state snapshot with no way
to get the *files* back. `rollback_to` and `PatchUndo.rollback_all` were
implemented, tested, and called by nothing. The result is a runtime that
checkpoints diligently and cannot revert.

Two deliberate limits on what this does.

**It records; it does not automatically revert.** Auto-rollback on a failed step
sounds right and conflicts directly with the fix that lets a failed step be
local: later steps keep running, and some of them build on what the failed step
wrote. Reverting under them would turn one honest failure into a corrupted
workspace — the opposite of the property this is for. So the runtime captures
the point it could return to, reports it, and leaves the decision where it
belongs.

**A ref is only recorded when it means something.** Outside a repository, or
before the first commit, there is no ref; the field stays `None` rather than
holding a string that cannot be checked out.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shamsu.tools.git import run_git

#: How much of a hash to show a human. Enough to be unambiguous in any
#: repository a person is looking at, short enough to read in a report line.
SHORT_REF = 12


def head_ref(workspace: Path) -> str | None:
    """The commit a checkpoint can return the working tree to.

    `None` for a workspace that is not a repository, or one with no commits
    yet — both are ordinary states, and a checkpoint without a ref is still
    worth having for its state snapshot.
    """
    outcome = run_git(workspace, "rev-parse", "HEAD")
    if not outcome.ok:
        return None
    ref = outcome.text.strip()
    return ref or None


@dataclass(frozen=True)
class RecoveryPoint:
    """Where a run could be returned to, and what has happened since."""

    ref: str | None
    changed_files: tuple[str, ...] = ()

    @property
    def recoverable(self) -> bool:
        return self.ref is not None

    def render(self) -> str:
        """A line a person can act on.

        Named commands rather than prose. "You can revert" is not actionable at
        the moment someone needs it; `git checkout -- .` is.
        """
        if not self.recoverable:
            return (
                "No recovery point: this workspace is not a git repository, so "
                "changes cannot be reverted automatically. `git init` would fix "
                "that for the next run."
            )
        short = (self.ref or "")[:SHORT_REF]
        if not self.changed_files:
            return f"Recovery point {short}; the working tree is unchanged."

        listed = ", ".join(self.changed_files[:5])
        more = f" (+{len(self.changed_files) - 5} more)" if len(self.changed_files) > 5 else ""
        return (
            f"Recovery point {short}. Changed since: {listed}{more}. "
            f"To discard the run's edits: `git checkout {short} -- .` "
            f"(untracked files are not removed by that; `git clean -n` lists them first)."
        )


def recovery_point(workspace: Path, changed_files: tuple[str, ...] = ()) -> RecoveryPoint:
    """Capture where this run could be returned to."""
    return RecoveryPoint(ref=head_ref(workspace), changed_files=changed_files)


__all__ = ["SHORT_REF", "RecoveryPoint", "head_ref", "recovery_point"]
