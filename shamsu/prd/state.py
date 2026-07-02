"""Workspace-local generation plan state for resumable PRD pipelines."""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from shamsu.safety.sandbox import Sandbox
from shamsu.types import DjangoFileSpec, ProjectSpec, TaskStepStatus

STATE_FILENAME = "generation-state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class GenerationStepState:
    id: int
    file: DjangoFileSpec
    status: TaskStepStatus = TaskStepStatus.PENDING
    error: str | None = None


@dataclass
class GenerationState:
    task_id: str
    prd_path: str
    project_name: str
    app_name: str
    generation_order: list[GenerationStepState]
    completed_files: list[str] = field(default_factory=list)
    last_error: str | None = None
    accepted: bool = False
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def next_pending(self) -> GenerationStepState | None:
        done_paths = {
            step.file.path
            for step in self.generation_order
            if step.status == TaskStepStatus.DONE
        }
        for step in self.generation_order:
            if step.status != TaskStepStatus.PENDING:
                continue
            if all(dep in done_paths for dep in step.file.depends_on):
                return step
        return None


def state_path(workspace: Path) -> Path:
    return Sandbox(workspace).validate(Path(".shamsu") / STATE_FILENAME)


def create_generation_state(
    project: ProjectSpec,
    prd_path: Path,
    workspace: Path,
    accepted: bool = False,
) -> GenerationState:
    validated_prd = Sandbox(workspace).validate(prd_path)
    try:
        prd_relative = str(validated_prd.relative_to(Path(workspace).resolve()))
    except ValueError:
        prd_relative = str(validated_prd)
    steps = [
        GenerationStepState(id=index + 1, file=file_spec)
        for index, file_spec in enumerate(project.generation_order)
    ]
    return GenerationState(
        task_id=f"prd-{uuid.uuid4().hex[:12]}",
        prd_path=prd_relative,
        project_name=project.project_name,
        app_name=project.app_name,
        generation_order=steps,
        accepted=accepted,
    )


def save_generation_state(state: GenerationState, workspace: Path) -> Path:
    path = state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    state.updated_at = _now()
    path.write_text(json.dumps(_state_to_dict(state), indent=2), encoding="utf-8")
    return path


def load_generation_state(workspace: Path) -> GenerationState:
    path = state_path(workspace)
    return _state_from_dict(json.loads(path.read_text(encoding="utf-8")))


def reset_generation_state(workspace: Path) -> None:
    state_path(workspace).unlink(missing_ok=True)


def mark_step_running(state: GenerationState, step_id: int) -> GenerationState:
    step = _find_step(state, step_id)
    step.status = TaskStepStatus.RUNNING
    step.error = None
    state.last_error = None
    state.updated_at = _now()
    return state


def mark_step_done(state: GenerationState, step_id: int) -> GenerationState:
    step = _find_step(state, step_id)
    step.status = TaskStepStatus.DONE
    step.error = None
    if step.file.path not in state.completed_files:
        state.completed_files.append(step.file.path)
    state.last_error = None
    state.updated_at = _now()
    return state


def mark_step_failed(state: GenerationState, step_id: int, error: str) -> GenerationState:
    step = _find_step(state, step_id)
    step.status = TaskStepStatus.FAILED
    step.error = error
    state.last_error = error
    state.updated_at = _now()
    return state


def _find_step(state: GenerationState, step_id: int) -> GenerationStepState:
    for step in state.generation_order:
        if step.id == step_id:
            return step
    raise ValueError(f"Unknown generation step id: {step_id}")


def _state_to_dict(state: GenerationState) -> dict:
    data = asdict(state)
    for step in data["generation_order"]:
        status = step["status"]
        step["status"] = status.value if hasattr(status, "value") else str(status)
    return data


def _state_from_dict(data: dict) -> GenerationState:
    steps = [
        GenerationStepState(
            id=step["id"],
            file=DjangoFileSpec(**step["file"]),
            status=TaskStepStatus(step.get("status", TaskStepStatus.PENDING)),
            error=step.get("error"),
        )
        for step in data["generation_order"]
    ]
    return GenerationState(
        task_id=data["task_id"],
        prd_path=data["prd_path"],
        project_name=data["project_name"],
        app_name=data["app_name"],
        generation_order=steps,
        completed_files=data.get("completed_files", []),
        last_error=data.get("last_error"),
        accepted=data.get("accepted", False),
        created_at=data.get("created_at", _now()),
        updated_at=data.get("updated_at", _now()),
    )
