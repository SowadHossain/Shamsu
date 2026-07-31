"""Approval-backed Django project writer."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from shamsu.patch.rollback import rollback_transaction
from shamsu.patch.transactions import TransactionWorkspace
from shamsu.prd.state import (
    GenerationState,
    create_generation_state,
    load_generation_state,
    mark_step_done,
    mark_step_failed,
    mark_step_running,
    mark_step_skipped,
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
        self.transactions = TransactionWorkspace(self.workspace_root)

    def write_project(
        self,
        project: ProjectSpec,
        prd_path: Path,
        target_dir: Path | None = None,
    ) -> GenerationState:
        root = self.sandbox.validate(target_dir or ".")
        if root.exists() and not root.is_dir():
            raise ValueError(f"Target is not a directory: {root}")

        target_paths = [self._workspace_path(root / file.path) for file in project.generation_order]
        request = ApprovalRequest(
            action_type="file_write",
            description=f"Generate Django project '{project.project_name}' in {root}",
            risk_level="medium",
            preview="\n".join(file.path for file in project.generation_order),
            working_dir=str(root),
            reason="Generate deterministic Django backend files from an approved PRD plan.",
            target_paths=target_paths,
        )
        approved = self.approval_manager.ask(request)
        if not approved:
            raise PermissionError("Django project generation was not approved.")

        root.mkdir(parents=True, exist_ok=True)
        state = self._load_or_create_state(project, prd_path, root)
        self._log("workflow.started", {"project": project.project_name}, "Django generation started")
        contents = get_template_provider(project.archetype).render_all(project)
        transaction_id = self.transactions.begin(
            reason=f"Generate Django project {project.project_name}",
            operations=[{"op": "write", "path": path} for path in target_paths],
            destructive=False,
        )
        try:
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
                    self._write_file(root, step.file.path, content, transaction_id)
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
        except BaseException:
            rollback_transaction(self.workspace_root, transaction_id)
            raise
        self.transactions.finalize(transaction_id, "applied")
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

    def _load_or_create_state(
        self,
        project: ProjectSpec,
        prd_path: Path,
        root: Path,
    ) -> GenerationState:
        path = state_path(self.workspace_root)
        if path.exists():
            state = load_generation_state(self.workspace_root)
            target_key = self._target_key(root)
            state_paths = [step.file.path for step in state.generation_order]
            project_paths = [item.path for item in project.generation_order]
            completed_exist = all((root / item).is_file() for item in state.completed_files)
            if (
                state.project_name == project.project_name
                and state.app_name == project.app_name
                and state.target_dir == target_key
                and state_paths == project_paths
                and completed_exist
            ):
                return state
        return create_generation_state(
            project,
            prd_path,
            self.workspace_root,
            accepted=True,
            target_dir=root,
        )

    def _target_key(self, root: Path) -> str:
        try:
            return str(root.relative_to(self.workspace_root)) or "."
        except ValueError:
            return str(root)

    def _write_file(
        self,
        root: Path,
        relative_path: str,
        content: str,
        transaction_id: str,
    ) -> None:
        target = self.sandbox.validate(root / relative_path)
        if target.exists():
            request = ApprovalRequest(
                action_type="file_edit",
                description=f"Overwrite existing generated file: {target}",
                risk_level="medium",
                preview=content[:4000],
                working_dir=str(root),
                reason="The target file already exists.",
                target_paths=[self._workspace_path(target)],
            )
            approved = self.approval_manager.ask(request)
            if not approved:
                raise PermissionError(f"Overwrite denied: {target}")
        workspace_path = self._workspace_path(target)
        self.transactions.backup_file(transaction_id, workspace_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.transactions.record_after(transaction_id, workspace_path)

    def _workspace_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.workspace_root).as_posix()

    def _log(self, event_type: str, payload: dict, summary: str) -> None:
        if self.session_logger:
            self.session_logger.log(event_type, payload, summary, workflow_id="generate-django")
