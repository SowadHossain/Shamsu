"""Executes one Taskmaster task through SHAMSU's existing safety/tool stack.

Taskmaster owns the task graph, dependencies, and status (see
shamsu/taskmaster/). This workflow owns everything SHAMSU is responsible for
per `agent context/prompts/Taskmaster.md` section 7: dependency gating,
compact context assembly (Codebase-Memory MCP facts via CodeEditWorkflow's
own search+memory brief, Graphiti durable memories), the coder model +
PatchEngine mutation, verification, diagnostics on failure, and Taskmaster
status updates. It never talks to Taskmaster's task file directly and never
writes files itself - CodeEditWorkflow/PatchEngine own that.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from shamsu.abstract.service import AbstractService
from shamsu.agents.code_edit_workflow import CodeEditResult, CodeEditWorkflow
from shamsu.interfaces import ICommandRunner, ISearchAgent
from shamsu.memory.service import MemoryService
from shamsu.patch.types import VerificationOutcome
from shamsu.patch.verifier import run_verification
from shamsu.taskmaster.service import TaskmasterService
from shamsu.taskmaster.types import TaskmasterTask
from shamsu.tools.executor import CommandRunner


class CodeEditWorkflowLike(Protocol):
    async def run(self, request: str) -> CodeEditResult: ...


@dataclass(frozen=True)
class TaskExecutionResult:
    task_id: str
    status: str  # "done" | "failed" | "blocked" | "applied_unverified" | "error"
    message: str = ""
    changed_files: list[str] = field(default_factory=list)
    verification: VerificationOutcome | None = None
    error: str = ""


class TaskExecutionWorkflow:
    def __init__(
        self,
        workspace_root: Path,
        search: ISearchAgent,
        service: TaskmasterService,
        memory_service: MemoryService | None = None,
        abstract_service: AbstractService | None = None,
        code_edit_workflow: CodeEditWorkflowLike | None = None,
        command_runner: ICommandRunner | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.service = service
        self.memory_service = memory_service
        self.abstract_service = abstract_service
        self.code_edit_workflow = code_edit_workflow or CodeEditWorkflow(self.workspace_root, search=search)
        self.command_runner = command_runner or CommandRunner(self.workspace_root)

    async def run(self, task_id: str, verify_command: str = "") -> TaskExecutionResult:
        ready, reason = self.service.ensure_ready()
        if not ready:
            return TaskExecutionResult(task_id, "error", error=reason)

        shown = self.service.show_task(task_id)
        if not shown.get("ok"):
            return TaskExecutionResult(task_id, "error", error=shown.get("error") or f"Task {task_id} not found.")
        task: TaskmasterTask = shown["task"]

        if task.status == "done":
            return TaskExecutionResult(task_id, "done", message="Task is already done.")

        listing = self.service.list_tasks()
        if not listing.get("ok"):
            return TaskExecutionResult(task_id, "error", error=listing.get("error") or "Could not list tasks.")
        incomplete = self.service.incomplete_dependencies(task, listing["tasks"])
        if incomplete:
            reason = f"Waiting on dependency task(s): {', '.join(incomplete)}."
            self.service.mark_blocked(task_id, reason)
            return TaskExecutionResult(task_id, "blocked", message=reason)

        self.service.mark_in_progress(task_id)
        request = self._build_request(task)
        edit_result = await self.code_edit_workflow.run(request)
        if not edit_result.applied:
            error = edit_result.error or "Patch was not applied."
            self.service.mark_failed(task_id, error)
            return TaskExecutionResult(task_id, "failed", error=error, changed_files=edit_result.changed_files)

        if not verify_command.strip():
            return TaskExecutionResult(
                task_id, "applied_unverified",
                message=(
                    "Change applied but not verified (no verification command given). "
                    "Run /tasks execute <id> --verify \"<command>\" to verify, or /tasks mark-done "
                    f"{task_id} to accept it without verification."
                ),
                changed_files=edit_result.changed_files,
            )

        outcome = run_verification(self.command_runner, self.workspace_root, verify_command)
        if outcome.passed:
            self.service.mark_done(task_id, note=f"Verified via: {verify_command}")
            if self.abstract_service is not None:
                self.abstract_service.mark_stale()
            return TaskExecutionResult(task_id, "done", changed_files=edit_result.changed_files, verification=outcome)

        reason = _verification_failure_summary(outcome)
        result = self.service.mark_failed(task_id, reason)
        status_note = f" (retry {result.get('retry_count')}; now {result.get('next_status')})" if result.get("retry_count") else ""
        return TaskExecutionResult(
            task_id, "failed", error=reason + status_note,
            changed_files=edit_result.changed_files, verification=outcome,
        )

    def _build_request(self, task: TaskmasterTask) -> str:
        memory_brief = ""
        if self.memory_service is not None:
            memory_brief = self.memory_service.render_relevant(f"{task.title} {task.description}".strip())
        lines = [
            "You are executing exactly one task from a Taskmaster-generated task graph "
            "as part of SHAMSU's PRD-driven execution. Implement only this task; keep "
            "changes minimal and directly related to it.",
            f"Task {task.id}: {task.title}",
        ]
        if task.description:
            lines.append(f"Description: {task.description}")
        if task.details:
            lines.append(f"Implementation details: {task.details}")
        if task.test_strategy:
            lines.append(f"Acceptance criteria / test strategy: {task.test_strategy}")
        if task.dependencies:
            lines.append(f"Dependencies (already completed): {', '.join(task.dependencies)}")
        if memory_brief:
            lines.append(memory_brief)
        return "\n\n".join(lines)


def _verification_failure_summary(outcome: VerificationOutcome) -> str:
    packet = outcome.error_packet or {}
    summary = packet.get("summary")
    if summary:
        return str(summary)
    return f"Verification command `{outcome.command}` exited {outcome.exit_code}."
