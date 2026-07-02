"""Generated-project test feedback loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from shamsu.agents.bugfix_workflow import BugFixResult, BugFixWorkflow
from shamsu.interfaces import ILLMManager, IPatchEngine, ISearchAgent
from shamsu.session.manager import SessionLogger
from shamsu.tools.django import DjangoTestRunner
from shamsu.types import TestRunResult


class DjangoTestRunnerLike(Protocol):
    def run(self, project_cwd: Path | str = ".") -> TestRunResult: ...


class BugFixWorkflowLike(Protocol):
    async def run(self, report: str) -> BugFixResult: ...


@dataclass(frozen=True)
class FeedbackIteration:
    index: int
    test_result: TestRunResult
    fix_applied: bool = False
    changed_files: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class ErrorFeedbackResult:
    success: bool
    iterations: list[FeedbackIteration]
    final_result: TestRunResult
    error: str = ""


class ErrorFeedbackLoop:
    def __init__(
        self,
        workspace_root: Path,
        search: ISearchAgent,
        llm: ILLMManager | None = None,
        patch_engine: IPatchEngine | None = None,
        test_runner: DjangoTestRunnerLike | None = None,
        bugfix_workflow: BugFixWorkflowLike | None = None,
        session_logger: SessionLogger | None = None,
        max_iterations: int = 3,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.search = search
        self.test_runner = test_runner or DjangoTestRunner(
            self.workspace_root,
            session_logger=session_logger,
        )
        self.bugfix_workflow = bugfix_workflow or BugFixWorkflow(
            self.workspace_root,
            search=search,
            llm=llm,
            patch_engine=patch_engine,
        )
        self.session_logger = session_logger
        self.max_iterations = max_iterations

    async def run(self, project_cwd: Path | str = ".") -> ErrorFeedbackResult:
        iterations: list[FeedbackIteration] = []
        final_result = self.test_runner.run(project_cwd)
        if final_result.failed == 0:
            self._log("workflow.finished", final_result, "Django tests passed before fixes")
            return ErrorFeedbackResult(success=True, iterations=[], final_result=final_result)

        for index in range(1, self.max_iterations + 1):
            bug_report = _bug_report_from_tests(final_result, index)
            fix = await self.bugfix_workflow.run(bug_report)
            iterations.append(
                FeedbackIteration(
                    index=index,
                    test_result=final_result,
                    fix_applied=fix.applied,
                    changed_files=fix.changed_files,
                    error=fix.error,
                )
            )
            if not fix.applied:
                self._log("workflow.failed", final_result, fix.error or "Bug-fix patch was not applied")
                return ErrorFeedbackResult(
                    success=False,
                    iterations=iterations,
                    final_result=final_result,
                    error=fix.error or "Bug-fix patch was not applied.",
                )
            final_result = self.test_runner.run(project_cwd)
            if final_result.failed == 0:
                self._log("workflow.finished", final_result, "Django tests passed after fixes")
                return ErrorFeedbackResult(
                    success=True,
                    iterations=iterations,
                    final_result=final_result,
                )

        self._log("workflow.failed", final_result, "Django tests still failing after retries")
        return ErrorFeedbackResult(
            success=False,
            iterations=iterations,
            final_result=final_result,
            error=f"Django tests still failing after {self.max_iterations} fix attempts.",
        )

    def _log(self, event_type: str, result: TestRunResult, summary: str) -> None:
        if not self.session_logger:
            return
        self.session_logger.log(
            event_type,
            {"passed": result.passed, "failed": result.failed, "failures": [vars(f) for f in result.failures]},
            summary,
            workflow_id="error-feedback-loop",
        )


def _bug_report_from_tests(result: TestRunResult, iteration: int) -> str:
    failures = "\n".join(
        f"- {failure.test_name} {failure.file}:{failure.line or ''} {failure.error_message}"
        for failure in result.failures
    )
    return (
        f"Django test feedback iteration {iteration} failed.\n"
        f"Passed: {result.passed}\n"
        f"Failed: {result.failed}\n\n"
        f"Structured failures:\n{failures or 'No structured failures parsed.'}\n\n"
        f"Raw output:\n{result.raw_output}"
    )
