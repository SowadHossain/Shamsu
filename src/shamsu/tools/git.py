"""Git tools: `git.inspect` and `git.checkpoint`.

Logical tools, not command wrappers. Plan §22 is explicit that the model should
not be choosing among low-level git commands for ordinary work, so
`git.inspect` collects branch, status, changed files, diff, untracked files,
and recent commits in one call. v1 exposed twenty-three separate git tools and
the model spent turns picking between them.

`git.checkpoint` is the rollback anchor. It commits the current state so a
later step can be undone by returning to a known-good tree — which is what
makes "reversible" true for a multi-file change that `PatchUndo` alone cannot
cover.

Every command here is a fixed argv list run without a shell. No model-supplied
string is ever interpolated into a command.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.enums import EvidenceKind, Phase, Risk
from shamsu.interfaces.tools import ToolContract, ToolResult
from shamsu.tools.base import Tool

#: Diff output past this is summarised rather than shown. A full diff of a
#: large change would evict the task from the frame.
MAX_DIFF_LINES = 200


@dataclass(frozen=True)
class GitOutcome:
    ok: bool
    stdout: str
    stderr: str

    @property
    def text(self) -> str:
        return self.stdout.strip() or self.stderr.strip()


def run_git(workspace: Path, *args: str, timeout: float = 20.0) -> GitOutcome:
    """Run one git command with a fixed argv.

    No shell and no interpolation: `args` are literal arguments, so nothing a
    model produces can become a command. `-C` rather than `cwd` so the target
    repository is unambiguous even if the process working directory moves.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return GitOutcome(False, "", "git is not installed")
    except subprocess.TimeoutExpired:
        return GitOutcome(False, "", f"git {args[0] if args else ''} timed out")
    except OSError as exc:
        return GitOutcome(False, "", f"git failed: {exc}")

    return GitOutcome(completed.returncode == 0, completed.stdout, completed.stderr)


def is_repository(workspace: Path) -> bool:
    return run_git(workspace, "rev-parse", "--git-dir").ok


# ---------------------------------------------------------------------------
# git.inspect
# ---------------------------------------------------------------------------


class GitInspectInput(BaseModel):
    include_diff: bool = Field(
        default=True, description="Include the working-tree diff. Turn off if it is large."
    )
    path: str = Field(default="", description="Limit the diff to this path.")


class GitInspectTool(Tool[GitInspectInput]):
    """Branch, status, changed files, diff, untracked files, recent commits."""

    input_model = GitInspectInput

    contract = ToolContract(
        name="git.inspect",
        purpose=(
            "Show repository state: branch, changed and untracked files, the "
            "working-tree diff, and recent commits."
        ),
        allowed_phases=frozenset(
            {Phase.INSPECT, Phase.PLAN, Phase.AUTHOR, Phase.VERIFY, Phase.REPAIR}
        ),
        risk=Risk.LOW,
        reversible=True,
        timeout_seconds=30.0,
        max_output_bytes=20_000,
        produces_evidence=frozenset({EvidenceKind.GIT_DIFF_REVIEWED}),
    )

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    async def run(self, arguments: GitInspectInput, cancel: CancellationToken) -> ToolResult:
        started = time.monotonic()
        cancel.raise_if_cancelled()

        if not is_repository(self._workspace):
            return self.failed("not a git repository", started=started)

        sections: list[str] = []

        branch = run_git(self._workspace, "rev-parse", "--abbrev-ref", "HEAD")
        sections.append(f"Branch: {branch.text or '(unknown)'}")

        status = run_git(self._workspace, "status", "--porcelain")
        if status.stdout.strip():
            sections.append(f"Changed files:\n{status.stdout.strip()}")
        else:
            sections.append("Working tree is clean.")

        cancel.raise_if_cancelled()

        if arguments.include_diff:
            diff_args = ["diff"]
            if arguments.path:
                diff_args += ["--", arguments.path]
            diff = run_git(self._workspace, *diff_args)
            if diff.stdout.strip():
                sections.append(f"Diff:\n{self._trim(diff.stdout)}")

        log = run_git(self._workspace, "log", "--oneline", "-5")
        if log.stdout.strip():
            sections.append(f"Recent commits:\n{log.stdout.strip()}")
        else:
            # A fresh repository has no commits; saying so beats an empty
            # section the model has to interpret.
            sections.append("Recent commits: none (no commits yet).")

        return self.ok("\n\n".join(sections), started=started)

    @staticmethod
    def _trim(diff: str) -> str:
        lines = diff.splitlines()
        if len(lines) <= MAX_DIFF_LINES:
            return diff.strip()
        kept = "\n".join(lines[:MAX_DIFF_LINES])
        return f"{kept}\n… ({len(lines) - MAX_DIFF_LINES} more diff line(s); narrow with 'path')"


# ---------------------------------------------------------------------------
# git.checkpoint
# ---------------------------------------------------------------------------


class GitCheckpointInput(BaseModel):
    label: str = Field(
        min_length=1,
        max_length=120,
        description="What this checkpoint captures, e.g. 'after adding login validation'.",
    )


class GitCheckpointTool(Tool[GitCheckpointInput]):
    """Commit the current state so a later step can be rolled back to it.

    Mutating — it writes a commit — but not risky: it only ever *adds* history,
    and the whole point is to make the preceding work recoverable. Marked
    `reversible` because `git reset` returns to the parent.

    Commits are prefixed so a human scanning `git log` can tell agent
    checkpoints from real commits at a glance, and so they can be squashed or
    dropped as a group later.
    """

    input_model = GitCheckpointInput

    contract = ToolContract(
        name="git.checkpoint",
        purpose=(
            "Commit the current working tree as a recoverable checkpoint. "
            "Use after a verified step, before attempting the next one."
        ),
        allowed_phases=frozenset({Phase.AUTHOR, Phase.REPAIR, Phase.VERIFY}),
        risk=Risk.LOW,
        reversible=True,
        timeout_seconds=30.0,
        max_output_bytes=4_000,
        produces_evidence=frozenset({EvidenceKind.CHECKPOINT_CREATED}),
        mutating=True,
    )

    PREFIX = "shamsu-checkpoint: "

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace)

    async def run(self, arguments: GitCheckpointInput, cancel: CancellationToken) -> ToolResult:
        started = time.monotonic()
        cancel.raise_if_cancelled()

        if not is_repository(self._workspace):
            return self.failed("not a git repository", started=started)

        status = run_git(self._workspace, "status", "--porcelain")
        if not status.stdout.strip():
            # Not a failure of the tool, but no checkpoint was created — so it
            # must not report CHECKPOINT_CREATED evidence.
            return self.failed("nothing to check point; the working tree is clean", started=started)

        staged = run_git(self._workspace, "add", "-A")
        if not staged.ok:
            return self.failed(f"could not stage changes: {staged.text}", started=started)

        message = f"{self.PREFIX}{arguments.label}"
        committed = run_git(self._workspace, "commit", "-m", message, "--no-verify")
        if not committed.ok:
            return self.failed(f"could not commit: {committed.text}", started=started)

        head = run_git(self._workspace, "rev-parse", "--short", "HEAD")
        return self.ok(
            f"Checkpoint {head.text} created: {arguments.label}\n{committed.stdout.strip()}",
            started=started,
        )


def rollback_to(workspace: Path, ref: str) -> GitOutcome:
    """Hard-reset the working tree to `ref`.

    Not exposed as a model-facing tool. Rollback is a *runtime* decision made
    after a step fails verification; letting the model discard work on its own
    judgement is precisely the autonomy this design withholds.
    """
    return run_git(workspace, "reset", "--hard", ref)


__all__ = [
    "MAX_DIFF_LINES",
    "GitCheckpointInput",
    "GitCheckpointTool",
    "GitInspectInput",
    "GitInspectTool",
    "GitOutcome",
    "is_repository",
    "rollback_to",
    "run_git",
]
