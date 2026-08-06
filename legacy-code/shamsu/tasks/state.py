"""Workspace-local, general-purpose multi-milestone task tracker.

Generalizes the pattern already proven in prd/state.py's Django-specific
GenerationState to any long-running, multi-phase task, per
REQUIREMENTS.md section 20 ("Long-Running Task Support"): phases, blocked
steps with a reason (distinct from a transient FAILED step worth retrying),
files touched, commands run, test results, and a next_action hint.

This is additive, not a replacement for prd/state.py's GenerationState —
the Django pipeline keeps its own state file. generation_state_to_milestone_task
in this module projects that state into a MilestoneTask for unified
`/tasks` visibility without changing how the Django pipeline itself works.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from shamsu.prd.state import GenerationState
from shamsu.safety.sandbox import Sandbox
from shamsu.types import TaskStep, TaskStepStatus, TestFailure, TestRunResult

TASKS_DIRNAME = "tasks"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MilestoneTask:
    task_id: str
    user_request: str
    steps: list[TaskStep]
    phase: str = "default"
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    files_created: list[str] = field(default_factory=list)
    files_edited: list[str] = field(default_factory=list)
    commands_executed: list[dict] = field(default_factory=list)
    test_results: list[TestRunResult] = field(default_factory=list)
    next_action: str = ""

    @property
    def blocked_steps(self) -> list[TaskStep]:
        return [step for step in self.steps if step.status == TaskStepStatus.BLOCKED]

    def next_pending(self) -> TaskStep | None:
        done_ids = {step.id for step in self.steps if step.status == TaskStepStatus.DONE}
        for step in self.steps:
            if step.status != TaskStepStatus.PENDING:
                continue
            if all(dep in done_ids for dep in step.depends_on):
                return step
        return None


def tasks_dir(workspace: Path) -> Path:
    directory = Sandbox(workspace).validate(Path(".shamsu") / TASKS_DIRNAME)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def task_path(workspace: Path, task_id: str) -> Path:
    return tasks_dir(workspace) / f"{task_id}.json"


def create_task(user_request: str, steps: list[TaskStep], phase: str = "default") -> MilestoneTask:
    return MilestoneTask(
        task_id=f"task-{uuid.uuid4().hex[:12]}",
        user_request=user_request,
        steps=steps,
        phase=phase,
    )


def save_task(task: MilestoneTask, workspace: Path) -> Path:
    path = task_path(workspace, task.task_id)
    task.updated_at = _now()
    path.write_text(json.dumps(_task_to_dict(task), indent=2), encoding="utf-8")
    return path


def load_task(workspace: Path, task_id: str) -> MilestoneTask:
    path = task_path(workspace, task_id)
    return _task_from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_task_ids(workspace: Path) -> list[str]:
    directory = tasks_dir(workspace)
    return sorted(p.stem for p in directory.glob("*.json"))


def mark_step_running(task: MilestoneTask, step_id: int) -> MilestoneTask:
    step = _find_step(task, step_id)
    step.status = TaskStepStatus.RUNNING
    step.error = None
    task.next_action = f"Running step {step_id}: {step.description}"
    task.updated_at = _now()
    return task


def mark_step_done(task: MilestoneTask, step_id: int, result: str | None = None) -> MilestoneTask:
    step = _find_step(task, step_id)
    step.status = TaskStepStatus.DONE
    step.error = None
    step.result = result
    task.next_action = ""
    task.updated_at = _now()
    return task


def mark_step_failed(task: MilestoneTask, step_id: int, error: str) -> MilestoneTask:
    step = _find_step(task, step_id)
    step.status = TaskStepStatus.FAILED
    step.error = error
    task.next_action = f"Step {step_id} failed: {error}"
    task.updated_at = _now()
    return task


def mark_step_blocked(task: MilestoneTask, step_id: int, reason: str) -> MilestoneTask:
    """Mark a step as genuinely stuck (needs human input), distinct from a
    transient FAILED step that's still worth retrying automatically."""
    step = _find_step(task, step_id)
    step.status = TaskStepStatus.BLOCKED
    step.error = reason
    task.next_action = f"Blocked on step {step_id}: {reason}"
    task.updated_at = _now()
    return task


def mark_step_skipped(task: MilestoneTask, step_id: int, reason: str) -> MilestoneTask:
    step = _find_step(task, step_id)
    step.status = TaskStepStatus.SKIPPED
    step.error = reason
    task.updated_at = _now()
    return task


def record_file_created(task: MilestoneTask, path: str) -> None:
    if path not in task.files_created:
        task.files_created.append(path)


def record_file_edited(task: MilestoneTask, path: str) -> None:
    if path not in task.files_edited:
        task.files_edited.append(path)


def record_command(task: MilestoneTask, command: str, exit_code: int) -> None:
    task.commands_executed.append({"command": command, "exit_code": exit_code, "at": _now()})


def record_test_result(task: MilestoneTask, result: TestRunResult) -> None:
    task.test_results.append(result)


def advance_phase(
    task: MilestoneTask,
    next_phase: str,
    test_result: TestRunResult | None = None,
) -> bool:
    """Advance to the next phase only if every step in the *current* phase
    is DONE/SKIPPED and (if given) the phase's tests pass. Otherwise leaves
    the task in the current phase with a next_action explaining what's
    outstanding, and returns False — callers should surface that as a
    blocker rather than silently proceeding.
    """
    current_phase_steps = [s for s in task.steps if s.phase == task.phase]
    incomplete = [
        s for s in current_phase_steps
        if s.status not in (TaskStepStatus.DONE, TaskStepStatus.SKIPPED)
    ]
    if incomplete:
        first = incomplete[0]
        task.next_action = (
            f"Cannot advance to '{next_phase}': {len(incomplete)} step(s) in "
            f"'{task.phase}' not yet done (first: step {first.id} - {first.description})."
        )
        task.updated_at = _now()
        return False
    if test_result is not None:
        record_test_result(task, test_result)
        if test_result.failed:
            task.next_action = (
                f"Cannot advance to '{next_phase}': {test_result.failed} test(s) failing in '{task.phase}'."
            )
            task.updated_at = _now()
            return False
    task.phase = next_phase
    task.next_action = ""
    task.updated_at = _now()
    return True


def _find_step(task: MilestoneTask, step_id: int) -> TaskStep:
    for step in task.steps:
        if step.id == step_id:
            return step
    raise ValueError(f"Unknown task step id: {step_id}")


def _task_to_dict(task: MilestoneTask) -> dict:
    data = asdict(task)
    for step in data["steps"]:
        status = step["status"]
        step["status"] = status.value if hasattr(status, "value") else str(status)
    return data


def _task_from_dict(data: dict) -> MilestoneTask:
    steps = [
        TaskStep(
            id=step["id"],
            description=step["description"],
            type=step["type"],
            specialist=step.get("specialist", "coder"),
            status=TaskStepStatus(step.get("status", TaskStepStatus.PENDING)),
            depends_on=step.get("depends_on", []),
            target_file=step.get("target_file"),
            result=step.get("result"),
            error=step.get("error"),
            phase=step.get("phase", "default"),
        )
        for step in data["steps"]
    ]
    test_results = [
        TestRunResult(
            passed=result["passed"],
            failed=result["failed"],
            failures=[TestFailure(**failure) for failure in result.get("failures", [])],
            raw_output=result.get("raw_output", ""),
        )
        for result in data.get("test_results", [])
    ]
    return MilestoneTask(
        task_id=data["task_id"],
        user_request=data["user_request"],
        steps=steps,
        phase=data.get("phase", "default"),
        created_at=data.get("created_at", _now()),
        updated_at=data.get("updated_at", _now()),
        files_created=data.get("files_created", []),
        files_edited=data.get("files_edited", []),
        commands_executed=data.get("commands_executed", []),
        test_results=test_results,
        next_action=data.get("next_action", ""),
    )


def generation_state_to_milestone_task(state: GenerationState) -> MilestoneTask:
    """Project a Django-pipeline GenerationState into a MilestoneTask for
    unified `/tasks` visibility, without changing how GenerationState itself
    is produced/consumed by the Django pipeline. One-way and read-only:
    GenerationState remains authoritative for Django-specific fields.
    """
    steps = [
        TaskStep(
            id=step.id,
            description=f"Generate {step.file.path}",
            type="file_create",
            specialist=step.file.specialist or "coder",
            status=step.status,
            depends_on=[],
            target_file=step.file.path,
            result=None,
            error=step.error,
        )
        for step in state.generation_order
    ]
    return MilestoneTask(
        task_id=state.task_id,
        user_request=f"Generate Django project '{state.project_name}' from {state.prd_path}",
        steps=steps,
        phase="generate",
        created_at=state.created_at,
        updated_at=state.updated_at,
        files_created=list(state.completed_files),
        next_action=state.last_error or "",
    )
