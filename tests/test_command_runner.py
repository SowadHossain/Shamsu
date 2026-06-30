from __future__ import annotations

from pathlib import Path

from shamsu.tools.executor import (
    BLOCKED_EXIT_CODE,
    DENIED_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    WORKSPACE_EXIT_CODE,
    CommandRunner,
)
from shamsu.types import ApprovalRequest, TestRunResult as ShamsuTestRunResult


def test_safe_command_runs_and_captures_stdout(tmp_path: Path):
    runner = CommandRunner(tmp_path)

    code, stdout, stderr = runner.run("python -m pytest --version", tmp_path)

    assert code == 0
    assert "pytest" in stdout.lower()
    assert stderr == ""


def test_blocked_command_is_not_executed(tmp_path: Path):
    def fail_if_called(_request: ApprovalRequest) -> bool:
        raise AssertionError("blocked commands must not ask for approval")

    runner = CommandRunner(tmp_path, approval_func=fail_if_called)

    code, stdout, stderr = runner.run("rm -rf /", tmp_path)

    assert code == BLOCKED_EXIT_CODE
    assert stdout == ""
    assert "Blocked command" in stderr


def test_medium_risk_command_asks_approval_and_denies_cleanly(tmp_path: Path):
    requests: list[ApprovalRequest] = []

    def deny(request: ApprovalRequest) -> bool:
        requests.append(request)
        return False

    runner = CommandRunner(tmp_path, approval_func=deny)

    code, stdout, stderr = runner.run("python -c \"print('nope')\"", tmp_path)

    assert code == DENIED_EXIT_CODE
    assert stdout == ""
    assert "denied" in stderr
    assert requests[0].action_type == "run_command"
    assert requests[0].risk_level == "medium"


def test_medium_risk_command_runs_when_approved(tmp_path: Path):
    requests: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> bool:
        requests.append(request)
        return True

    runner = CommandRunner(tmp_path, approval_func=approve)

    code, stdout, stderr = runner.run("python -c \"print('medium ok')\"", tmp_path)

    assert code == 0
    assert stdout.strip() == "medium ok"
    assert stderr == ""
    assert requests


def test_cwd_outside_workspace_is_rejected(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    runner = CommandRunner(workspace)

    code, stdout, stderr = runner.run("python -m pytest --version", outside)

    assert code == WORKSPACE_EXIT_CODE
    assert stdout == ""
    assert "outside workspace" in stderr


def test_stdout_and_stderr_secrets_are_redacted(tmp_path: Path):
    runner = CommandRunner(tmp_path, approval_func=lambda _request: True)
    command = (
        "python -c \"import sys; "
        "print('SECRET_KEY = \\\"django-insecure-secret\\\"'); "
        "print('password = \\\"abc123\\\"', file=sys.stderr)\""
    )

    code, stdout, stderr = runner.run(command, tmp_path)

    assert code == 0
    assert "[REDACTED]" in stdout
    assert "[REDACTED]" in stderr
    assert "django-insecure-secret" not in stdout
    assert "abc123" not in stderr


def test_timeout_returns_nonzero_without_crashing(tmp_path: Path):
    runner = CommandRunner(
        tmp_path,
        approval_func=lambda _request: True,
        timeout_seconds=0.1,
    )

    code, _stdout, stderr = runner.run(
        "python -c \"import time; time.sleep(2)\"",
        tmp_path,
    )

    assert code == TIMEOUT_EXIT_CODE
    assert "timed out" in stderr


def test_run_tests_returns_passed_and_failed_counts(monkeypatch, tmp_path: Path):
    runner = CommandRunner(tmp_path)

    def fake_run(command: str, cwd: Path) -> tuple[int, str, str]:
        assert command == "python -m pytest tests/ -q"
        assert cwd == tmp_path
        return 1, "F. [100%]\n1 failed, 1 passed in 0.02s", ""

    monkeypatch.setattr(runner, "run", fake_run)

    result = runner.run_tests(tmp_path)

    assert result == ShamsuTestRunResult(
        passed=1,
        failed=1,
        raw_output="F. [100%]\n1 failed, 1 passed in 0.02s",
    )
