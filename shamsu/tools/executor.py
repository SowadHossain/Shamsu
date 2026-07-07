"""
Internal command execution helpers for workspace-bound SHAMSU tools.
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.diagnostics.setup import DiagnosticsWorkspace
from shamsu.diagnostics.types import ErrorPacket
from shamsu.interfaces import ICommandRunner
from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.audit import AuditLogger
from shamsu.safety.commands import classify_command, redact
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.session.manager import SessionLogger
from shamsu.tools.codebase_memory import CodebaseMemoryAdapter
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
        session_logger: SessionLogger | None = None,
        approval_manager: ApprovalManager | None = None,
        diagnostic_digest: DiagnosticDigest | None = None,
        action_ledger: ActionLedger | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.approval_func = approval_func
        self.approval_manager = approval_manager or ApprovalManager(approval_func, session_logger)
        self.timeout_seconds = timeout_seconds
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self.audit_logger = AuditLogger(self.workspace_root)
        self.diagnostic_digest = diagnostic_digest or DiagnosticDigest(
            self.workspace_root, memory_adapter=CodebaseMemoryAdapter()
        )
        self.diagnostics_workspace = DiagnosticsWorkspace(self.workspace_root)
        self.last_error_packet: ErrorPacket | None = None

    def run(self, command: str, cwd: Path) -> tuple[int, str, str]:
        if self.session_logger:
            self.session_logger.log(
                "command.started",
                {"command": command, "cwd": str(cwd)},
                f"Command started: {command}",
                workflow_id="command",
            )
        ledger_cmd_id = self.action_ledger.log_command_start(command, cwd) if self.action_ledger else ""
        try:
            validated_cwd = self._validate_cwd(cwd)
        except (SecurityError, ValueError) as exc:
            if self.session_logger:
                self.session_logger.log(
                    "command.failed",
                    {"command": command, "error": str(exc)},
                    "Command rejected before execution",
                    workflow_id="command",
                )
            self.audit_logger.log("command_run", "error", details={"command": command, "stderr": str(exc)})
            if self.action_ledger:
                self.action_ledger.log_command_finish(ledger_cmd_id, command, cwd, WORKSPACE_EXIT_CODE, "", str(exc))
            return WORKSPACE_EXIT_CODE, "", str(exc)

        risk = classify_command(command)
        if risk == CommandRisk.BLOCKED:
            if self.session_logger:
                self.session_logger.log(
                    "command.blocked",
                    {"command": command, "risk": risk.value},
                    f"Blocked command: {command}",
                    workflow_id="command",
                )
            self.audit_logger.log("command_run", "blocked", details={"command": command, "risk": risk.value})
            if self.action_ledger:
                self.action_ledger.log_command_finish(
                    ledger_cmd_id, command, validated_cwd, BLOCKED_EXIT_CODE, "", f"Blocked command: {command}"
                )
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
            self.approval_manager.session_logger = self.session_logger
            if not self.approval_manager.ask(request):
                if self.session_logger:
                    self.session_logger.log(
                        "command.denied",
                        {"command": command, "cwd": str(validated_cwd)},
                        f"Command denied: {command}",
                        workflow_id="command",
                    )
                self.audit_logger.log(
                    "approval",
                    "denied",
                    details={"action_type": request.action_type, "preview": request.preview},
                )
                self.audit_logger.log("command_run", "denied", details={"command": command, "risk": risk.value})
                if self.action_ledger:
                    self.action_ledger.log_command_finish(
                        ledger_cmd_id, command, validated_cwd, DENIED_EXIT_CODE, "", f"Command denied by user: {command}"
                    )
                return DENIED_EXIT_CODE, "", f"Command denied by user: {command}"
            self.audit_logger.log(
                "approval",
                "approved",
                details={"action_type": request.action_type, "preview": request.preview},
            )

        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=validated_cwd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = redact(_as_text(exc.stdout))
            stderr = redact(_as_text(exc.stderr))
            message = f"Command timed out after {self.timeout_seconds} seconds: {command}"
            if stderr:
                message = f"{message}\n{stderr}"
            if self.session_logger:
                self.session_logger.log(
                    "command.failed",
                    {"command": command, "stdout": stdout, "stderr": message, "exit_code": TIMEOUT_EXIT_CODE},
                    f"Command timed out: {command}",
                    workflow_id="command",
                )
            self.audit_logger.log(
                "command_run",
                "timeout",
                details={"command": command, "stdout": stdout, "stderr": message},
            )
            diagnostics_path = self._run_diagnostics(command, validated_cwd, TIMEOUT_EXIT_CODE, stdout, message)
            if self.action_ledger:
                self.action_ledger.log_command_finish(
                    ledger_cmd_id, command, validated_cwd, TIMEOUT_EXIT_CODE, stdout, message, diagnostics_path
                )
            return TIMEOUT_EXIT_CODE, stdout, message

        result = (
            completed.returncode,
            redact(completed.stdout or ""),
            redact(completed.stderr or ""),
        )
        if self.session_logger:
            self.session_logger.log(
                "command.finished",
                {
                    "command": command,
                    "exit_code": result[0],
                    "stdout": result[1],
                    "stderr": result[2],
                },
                f"Command finished with exit {result[0]}: {command}",
                workflow_id="command",
            )
        self.audit_logger.log(
            "command_run",
            "success" if result[0] == 0 else "error",
            details={"command": command, "exit_code": result[0], "stdout": result[1], "stderr": result[2]},
        )
        diagnostics_path = self._run_diagnostics(command, validated_cwd, result[0], result[1], result[2])
        if self.action_ledger:
            self.action_ledger.log_command_finish(
                ledger_cmd_id, command, validated_cwd, result[0], result[1], result[2], diagnostics_path
            )
        return result

    def _run_diagnostics(self, command: str, cwd: Path, exit_code: int, stdout: str, stderr: str) -> str:
        """Parse this command's output into a compact ErrorPacket *before*
        anything reaches the model - never the other way around. Best-effort:
        a digest failure must never break command execution or hide the raw
        result already returned to the caller. Returns the ActionLedger-relative
        diagnostics path (empty string if there's no ledger or digest failed)."""
        try:
            raw_log_path = str(self.session_logger.events_path) if self.session_logger else ""
            packet = self.diagnostic_digest.run(command, cwd, exit_code, stdout, stderr, raw_log_path=raw_log_path)
            self.last_error_packet = packet
            self.diagnostics_workspace.save_packet(packet.to_dict())
            if self.session_logger:
                self.session_logger.log(
                    "diagnostics.packet",
                    packet.to_dict(),
                    packet.summary or "Diagnostics parsed.",
                    workflow_id="diagnostics",
                )
            if self.action_ledger:
                return self.action_ledger.log_diagnostics(packet.parser_chain, packet.summary, packet.to_dict())
            return ""
        except Exception as exc:  # pragma: no cover - defensive, must never break command execution
            if self.session_logger:
                self.session_logger.log(
                    "diagnostics.error",
                    {"command": command, "error": str(exc)},
                    "DiagnosticDigest failed; raw output remains available in session logs.",
                    workflow_id="diagnostics",
                )
            return ""

    def validate_and_approve(
        self,
        command: str,
        cwd: Path,
        description: str | None = None,
    ) -> tuple[bool, int, str, Path | None]:
        """Validate command/cwd and ask approval when needed without executing.

        Used by detached dev-server launches: the command must pass the same
        safety gates as normal command execution, but SHAMSU must not wait for
        a long-running server process to exit.
        """
        try:
            validated_cwd = self._validate_cwd(cwd)
        except (SecurityError, ValueError) as exc:
            return False, WORKSPACE_EXIT_CODE, str(exc), None

        risk = classify_command(command)
        if risk == CommandRisk.BLOCKED:
            if self.session_logger:
                self.session_logger.log(
                    "command.blocked",
                    {"command": command, "risk": risk.value},
                    f"Blocked command: {command}",
                    workflow_id="command",
                )
            self.audit_logger.log("command_run", "blocked", details={"command": command, "risk": risk.value})
            return False, BLOCKED_EXIT_CODE, f"Blocked command: {command}", validated_cwd

        if risk == CommandRisk.MEDIUM:
            request = ApprovalRequest(
                action_type="run_command",
                description=description or f"Run command: {command}",
                risk_level="medium",
                preview=command,
                working_dir=str(validated_cwd),
                reason="Command is medium risk or unknown.",
            )
            self.approval_manager.session_logger = self.session_logger
            if not self.approval_manager.ask(request):
                if self.session_logger:
                    self.session_logger.log(
                        "command.denied",
                        {"command": command, "cwd": str(validated_cwd)},
                        f"Command denied: {command}",
                        workflow_id="command",
                    )
                self.audit_logger.log("command_run", "denied", details={"command": command, "risk": risk.value})
                return False, DENIED_EXIT_CODE, f"Command denied by user: {command}", validated_cwd
        return True, 0, "Command approved.", validated_cwd

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
