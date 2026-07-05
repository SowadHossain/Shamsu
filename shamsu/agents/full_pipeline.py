"""Full PRD-to-Django pipeline orchestration."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shamsu.agents.bugfix_workflow import BugFixWorkflow
from shamsu.agents.error_feedback_loop import ErrorFeedbackLoop, ErrorFeedbackResult
from shamsu.interfaces import ISearchAgent
from shamsu.prd.input import parse_prd_file
from shamsu.prd.project import build_project_spec
from shamsu.registry import load_registry_entry
from shamsu.registry.scaffold import scaffold_template
from shamsu.registry.schema import Category
from shamsu.safety.approval import ask_approval
from shamsu.safety.sandbox import Sandbox
from shamsu.session.manager import SessionLogger
from shamsu.templates.django.checker import ConsistencyDiagnostic
from shamsu.templates.django.docs import render_pipeline_summary
from shamsu.templates.django.writer import DjangoProjectWriter
from shamsu.tools.django import DjangoSetupResult, DjangoSetupRunner, DjangoTestRunner
from shamsu.types import ProjectSpec, TestRunResult
from shamsu.verify import DoDRunResult, run_dod


class SetupRunnerLike(Protocol):
    def run(self, project_cwd: Path | str = ".") -> DjangoSetupResult: ...


class TestRunnerLike(Protocol):
    def run(self, project_cwd: Path | str = ".") -> TestRunResult: ...


class FeedbackLoopLike(Protocol):
    async def run(self, project_cwd: Path | str = ".") -> ErrorFeedbackResult: ...


@dataclass(frozen=True)
class FullPipelineResult:
    prd_path: Path
    target_dir: Path
    project: ProjectSpec | None = None
    written_files: list[str] | None = None
    diagnostics: list[ConsistencyDiagnostic] | None = None
    setup_result: DjangoSetupResult | None = None
    test_result: TestRunResult | None = None
    feedback_result: ErrorFeedbackResult | None = None
    dod_result: DoDRunResult | None = None
    preview_url: str = ""
    success: bool = False
    error: str = ""


class FullDjangoPipeline:
    def __init__(
        self,
        workspace_root: Path,
        search: ISearchAgent,
        approval_func=ask_approval,
        session_logger: SessionLogger | None = None,
        setup_runner: SetupRunnerLike | None = None,
        test_runner: TestRunnerLike | None = None,
        feedback_loop: FeedbackLoopLike | None = None,
        long_running: bool = False,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.search = search
        self.approval_func = approval_func
        self.session_logger = session_logger
        self.setup_runner = setup_runner
        self.test_runner = test_runner
        self.feedback_loop = feedback_loop
        self.long_running = long_running

    async def run(self, prd_path: Path | str, target_dir: Path | str | None = None) -> FullPipelineResult:
        try:
            validated_prd = self._validate_prd(prd_path)
            validated_target = self.sandbox.validate(target_dir or ".")
            parsed = parse_prd_file(validated_prd)
            project = build_project_spec(parsed)
            self._log("workflow.started", {"prd_path": str(validated_prd)}, "Full Django pipeline started")

            if project.category == Category.MULTIPLAYER_GAME.value:
                return self._run_registry_pipeline(validated_prd, validated_target, project)

            writer = DjangoProjectWriter(
                self.workspace_root,
                approval_func=self.approval_func,
                session_logger=self.session_logger,
            )
            state = writer.write_project(project, validated_prd, target_dir=validated_target)
            diagnostics = writer.check_project(project, target_dir=validated_target)
            written_files = [step.file.path for step in state.generation_order if step.status.value == "done"]

            if diagnostics:
                return self._result(
                    validated_prd,
                    validated_target,
                    project,
                    written_files,
                    diagnostics,
                    success=False,
                    error="Generated backend consistency checks reported diagnostics.",
                )

            setup_runner = self.setup_runner or DjangoSetupRunner(
                self.workspace_root,
                session_logger=self.session_logger,
            )
            setup_result = setup_runner.run(validated_target)
            if not setup_result.ok and self.long_running:
                setup_result = await self._retry_setup_via_bugfix(
                    setup_result, setup_runner, validated_target,
                )
            if not setup_result.ok:
                return self._result(
                    validated_prd,
                    validated_target,
                    project,
                    written_files,
                    diagnostics,
                    setup_result=setup_result,
                    success=False,
                    error="Django setup failed.",
                )

            test_result = (self.test_runner or DjangoTestRunner(
                self.workspace_root,
                session_logger=self.session_logger,
            )).run(validated_target)
            if test_result.failed == 0:
                return self._result(
                    validated_prd,
                    validated_target,
                    project,
                    written_files,
                    diagnostics,
                    setup_result=setup_result,
                    test_result=test_result,
                    success=True,
                )

            feedback_result = await (self.feedback_loop or ErrorFeedbackLoop(
                self.workspace_root,
                search=self.search,
                session_logger=self.session_logger,
            )).run(validated_target)
            return self._result(
                validated_prd,
                validated_target,
                project,
                written_files,
                diagnostics,
                setup_result=setup_result,
                test_result=feedback_result.final_result,
                feedback_result=feedback_result,
                success=feedback_result.success,
                error="" if feedback_result.success else feedback_result.error,
            )
        except Exception as exc:
            path = Path(prd_path)
            target = self.sandbox.validate(target_dir or ".")
            self._log("workflow.failed", {"error": str(exc)}, "Full Django pipeline failed")
            return FullPipelineResult(prd_path=path, target_dir=target, success=False, error=str(exc))

    def _validate_prd(self, prd_path: Path | str) -> Path:
        path = self.sandbox.validate(prd_path)
        if not path.exists() or not path.is_file():
            raise ValueError(f"PRD file not found: {path}")
        return path

    async def _retry_setup_via_bugfix(
        self,
        setup_result: DjangoSetupResult,
        setup_runner: SetupRunnerLike,
        target_dir: Path,
    ) -> DjangoSetupResult:
        """Long-running mode only: setup failures (missing migration, a
        syntax error in generated code, ...) are often auto-fixable the same
        way a failing test is, via BugFixWorkflow. One bounded retry, not an
        unbounded loop — if the retry doesn't fix it, the caller still
        reports Django setup failed rather than looping here.
        """
        self._log(
            "workflow.retrying", {"target_dir": str(target_dir)},
            "Django setup failed; attempting one bugfix-and-retry pass",
        )
        fix = await BugFixWorkflow(
            self.workspace_root,
            search=self.search,
            session_logger=self.session_logger,
        ).run(setup_result.bugfix_context)
        if not fix.applied:
            return setup_result
        return setup_runner.run(target_dir)

    def _run_registry_pipeline(
        self,
        prd_path: Path,
        target_dir: Path,
        project: ProjectSpec,
    ) -> FullPipelineResult:
        entry = load_registry_entry(project.category or Category.GENERAL_WEB.value)
        self._log(
            "project.category_selected",
            {
                "category": entry.category.value,
                "confidence": project.archetype_confidence,
                "template": str(entry.root),
            },
            f"Selected project category {entry.category.value}",
        )
        scaffold = scaffold_template(
            entry,
            self.workspace_root,
            target_dir,
            approval_func=self.approval_func,
            session_logger=self.session_logger,
        )
        dod_result = run_dod(
            entry,
            self.workspace_root,
            scaffold.target_dir,
            session_logger=self.session_logger,
        )
        failures = dod_result.required_failures
        error = ""
        if failures:
            error = "Required DoD failed: " + ", ".join(failure.item_id for failure in failures)
        result = FullPipelineResult(
            prd_path=prd_path,
            target_dir=scaffold.target_dir,
            project=project,
            written_files=scaffold.copied_files,
            diagnostics=[],
            dod_result=dod_result,
            preview_url=entry.manifest.preview_url,
            success=not failures,
            error=error,
        )
        event_type = "workflow.finished" if result.success else "workflow.failed"
        self._log(
            event_type,
            {
                "project": project.project_name,
                "category": entry.category.value,
                "target_dir": str(scaffold.target_dir),
                "written_files": scaffold.copied_files,
                "dod_required_failures": [failure.item_id for failure in failures],
                "success": result.success,
                "error": error,
            },
            "Category pipeline finished" if result.success else "Category pipeline stopped",
        )
        self._write_summary(result)
        return result

    def _result(
        self,
        prd_path: Path,
        target_dir: Path,
        project: ProjectSpec,
        written_files: list[str],
        diagnostics: list[ConsistencyDiagnostic],
        setup_result: DjangoSetupResult | None = None,
        test_result: TestRunResult | None = None,
        feedback_result: ErrorFeedbackResult | None = None,
        dod_result: DoDRunResult | None = None,
        preview_url: str = "",
        success: bool = False,
        error: str = "",
    ) -> FullPipelineResult:
        event_type = "workflow.finished" if success else "workflow.failed"
        self._log(
            event_type,
            {
                "project": project.project_name,
                "target_dir": str(target_dir),
                "written_files": written_files,
                "diagnostics": [vars(item) for item in diagnostics],
                "success": success,
                "error": error,
            },
            "Full Django pipeline finished" if success else "Full Django pipeline stopped",
        )
        result = FullPipelineResult(
            prd_path=prd_path,
            target_dir=target_dir,
            project=project,
            written_files=written_files,
            diagnostics=diagnostics,
            setup_result=setup_result,
            test_result=test_result,
            feedback_result=feedback_result,
            dod_result=dod_result,
            preview_url=preview_url,
            success=success,
            error=error,
        )
        self._write_summary(result)
        return result

    def _write_summary(self, result: FullPipelineResult) -> None:
        target = self.sandbox.validate(result.target_dir / "SHAMSU_SUMMARY.md")
        target.write_text(render_pipeline_summary(result), encoding="utf-8")

    def _log(self, event_type: str, payload: dict, summary: str) -> None:
        if self.session_logger:
            self.session_logger.log(event_type, payload, summary, workflow_id="full-django-pipeline")
