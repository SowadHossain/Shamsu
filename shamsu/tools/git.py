"""
Read-only git helpers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shamsu.interfaces import ICommandRunner
from shamsu.tools.executor import CommandRunner


@dataclass(frozen=True)
class GitStatus:
    is_git_repo: bool
    is_dirty: bool
    changed_files: list[str] = field(default_factory=list)
    raw_output: str = ""
    error: str = ""


class GitTool:
    def __init__(self, workspace_root: Path, command_runner: ICommandRunner | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.command_runner = command_runner or CommandRunner(self.workspace_root)

    def status(self, cwd: Path | None = None) -> GitStatus:
        working_dir = cwd or self.workspace_root
        code, stdout, stderr = self.command_runner.run("git status --short", working_dir)
        if code != 0:
            return GitStatus(
                is_git_repo=False,
                is_dirty=False,
                raw_output=stdout,
                error=stderr,
            )
        changed_files = [
            line[3:].strip() if len(line) > 3 else line.strip()
            for line in stdout.splitlines()
            if line.strip()
        ]
        return GitStatus(
            is_git_repo=True,
            is_dirty=bool(changed_files),
            changed_files=changed_files,
            raw_output=stdout,
        )

    def diff(self, cwd: Path | None = None) -> tuple[bool, str, str]:
        working_dir = cwd or self.workspace_root
        code, stdout, stderr = self.command_runner.run("git diff", working_dir)
        return code == 0, stdout, stderr

    def warn_if_dirty(self, cwd: Path | None = None) -> str | None:
        status = self.status(cwd)
        if not status.is_git_repo:
            return "Workspace is not a git repository."
        if status.is_dirty:
            files = ", ".join(status.changed_files)
            return f"Workspace has uncommitted changes: {files}"
        return None
