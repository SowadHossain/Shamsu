"""Narrow action-tool registry for native Ollama tool calling.

This registry intentionally exposes only approved action tools. Retrieval stays
with Graphiti/Codebase-Memory and is not available here.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.session.manager import SessionLogger
from shamsu.tools.django import DjangoSetupRunner, DjangoTestRunner
from shamsu.tools.executor import CommandRunner
from shamsu.types import ApprovalRequest, ToolDefinition, ToolResult


class ToolRegistry:
    def __init__(
        self,
        workspace_root: Path,
        approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
        session_logger: SessionLogger | None = None,
        approval_manager: ApprovalManager | None = None,
        action_ledger: ActionLedger | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self.approval_manager = approval_manager or ApprovalManager(approval_func, session_logger)
        self.command_runner = command_runner or CommandRunner(
            self.workspace_root,
            approval_func=approval_func,
            session_logger=session_logger,
            approval_manager=self.approval_manager,
            action_ledger=action_ledger,
        )
        self._definitions = {
            item.name: item
            for item in (
                ToolDefinition(
                    name="run_safe_command",
                    description=(
                        "Run a workspace-bound command through SHAMSU safety gates. "
                        "Unsafe commands are blocked or require approval."
                    ),
                    parameters={
                        "command": {"type": "string", "description": "Command to run."},
                        "cwd": {
                            "type": "string",
                            "description": "Relative working directory inside the workspace.",
                            "default": ".",
                        },
                    },
                    required=["command"],
                ),
                ToolDefinition(
                    name="django_setup",
                    description="Run Django setup in a workspace project: install requirements, makemigrations, migrate.",
                    parameters={
                        "project_cwd": {
                            "type": "string",
                            "description": "Relative Django project directory.",
                            "default": ".",
                        }
                    },
                ),
                ToolDefinition(
                    name="django_test",
                    description="Run Django tests for a workspace project with manage.py.",
                    parameters={
                        "project_cwd": {
                            "type": "string",
                            "description": "Relative Django project directory.",
                            "default": ".",
                        }
                    },
                ),
            )
        }

    def definitions(self) -> list[ToolDefinition]:
        return list(self._definitions.values())

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [definition.to_ollama_schema() for definition in self.definitions()]

    def has_tool(self, name: str) -> bool:
        return name in self._definitions

    def validate_arguments(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        definition = self._definitions.get(name)
        if definition is None:
            return ToolResult(False, f"Unknown tool: {name}", {"tool": name})
        if not isinstance(arguments, dict):
            return ToolResult(False, "Tool arguments must be an object.", {"tool": name})
        for required in definition.required:
            if required not in arguments or arguments[required] in (None, ""):
                return ToolResult(False, f"Missing required argument: {required}", {"tool": name})
        for key, value in arguments.items():
            schema = definition.parameters.get(key)
            if schema is None:
                return ToolResult(False, f"Unexpected argument: {key}", {"tool": name})
            expected = schema.get("type")
            if expected == "string" and not isinstance(value, str):
                return ToolResult(False, f"Argument {key} must be a string.", {"tool": name})
        return ToolResult(True, "Arguments valid.", {})

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        validation = self.validate_arguments(name, arguments)
        if not validation.ok:
            return validation
        try:
            if name == "run_safe_command":
                return self.run_safe_command(
                    str(arguments.get("command") or ""),
                    str(arguments.get("cwd") or "."),
                )
            if name == "django_setup":
                return self.django_setup(str(arguments.get("project_cwd") or "."))
            if name == "django_test":
                return self.django_test(str(arguments.get("project_cwd") or "."))
        except Exception as exc:
            return ToolResult(False, str(exc), {"tool": name})
        return ToolResult(False, f"Unknown tool: {name}", {"tool": name})

    def run_safe_command(self, command: str, cwd: str = ".") -> ToolResult:
        if not command.strip():
            return ToolResult(False, "Missing command.", {})
        if _looks_like_arbitrary_python(command):
            return ToolResult(
                False,
                "Blocked arbitrary Python execution.",
                {"exit_code": 126, "stdout": "", "stderr": "Blocked arbitrary Python execution."},
            )
        try:
            validated_cwd = self.sandbox.validate(cwd)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"cwd": cwd})
        code, stdout, stderr = self.command_runner.run(command, validated_cwd)
        data: dict[str, Any] = {"exit_code": code, "stdout": stdout, "stderr": stderr}
        if code != 0 and self.command_runner.last_error_packet is not None:
            data["diagnostics"] = self.command_runner.last_error_packet.to_model_context()
        return ToolResult(code == 0, f"Command exited with {code}.", data)

    def django_setup(self, project_cwd: str = ".") -> ToolResult:
        result = DjangoSetupRunner(
            self.workspace_root,
            command_runner=self.command_runner,
            session_logger=self.session_logger,
        ).run(project_cwd)
        return ToolResult(
            result.ok,
            "Django setup completed." if result.ok else "Django setup failed.",
            {
                "project_cwd": str(result.project_cwd),
                "commands": [
                    {
                        "step": item.step,
                        "command": item.command,
                        "exit_code": item.exit_code,
                        "stdout": item.stdout,
                        "stderr": item.stderr,
                    }
                    for item in result.commands
                ],
                "failures": [
                    {
                        "step": item.step,
                        "command": item.command,
                        "exit_code": item.exit_code,
                        "stdout": item.stdout,
                        "stderr": item.stderr,
                    }
                    for item in result.failures
                ],
            },
        )

    def django_test(self, project_cwd: str = ".") -> ToolResult:
        result = DjangoTestRunner(
            self.workspace_root,
            command_runner=self.command_runner,
            session_logger=self.session_logger,
        ).run(project_cwd)
        return ToolResult(
            result.failed == 0,
            f"Django tests finished: {result.passed} passed, {result.failed} failed.",
            {
                "passed": result.passed,
                "failed": result.failed,
                "failures": [vars(item) for item in result.failures],
                "raw_output": result.raw_output,
            },
        )


def _looks_like_arbitrary_python(command: str) -> bool:
    normalized = command.strip().lower()
    return bool(
        re.search(r"(^|\s)(python|python3|py)(\.exe)?\s+(-c|-(?:\s|$))", normalized)
        or re.search(r"python(\.exe)?[\"']?\s+(-c|-(?:\s|$))", normalized)
    )
