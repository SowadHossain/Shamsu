"""Approval-backed Django project writer."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from shamsu.prd.state import (
    GenerationState,
    create_generation_state,
    load_generation_state,
    mark_step_done,
    mark_step_failed,
    mark_step_skipped,
    mark_step_running,
    save_generation_state,
    state_path,
)
from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.sandbox import Sandbox
from shamsu.session.manager import SessionLogger
from shamsu.templates.django.checker import BackendConsistencyChecker, ConsistencyDiagnostic
from shamsu.templates.django.frontend_checker import FrontendConsistencyChecker
from shamsu.templates.registry import get_template_provider
from shamsu.types import ApprovalRequest, ProjectSpec


class DjangoProjectWriter:
    def __init__(
        self,
        workspace_root: Path,
        approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
        session_logger: SessionLogger | None = None,
        approval_manager: ApprovalManager | None = None,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.approval_func = approval_func
        self.session_logger = session_logger
        self.approval_manager = approval_manager or ApprovalManager(approval_func, session_logger)

    def write_project(
        self,
        project: ProjectSpec,
        prd_path: Path,
        target_dir: Path | None = None,
    ) -> GenerationState:
        root = self.sandbox.validate(target_dir or ".")
        if root.exists() and not root.is_dir():
            raise ValueError(f"Target is not a directory: {root}")
        root.mkdir(parents=True, exist_ok=True)

        request = ApprovalRequest(
            action_type="file_write",
            description=f"Generate Django project '{project.project_name}' in {root}",
            risk_level="medium",
            preview="\n".join(file.path for file in project.generation_order),
            working_dir=str(root),
            reason="Generate deterministic Django backend files from an approved PRD plan.",
        )
        approved = self.approval_manager.ask(request)
        if not approved:
            raise PermissionError("Django project generation was not approved.")

        state = self._load_or_create_state(project, prd_path)
        self._log("workflow.started", {"project": project.project_name}, "Django generation started")
        contents = get_template_provider(project.archetype).render_all(project)
        for step in state.generation_order:
            if step.status.value == "done":
                continue
            content = contents.get(step.file.path)
            if content is None:
                mark_step_skipped(state, step.id, "Generator is scheduled for a later milestone.")
                save_generation_state(state, self.workspace_root)
                continue
            try:
                mark_step_running(state, step.id)
                self._write_file(root, step.file.path, content)
                mark_step_done(state, step.id)
                self._log(
                    "project.generated",
                    {"file": step.file.path, "generator": step.file.generator},
                    f"Generated {step.file.path}",
                )
            except Exception as exc:
                mark_step_failed(state, step.id, str(exc))
                save_generation_state(state, self.workspace_root)
                self._log(
                    "workflow.failed",
                    {"file": step.file.path, "error": str(exc)},
                    f"Django generation failed at {step.file.path}",
                )
                raise
            save_generation_state(state, self.workspace_root)
        self._log(
            "workflow.finished",
            {"project": project.project_name, "completed_files": state.completed_files},
            "Django generation finished",
        )
        return state

    def check_project(self, project: ProjectSpec, target_dir: Path | None = None) -> list[ConsistencyDiagnostic]:
        root = self.sandbox.validate(target_dir or ".")
        return [
            *BackendConsistencyChecker(root).check(project),
            *FrontendConsistencyChecker(root).check(project),
        ]

    def _load_or_create_state(self, project: ProjectSpec, prd_path: Path) -> GenerationState:
        path = state_path(self.workspace_root)
        if path.exists():
            state = load_generation_state(self.workspace_root)
            if state.project_name == project.project_name and state.app_name == project.app_name:
                return state
        return create_generation_state(project, prd_path, self.workspace_root, accepted=True)

    def _write_file(self, root: Path, relative_path: str, content: str) -> None:
        target = self.sandbox.validate(root / relative_path)
        if target.exists():
            request = ApprovalRequest(
                action_type="file_edit",
                description=f"Overwrite existing generated file: {target}",
                risk_level="medium",
                preview=content[:4000],
                working_dir=str(root),
                reason="The target file already exists.",
            )
            approved = self.approval_manager.ask(request)
            if not approved:
                raise PermissionError(f"Overwrite denied: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def _log(self, event_type: str, payload: dict, summary: str) -> None:
        if self.session_logger:
            self.session_logger.log(event_type, payload, summary, workflow_id="generate-django")
