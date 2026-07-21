"""Full PRD-to-project pipeline orchestration.

Routes each PRD by its generation strategy (see registry/suitability.py):
  SCAFFOLD -> copy a template + fill holes from the PRD + verify/repair,
  DJANGO   -> the deterministic Django writer (CRUD/REST),
  FREEFORM -> generate from the PRD with no template (any bespoke project).
Templates are accelerators, never mandatory: a PRD that fits no template is
still built, from the PRD.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from shamsu.agents.bugfix_workflow import BugFixWorkflow
from shamsu.agents.error_feedback_loop import ErrorFeedbackLoop, ErrorFeedbackResult
from shamsu.agents.freeform_generator import FreeformGenerator, FreeformRunResult
from shamsu.agents.scaffold_pipeline import ScaffoldPipeline, ScaffoldRunResult
from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.interfaces import ISearchAgent
from shamsu.llm.manager import LLMManager
from shamsu.prd.input import parse_prd_file
from shamsu.prd.project import build_project_spec
from shamsu.registry import load_registry_entry
from shamsu.registry.scaffold import scaffold_template
from shamsu.registry.schema import Category
from shamsu.registry.suitability import GenerationStrategy, templates_enabled
from shamsu.repair.loop import RepairLoop
from shamsu.repair.proposer_llm import LLMProposer
from shamsu.repair.verifiers import DjangoTestVerifier
from shamsu.safety.approval import ask_approval
from shamsu.safety.sandbox import Sandbox
from shamsu.session.manager import SessionLogger
from shamsu.templates.django.checker import ConsistencyDiagnostic
from shamsu.templates.django.docs import render_pipeline_summary
from shamsu.templates.django.writer import DjangoProjectWriter
from shamsu.tools.django import DjangoSetupResult, DjangoSetupRunner, DjangoTestRunner
from shamsu.types import ProjectSpec, TestRunResult
from shamsu.verify import DoDRunResult, build_prd_checklist, run_dod


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
    prd_checklist: list | None = None


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
        generate: Callable[[str, str, dict], str] | None = None,
        strict_django_repair: bool = False,
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
        # Synchronous (system, user, schema) -> raw JSON model call used by the
        # scaffold/freeform paths for hole-fill and strict repair. None keeps
        # those paths deterministic (scaffold + verify + DoD, no model).
        self.generate = generate
        # Opt-in: drive the Django test-fix step with the strict RepairLoop
        # (rollback, one-error-at-a-time) instead of ErrorFeedbackLoop. Default
        # off so the proven Django flow and its tests are unchanged.
        self.strict_django_repair = strict_django_repair

    async def run(self, prd_path: Path | str, target_dir: Path | str | None = None) -> FullPipelineResult:
        try:
            validated_prd = self._validate_prd(prd_path)
            validated_target = self.sandbox.validate(target_dir or ".")
            parsed = parse_prd_file(validated_prd)
            project = build_project_spec(parsed)
            self._log("workflow.started", {"prd_path": str(validated_prd)}, "Full pipeline started")
            if not project.generation_ready:
                error = f"Generation needs input: {project.clarification_question}"
                self._log(
                    "workflow.needs_input",
                    {"project": project.project_name, "question": project.clarification_question},
                    "Full pipeline needs input before writing files",
                )
                return FullPipelineResult(
                    prd_path=validated_prd,
                    target_dir=validated_target,
                    project=project,
                    written_files=[],
                    diagnostics=[],
                    success=False,
                    error=error,
                )

            strategy = self._strategy(project)
            if strategy is GenerationStrategy.SCAFFOLD:
                return await self._run_scaffold_strategy(validated_prd, validated_target, project)
            if strategy is GenerationStrategy.FREEFORM and self.generate is not None:
                # No template fits: build it from the PRD. With no model wired we
                # fall through to the deterministic writer (keeps old behavior).
                return await self._run_freeform_strategy(validated_prd, validated_target, project)
            if strategy is GenerationStrategy.FREEFORM and not templates_enabled():
                # Templates are disabled and no generator is wired: report that
                # honestly rather than silently copying/writing a template.
                return self._templates_disabled_result(validated_prd, validated_target, project)

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

            if self.strict_django_repair and self.generate is not None and self.feedback_loop is None:
                return await self._run_django_strict_repair(
                    validated_prd, validated_target, project, written_files,
                    diagnostics, setup_result, test_result,
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

    def _strategy(self, project: ProjectSpec) -> GenerationStrategy:
        strategy = self._raw_strategy(project)
        # Templates disabled: build scaffold-eligible projects (games, etc.) from
        # the PRD via the freeform generator instead of copying boilerplate. The
        # deterministic Django writer (DJANGO strategy) is intentionally kept.
        if strategy is GenerationStrategy.SCAFFOLD and not templates_enabled():
            return GenerationStrategy.FREEFORM
        return strategy

    def _raw_strategy(self, project: ProjectSpec) -> GenerationStrategy:
        suitability = getattr(project, "suitability", None)
        if suitability is not None:
            return suitability.strategy
        # Back-compat when a ProjectSpec predates suitability: the old rule was
        # "multiplayer-game category -> scaffold, everything else -> Django".
        if project.category == Category.MULTIPLAYER_GAME.value:
            return GenerationStrategy.SCAFFOLD
        return GenerationStrategy.DJANGO

    def _templates_disabled_result(
        self, prd_path: Path, target_dir: Path, project: ProjectSpec
    ) -> FullPipelineResult:
        """Templates are off and no code generator is wired, so there is nothing
        to build from - never silently fall back to a template. Honest failure."""
        error = (
            "Templates are disabled (SHAMSU_ENABLE_TEMPLATES is not set) and no code "
            "generator is wired, so this project cannot be built from a template. Run the "
            "PRD build from the interactive REPL (which builds from scratch), or set "
            "SHAMSU_ENABLE_TEMPLATES=1 to restore template scaffolding."
        )
        self._log(
            "workflow.failed",
            {"project": project.project_name, "strategy": "freeform", "reason": "templates_disabled"},
            "Template routing disabled and no generator wired",
        )
        return FullPipelineResult(
            prd_path=prd_path,
            target_dir=target_dir,
            project=project,
            success=False,
            error=error,
        )

    async def _run_scaffold_strategy(
        self,
        prd_path: Path,
        target_dir: Path,
        project: ProjectSpec,
    ) -> FullPipelineResult:
        """Scaffold a template, fill its holes from the PRD, then verify + repair.

        Runs the (blocking, build-driving) ScaffoldPipeline in a worker thread so
        the injected sync `generate` can call the model with a plain asyncio.run.
        """
        pipeline = ScaffoldPipeline(
            self.workspace_root,
            generate=self.generate,
            approval_func=self.approval_func,
            session_logger=self.session_logger,
        )
        outcome: ScaffoldRunResult = await asyncio.to_thread(pipeline.run, project, target_dir)
        written = list(outcome.copied_files)
        if outcome.fill_result:
            written += [f for f in outcome.fill_result.changed_files if f not in written]
        result = FullPipelineResult(
            prd_path=prd_path,
            target_dir=outcome.target_dir,
            project=project,
            written_files=written,
            diagnostics=[],
            dod_result=outcome.dod_result,
            preview_url=outcome.preview_url,
            success=outcome.success,
            error=outcome.error,
            prd_checklist=build_prd_checklist(getattr(project, "prd_contract", None), outcome.target_dir),
        )
        event_type = "workflow.finished" if result.success else "workflow.failed"
        self._log(
            event_type,
            {
                "project": project.project_name,
                "strategy": "scaffold",
                "template": outcome.candidate,
                "target_dir": str(outcome.target_dir),
                "filled_holes": outcome.fill_result.filled if outcome.fill_result else [],
                "exit_code": outcome.exit_code,
                "success": result.success,
                "error": result.error,
            },
            "Scaffold pipeline finished" if result.success else "Scaffold pipeline stopped",
        )
        self._write_summary(result)
        return result

    async def _run_django_strict_repair(
        self,
        prd_path: Path,
        target_dir: Path,
        project: ProjectSpec,
        written_files: list[str],
        diagnostics,
        setup_result: DjangoSetupResult,
        test_result: TestRunResult,
    ) -> FullPipelineResult:
        """Drive the Django test-fix step with the strict RepairLoop (rollback,
        one-error-at-a-time, no false success) instead of ErrorFeedbackLoop."""
        from shamsu.tools.django import DJANGO_TEST_COMMAND

        runner = self.test_runner or DjangoTestRunner(
            self.workspace_root, session_logger=self.session_logger
        )
        verifier = DjangoTestVerifier(DJANGO_TEST_COMMAND, runner, target_dir)
        loop = RepairLoop(
            target_dir,
            verifier,
            LLMProposer(self.generate),
            max_attempts=4,
            session_logger=self.session_logger,
            digest=DiagnosticDigest(target_dir),
        )
        outcome = await asyncio.to_thread(loop.run)
        final_test = self.test_runner or runner
        return self._result(
            prd_path,
            target_dir,
            project,
            written_files,
            diagnostics,
            setup_result=setup_result,
            test_result=final_test.run(target_dir),
            success=outcome.success,
            error="" if outcome.success else outcome.final_message,
        )

    async def _run_freeform_strategy(
        self,
        prd_path: Path,
        target_dir: Path,
        project: ProjectSpec,
    ) -> FullPipelineResult:
        """Template-free build: derive a file plan from the PRD, generate each
        file, then verify + strict-repair. Runs in a worker thread so the sync
        `generate` can drive the model."""
        generator = FreeformGenerator(
            self.workspace_root, self.generate, session_logger=self.session_logger
        )
        outcome: FreeformRunResult = await asyncio.to_thread(generator.run, project, target_dir)
        result = FullPipelineResult(
            prd_path=prd_path,
            target_dir=outcome.target_dir,
            project=project,
            written_files=outcome.written_files,
            diagnostics=[],
            success=outcome.success,
            error=outcome.error,
            prd_checklist=build_prd_checklist(getattr(project, "prd_contract", None), outcome.target_dir),
        )
        event_type = "workflow.finished" if result.success else "workflow.failed"
        self._log(
            event_type,
            {
                "project": project.project_name,
                "strategy": "freeform",
                "stack": outcome.stack,
                "target_dir": str(outcome.target_dir),
                "written_files": outcome.written_files,
                "verify_command": outcome.verify_command,
                "exit_code": outcome.exit_code,
                "verified": outcome.verified,
                "success": result.success,
                "error": result.error,
            },
            "Freeform pipeline finished" if result.success else "Freeform pipeline stopped",
        )
        self._write_summary(result)
        return result

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
            llm=LLMManager(session_logger=self.session_logger),
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
