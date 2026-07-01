from __future__ import annotations

import subprocess
from pathlib import Path

from shamsu.tools.git import GitTool


class FakeRunner:
    def __init__(self, response: tuple[int, str, str]) -> None:
        self.response = response
        self.commands: list[str] = []

    def run(self, command: str, cwd: Path) -> tuple[int, str, str]:
        self.commands.append(command)
        return self.response

    def run_tests(self, cwd: Path):  # pragma: no cover - not used by GitTool
        raise NotImplementedError


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_git_status_uses_read_only_status_command(tmp_path):
    runner = FakeRunner((0, " M app.py\n?? new.py\n", ""))

    status = GitTool(tmp_path, command_runner=runner).status()

    assert runner.commands == ["git status --short"]
    assert status.is_git_repo is True
    assert status.is_dirty is True
    assert status.changed_files == ["app.py", "new.py"]


def test_git_diff_uses_read_only_diff_command(tmp_path):
    runner = FakeRunner((0, "diff --git a/app.py b/app.py\n", ""))

    ok, stdout, stderr = GitTool(tmp_path, command_runner=runner).diff()

    assert runner.commands == ["git diff"]
    assert ok is True
    assert "diff --git" in stdout
    assert stderr == ""


def test_non_git_workspace_fails_gracefully(tmp_path):
    status = GitTool(tmp_path).status()

    assert status.is_git_repo is False
    assert status.is_dirty is False
    assert status.error


def test_clean_git_workspace_returns_clean_status(tmp_path):
    _git(tmp_path, "init")

    status = GitTool(tmp_path).status()

    assert status.is_git_repo is True
    assert status.is_dirty is False
    assert status.changed_files == []


def test_dirty_git_workspace_reports_changed_files(tmp_path):
    _git(tmp_path, "init")
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    status = GitTool(tmp_path).status()

    assert status.is_git_repo is True
    assert status.is_dirty is True
    assert status.changed_files == ["app.py"]


def test_git_diff_returns_diff_text(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "shamsu@example.com")
    _git(tmp_path, "config", "user.name", "SHAMSU")
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "initial")
    target.write_text("value = 2\n", encoding="utf-8")

    ok, stdout, stderr = GitTool(tmp_path).diff()

    assert ok is True
    assert "-value = 1" in stdout
    assert "+value = 2" in stdout
    assert stderr == ""
