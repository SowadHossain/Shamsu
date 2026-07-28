"""Scaffold-strategy pipeline: copy a template, fill its holes from the PRD,
then verify + strict-repair against the real build.

This is the path that finally makes a template *match the PRD* instead of
shipping its placeholder. It reuses the shared pieces end to end:
  scaffold_template (copy) -> ScaffoldFiller (PRD hole-fill) ->
  CommandVerifier(build) + RepairLoop (strict debug on failure) -> run_dod.

Success is ground truth only: the build exits 0 AND the Definition of Done
passes. With no model available it still scaffolds, verifies once, and runs the
DoD honestly (no PRD adaptation, no false success).
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from shamsu.action_ledger.context import get_current_run
from shamsu.agents.scaffold_filler import FillResult, ScaffoldFiller
from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.registry import load_registry_entry
from shamsu.registry.scaffold import scaffold_template
from shamsu.repair.loop import RepairLoop
from shamsu.repair.prompt import build_final_message
from shamsu.repair.proposer_llm import LLMProposer
from shamsu.repair.types import RepairResult
from shamsu.repair.verifiers import CommandVerifier
from shamsu.safety.approval import ask_approval
from shamsu.session.manager import SessionLogger
from shamsu.tools.executor import CommandRunner
from shamsu.types import ApprovalRequest, ProjectSpec
from shamsu.verify import DoDRunResult, run_dod

_BUILD_TIMEOUT_SECONDS = 600


class GenerateJSON(Protocol):
    def __call__(self, system: str, user: str, schema: dict) -> str: ...


class CommandRunnerLike(Protocol):
    def run(self, command: str, cwd: Path) -> tuple[int, str, str]: ...


@dataclass
class ScaffoldRunResult:
    target_dir: Path
    candidate: str
    copied_files: list[str] = field(default_factory=list)
    fill_result: FillResult | None = None
    repair_result: RepairResult | None = None
    dod_result: DoDRunResult | None = None
    preview_url: str = ""
    exit_code: int = 0
    success: bool = False
    final_message: str = ""
    error: str = ""


class ScaffoldPipeline:
    def __init__(
        self,
        workspace_root: Path,
        *,
        generate: GenerateJSON | None = None,
        command_runner: CommandRunnerLike | None = None,
        approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
        session_logger: SessionLogger | None = None,
        max_repair_attempts: int = 3,
        build_timeout: int = _BUILD_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.generate = generate
        self.command_runner = command_runner
        self.approval_func = approval_func
        self.session_logger = session_logger
        self.max_repair_attempts = max_repair_attempts
        self.build_timeout = build_timeout

    def run(self, project: ProjectSpec, target_dir: Path | str) -> ScaffoldRunResult:
        candidate = self._candidate(project)
        entry = load_registry_entry(candidate)
        scaffold = scaffold_template(
            entry,
            self.workspace_root,
            target_dir,
            approval_func=self.approval_func,
            session_logger=self.session_logger,
        )
        target = scaffold.target_dir

        fill_result: FillResult | None = None
        if self.generate is not None and getattr(project, "prd_contract", None) is not None:
            fill_result = ScaffoldFiller(
                self.workspace_root, self.generate, session_logger=self.session_logger
            ).fill(entry, target, project.prd_contract)

        build_cmd = entry.manifest.build_cmd or "npm run build"
        runner = self.command_runner or CommandRunner(
            self.workspace_root,
            approval_func=lambda _request: True,
            timeout_seconds=self.build_timeout,
            session_logger=self.session_logger,
            action_ledger=get_current_run(),
        )
        verifier = CommandVerifier(build_cmd, runner, target)

        repair_result: RepairResult | None = None
        if self.generate is not None:
            repair_result = RepairLoop(
                target,
                verifier,
                LLMProposer(self.generate),
                max_attempts=self.max_repair_attempts,
                session_logger=self.session_logger,
                digest=DiagnosticDigest(target),
            ).run()
            exit_code = repair_result.exit_code
            final_message = repair_result.final_message
        else:
            run = verifier.run()
            exit_code = run.exit_code
            final_message = build_final_message(exit_code, 0, "")

        dod_result = run_dod(
            entry,
            self.workspace_root,
            target,
            command_runner=runner,  # duck-typed CommandRunner; injected fakes honored in tests
            session_logger=self.session_logger,
        )
        success = exit_code == 0 and dod_result.ok
        error = "" if success else _error_summary(exit_code, dod_result)
        return ScaffoldRunResult(
            target_dir=target,
            candidate=candidate,
            copied_files=scaffold.copied_files,
            fill_result=fill_result,
            repair_result=repair_result,
            dod_result=dod_result,
            preview_url=entry.manifest.preview_url,
            exit_code=exit_code,
            success=success,
            final_message=final_message,
            error=error,
        )

    def _candidate(self, project: ProjectSpec) -> str:
        suitability = getattr(project, "suitability", None)
        if suitability is not None and suitability.candidate:
            return suitability.candidate
        return project.category or ""


def _error_summary(exit_code: int, dod_result: DoDRunResult) -> str:
    if exit_code != 0:
        return f"Build/verify failed (exit code {exit_code})."
    failures = dod_result.required_failures if dod_result else []
    if failures:
        return "Required DoD failed: " + ", ".join(failure.item_id for failure in failures)
    return ""
