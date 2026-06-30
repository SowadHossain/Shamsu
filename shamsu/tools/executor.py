"""
Internal command execution helpers for workspace-bound SHAMSU tools.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from shamsu.interfaces import ICommandRunner
from shamsu.safety.approval import ask_approval
from shamsu.safety.commands import classify_command, redact
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.types import ApprovalRequest, CommandRisk, TestRunResult

BLOCKED_EXIT_CODE = 126
DENIED_EXIT_CODE = 125
TIMEOUT_EXIT_CODE = 124
WORKSPACE_EXIT_CODE = 127


class CommandRunner(ICommandRunner):
    def __init__(
        self,
        workspace_root: Path,
        approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
        timeout_seconds: int = 120,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.approval_func = approval_func
        self.timeout_seconds = timeout_seconds

    def run(self, command: str, cwd: Path) -> tuple[int, str, str]:
        try:
            validated_cwd = self._validate_cwd(cwd)
        except (SecurityError, ValueError) as exc:
            return WORKSPACE_EXIT_CODE, "", str(exc)

        risk = classify_command(command)
        if risk == CommandRisk.BLOCKED:
            return BLOCKED_EXIT_CODE, "", f"Blocked command: {command}"

        if risk == CommandRisk.MEDIUM:
            request = ApprovalRequest(
                action_type="run_command",
                description=f"Run command: {command}",
                risk_level="medium",
                preview=command,
                working_dir=str(validated_cwd),
                reason="Command is medium risk or unknown.",
            )
            if not self.approval_func(request):
                return DENIED_EXIT_CODE, "", f"Command denied by user: {command}"

        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=validated_cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = redact(_as_text(exc.stdout))
            stderr = redact(_as_text(exc.stderr))
            message = f"Command timed out after {self.timeout_seconds} seconds: {command}"
            if stderr:
                message = f"{message}\n{stderr}"
            return TIMEOUT_EXIT_CODE, stdout, message

        return (
            completed.returncode,
            redact(completed.stdout or ""),
            redact(completed.stderr or ""),
        )

    def run_tests(self, cwd: Path) -> TestRunResult:
        exit_code, stdout, stderr = self.run("python -m pytest tests/ -q", cwd)
        raw_output = "\n".join(part for part in (stdout, stderr) if part)
        passed = _summary_count(raw_output, "passed")
        failed = _summary_count(raw_output, "failed")
        if exit_code != 0 and failed == 0:
            failed = 1
        return TestRunResult(passed=passed, failed=failed, raw_output=raw_output)

    def _validate_cwd(self, cwd: Path) -> Path:
        validated = self.sandbox.validate(cwd)
        if not validated.is_dir():
            raise ValueError(f"Working directory does not exist: {validated}")
        return validated


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _summary_count(output: str, word: str) -> int:
    match = re.search(rf"(\d+)\s+{re.escape(word)}\b", output)
    return int(match.group(1)) if match else 0
