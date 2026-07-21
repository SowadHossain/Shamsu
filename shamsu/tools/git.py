"""
Git helpers for SHAMSU.

This module provides typed Git operations instead of letting the model invent
raw git commands. Commands still run through CommandRunner, so workspace
validation, approval gates, command risk classification, logging, timeout
handling, and diagnostics remain active.
"""
from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from shamsu.interfaces import ICommandRunner
from shamsu.tools.executor import CommandRunner


@dataclass(frozen=True)
class GitStatus:
    is_git_repo: bool
    is_dirty: bool
    changed_files: list[str] = field(default_factory=list)
    raw_output: str = ""
    error: str = ""


@dataclass(frozen=True)
class GitCommandResult:
    ok: bool
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    message: str = ""

    @property
    def output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()


class GitTool:
    def __init__(self, workspace_root: Path, command_runner: ICommandRunner | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.command_runner = command_runner or CommandRunner(self.workspace_root)

    # -------------------------------------------------------------------------
    # Existing read-only API
    # -------------------------------------------------------------------------

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
        """
        Backwards-compatible diff API.

        Returns:
            (ok, stdout, stderr)
        """
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

    # -------------------------------------------------------------------------
    # Read-only git commands
    # -------------------------------------------------------------------------

    def is_repo(self, cwd: Path | None = None) -> GitCommandResult:
        return self._run_git(["rev-parse", "--is-inside-work-tree"], cwd)

    def status_full(self, cwd: Path | None = None) -> GitCommandResult:
        return self._run_git(["status"], cwd)

    def status_short(self, cwd: Path | None = None) -> GitCommandResult:
        return self._run_git(["status", "--short"], cwd)

    def diff_result(self, cwd: Path | None = None) -> GitCommandResult:
        return self._run_git(["diff"], cwd)

    def diff_staged(self, cwd: Path | None = None) -> GitCommandResult:
        return self._run_git(["diff", "--staged"], cwd)

    def diff_file(self, filepath: str, cwd: Path | None = None) -> GitCommandResult:
        filepath = filepath.strip()
        if not filepath:
            return _failed_result("git diff -- <file>", "Missing filepath.")
        path_error = _validate_path_arg(filepath)
        if path_error:
            return _failed_result("git diff -- <file>", path_error)
        return self._run_git(["diff", "--", filepath], cwd)

    def branch(self, cwd: Path | None = None) -> GitCommandResult:
        return self._run_git(["branch", "--show-current"], cwd)

    def branches(self, all_branches: bool = False, cwd: Path | None = None) -> GitCommandResult:
        args = ["branch"]
        if all_branches:
            args.append("--all")
        return self._run_git(args, cwd)

    def remote(self, cwd: Path | None = None) -> GitCommandResult:
        return self._run_git(["remote", "-v"], cwd)

    def log(self, limit: int = 10, cwd: Path | None = None) -> GitCommandResult:
        limit = max(1, min(int(limit or 10), 100))
        return self._run_git(["log", "--oneline", f"-{limit}"], cwd)

    def unpushed_commits(
        self,
        remote: str = "origin",
        branch: str = "",
        limit: int = 20,
        cwd: Path | None = None,
    ) -> GitCommandResult:
        """
        Show commits that exist locally but not on remote/branch.
        Useful before git_push approval.
        """
        remote = _clean_remote(remote)
        if not remote:
            return _failed_result("git log", "Invalid remote name.")

        branch = branch.strip() or self._current_branch_text(cwd)
        if not branch:
            return _failed_result("git log", "Could not determine current branch.")
        ref_error = _validate_ref_arg(branch)
        if ref_error:
            return _failed_result("git log", ref_error)

        limit = max(1, min(int(limit or 20), 100))
        return self._run_git(["log", "--oneline", f"-{limit}", f"{remote}/{branch}..HEAD"], cwd)

    # -------------------------------------------------------------------------
    # Local mutation git commands
    # -------------------------------------------------------------------------

    def add(self, paths: list[str], cwd: Path | None = None) -> GitCommandResult:
        cleaned, error = _clean_paths(paths)
        if error:
            return _failed_result("git add -- <paths>", error)
        return self._run_git(["add", "--", *cleaned], cwd)

    def add_all(self, cwd: Path | None = None) -> GitCommandResult:
        """
        Stage all tracked/untracked changes.

        Keep this as a typed method so the agent can request it explicitly, but
        expose it behind approval in the registry.
        """
        return self._run_git(["add", "--all"], cwd)

    def restore(self, paths: list[str], staged: bool = False, cwd: Path | None = None) -> GitCommandResult:
        """
        Restore file changes. This can discard local work, so only expose it
        with explicit user approval.
        """
        cleaned, error = _clean_paths(paths)
        if error:
            return _failed_result("git restore -- <paths>", error)
        args = ["restore"]
        if staged:
            args.append("--staged")
        args.extend(["--", *cleaned])
        return self._run_git(args, cwd)

    def commit(self, message: str, cwd: Path | None = None) -> GitCommandResult:
        message = message.strip()
        if not message:
            return _failed_result("git commit -m <message>", "Commit message is required.")
        # A fresh machine often has no git identity, so `git commit` fails with
        # "Please tell me who you are". Set a local identity first so the commit
        # actually lands instead of erroring out.
        self.ensure_identity(cwd)
        return self._run_git(["commit", "-m", message], cwd)

    def init(self, cwd: Path | None = None) -> GitCommandResult:
        """Initialize a git repository in the workspace (idempotent - git init on
        an existing repo is a no-op)."""
        return self._run_git(["init"], cwd)

    def ensure_identity(self, cwd: Path | None = None) -> None:
        """Ensure a git user.name/user.email exists so commits don't fail with
        'Please tell me who you are'. Only sets a *local* default when nothing is
        configured; never overrides an existing identity. Best-effort."""
        working_dir = cwd or self.workspace_root
        try:
            name_code, name_out, _ = self.command_runner.run("git config user.name", working_dir)
            if name_code != 0 or not name_out.strip():
                self.command_runner.run('git config user.name "SHAMSU"', working_dir)
            email_code, email_out, _ = self.command_runner.run("git config user.email", working_dir)
            if email_code != 0 or not email_out.strip():
                self.command_runner.run('git config user.email "shamsu@localhost"', working_dir)
        except Exception:
            pass

    def create_branch(self, branch: str, checkout: bool = True, cwd: Path | None = None) -> GitCommandResult:
        branch = branch.strip()
        ref_error = _validate_ref_arg(branch)
        if ref_error:
            return _failed_result("git checkout -b <branch>", ref_error)

        if checkout:
            return self._run_git(["checkout", "-b", branch], cwd)
        return self._run_git(["branch", branch], cwd)

    def checkout(self, branch: str, cwd: Path | None = None) -> GitCommandResult:
        branch = branch.strip()
        ref_error = _validate_ref_arg(branch)
        if ref_error:
            return _failed_result("git checkout <branch>", ref_error)
        return self._run_git(["checkout", branch], cwd)

    def stash_push(self, message: str = "", include_untracked: bool = False, cwd: Path | None = None) -> GitCommandResult:
        args = ["stash", "push"]
        if include_untracked:
            args.append("--include-untracked")
        if message.strip():
            args.extend(["-m", message.strip()])
        return self._run_git(args, cwd)

    def stash_list(self, cwd: Path | None = None) -> GitCommandResult:
        return self._run_git(["stash", "list"], cwd)

    def stash_pop(self, cwd: Path | None = None) -> GitCommandResult:
        """
        Pops the latest stash. This can modify files, so expose with approval.
        """
        return self._run_git(["stash", "pop"], cwd)

    # -------------------------------------------------------------------------
    # Remote/network git commands
    # -------------------------------------------------------------------------

    def fetch(self, remote: str = "origin", prune: bool = False, cwd: Path | None = None) -> GitCommandResult:
        remote = _clean_remote(remote)
        if not remote:
            return _failed_result("git fetch <remote>", "Invalid remote name.")

        args = ["fetch", remote]
        if prune:
            args.append("--prune")
        return self._run_git(args, cwd)

    def pull(self, remote: str = "origin", branch: str = "", cwd: Path | None = None) -> GitCommandResult:
        remote = _clean_remote(remote)
        if not remote:
            return _failed_result("git pull <remote> [branch]", "Invalid remote name.")

        args = ["pull", remote]
        branch = branch.strip()
        if branch:
            ref_error = _validate_ref_arg(branch)
            if ref_error:
                return _failed_result("git pull <remote> <branch>", ref_error)
            args.append(branch)
        return self._run_git(args, cwd)

    def push(
        self,
        remote: str = "origin",
        branch: str = "",
        set_upstream: bool = False,
        cwd: Path | None = None,
    ) -> GitCommandResult:
        """
        Push current or specified branch.

        Deliberately does NOT support force push. Add that later only as a
        separate, strongly approved method if you really need it.
        """
        remote = _clean_remote(remote)
        if not remote:
            return _failed_result("git push <remote> <branch>", "Invalid remote name.")

        branch = branch.strip() or self._current_branch_text(cwd)
        if not branch:
            return _failed_result("git push", "Could not determine current branch.")

        ref_error = _validate_ref_arg(branch)
        if ref_error:
            return _failed_result("git push <remote> <branch>", ref_error)

        args = ["push"]
        if set_upstream:
            args.append("-u")
        args.extend([remote, branch])
        return self._run_git(args, cwd)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _current_branch_text(self, cwd: Path | None = None) -> str:
        result = self.branch(cwd)
        if not result.ok:
            return ""
        return result.stdout.strip()

    def _run_git(self, args: list[str], cwd: Path | None = None) -> GitCommandResult:
        working_dir = cwd or self.workspace_root
        command = _shell_join(["git", *args])
        code, stdout, stderr = self.command_runner.run(command, working_dir)
        return GitCommandResult(
            ok=code == 0,
            command=command,
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            message=f"Git command exited with {code}.",
        )


def _failed_result(command: str, message: str) -> GitCommandResult:
    return GitCommandResult(
        ok=False,
        command=command,
        exit_code=1,
        stdout="",
        stderr=message,
        message=message,
    )


def _clean_paths(paths: Iterable[str]) -> tuple[list[str], str]:
    cleaned: list[str] = []
    for item in paths:
        path = str(item).strip()
        if not path:
            continue
        error = _validate_path_arg(path)
        if error:
            return [], error
        cleaned.append(path)

    if not cleaned:
        return [], "No paths provided."
    return cleaned, ""


def _validate_path_arg(path: str) -> str:
    """
    Prevent path args from being interpreted as flags.

    The actual workspace containment validation still happens in CommandRunner
    through cwd validation and in higher-level file tools through Sandbox. Git
    itself receives paths after '--'.
    """
    if not path.strip():
        return "Empty path is not allowed."
    if path.strip().startswith("-"):
        return f"Path cannot start with '-': {path}"
    if "\x00" in path:
        return "Path contains a null byte."
    return ""


def _clean_remote(remote: str) -> str:
    remote = str(remote or "origin").strip() or "origin"
    if not remote:
        return ""
    if remote.startswith("-"):
        return ""
    if any(ch.isspace() for ch in remote):
        return ""
    if "\x00" in remote:
        return ""
    return remote


def _validate_ref_arg(ref: str) -> str:
    """
    Conservative ref validation for branch names.

    Git supports more names than this, but the agent should prefer safe common
    branch names. This prevents shell/control weirdness and accidental flags.
    """
    ref = str(ref or "").strip()
    if not ref:
        return "Branch/ref name is required."
    if ref.startswith("-"):
        return f"Branch/ref cannot start with '-': {ref}"
    if "\x00" in ref:
        return "Branch/ref contains a null byte."
    if any(ch.isspace() for ch in ref):
        return f"Branch/ref cannot contain whitespace: {ref}"
    if ".." in ref:
        return f"Branch/ref cannot contain '..': {ref}"
    if "@{" in ref:
        return f"Branch/ref cannot contain '@{{': {ref}"
    if ref.endswith(".lock"):
        return f"Branch/ref cannot end with .lock: {ref}"
    return ""


def _shell_join(parts: list[str]) -> str:
    """
    Build a shell command string for CommandRunner.

    CommandRunner currently accepts a string and uses shell=True, so we quote
    every argument here rather than concatenating raw model-provided text.
    """
    if sys.platform == "win32":
        return subprocess.list2cmdline([str(part) for part in parts])
    return " ".join(shlex.quote(str(part)) for part in parts)