from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.code_edit_workflow import CodeEditWorkflow
from shamsu.agents.task_execution_workflow import TaskExecutionWorkflow
from shamsu.diagnostics.types import ErrorPacket
from shamsu.patch.engine import PatchEngine
from shamsu.taskmaster.service import STATUS_DEFERRED, STATUS_DONE, STATUS_PENDING, TaskmasterService
from shamsu.types import ContextPack, LLMResponse
from tests.test_taskmaster_service import FakeTaskmasterAdapter, _write_prd


class FakeCodeEditResult:
    def __init__(self, applied: bool, changed_files=None, error: str = "") -> None:
        self.applied = applied
        self.changed_files = changed_files or []
        self.error = error


class FakeCodeEditWorkflow:
    def __init__(self, applied: bool = True, changed_files=None, error: str = "") -> None:
        self.applied = applied
        self.changed_files = changed_files or ["app.py"]
        self.error = error
        self.requests: list[str] = []

    async def run(self, request: str) -> FakeCodeEditResult:
        self.requests.append(request)
        return FakeCodeEditResult(self.applied, self.changed_files, self.error)


class FakeCommandRunner:
    def __init__(self, exit_code: int = 0, error_packet=None) -> None:
        self.exit_code = exit_code
        self.last_error_packet = error_packet
        self.calls: list[str] = []

    def run(self, command: str, cwd):
        self.calls.append(command)
        return self.exit_code, "stdout", "stderr"

    def run_tests(self, cwd):
        raise NotImplementedError


class FakeAbstractService:
    def __init__(self) -> None:
        self.mark_stale_calls = 0

    def mark_stale(self) -> None:
        self.mark_stale_calls += 1


class FakeSearch:
    def search(self, query: str, top_k: int = 5, boost_paths=None):
        return []


class SpecialistAwareLLM:
    """Answers "planner" with a fixed plan and "coder" with a valid diff,
    recording every (specialist, pack) call - used to prove the planner step
    genuinely fires end-to-end through TaskExecutionWorkflow ->
    CodeEditWorkflow -> create_plan, not just via the fully-faked
    FakeCodeEditWorkflow used by the other tests in this file."""

    def __init__(self, plan_text: str, diff_text: str) -> None:
        self.plan_text = plan_text
        self.diff_text = diff_text
        self.calls: list[tuple[str, ContextPack]] = []

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        self.calls.append((specialist, pack))
        if specialist == "planner":
            return LLMResponse(raw=self.plan_text, model_used="fake")
        return LLMResponse(raw=self.diff_text, model_used="fake")


def _service_with_two_tasks(tmp_path: Path) -> TaskmasterService:
    adapter = FakeTaskmasterAdapter()
    service = TaskmasterService(tmp_path, adapter=adapter)
    service.parse_prd(_write_prd(tmp_path))
    return service


@pytest.mark.asyncio
async def test_execute_blocks_on_incomplete_dependency(tmp_path):
    service = _service_with_two_tasks(tmp_path)
    code_edit = FakeCodeEditWorkflow()
    workflow = TaskExecutionWorkflow(
        tmp_path, search=FakeSearch(), service=service,
        code_edit_workflow=code_edit, command_runner=FakeCommandRunner(),
    )

    result = await workflow.run("2")

    assert result.status == "blocked"
    assert "1" in result.message
    assert code_edit.requests == []  # never even attempted the coder step
    assert service.show_task("2")["task"].status == "blocked"


@pytest.mark.asyncio
async def test_execute_marks_failed_when_patch_is_not_applied(tmp_path):
    service = _service_with_two_tasks(tmp_path)
    code_edit = FakeCodeEditWorkflow(applied=False, error="diff rejected")
    workflow = TaskExecutionWorkflow(
        tmp_path, search=FakeSearch(), service=service,
        code_edit_workflow=code_edit, command_runner=FakeCommandRunner(),
    )

    result = await workflow.run("1")

    assert result.status == "failed"
    assert "diff rejected" in result.error
    assert service.show_task("1")["task"].status == STATUS_PENDING  # kept retryable, not "done"


@pytest.mark.asyncio
async def test_execute_without_verify_command_applies_but_does_not_mark_done(tmp_path):
    service = _service_with_two_tasks(tmp_path)
    code_edit = FakeCodeEditWorkflow(applied=True)
    workflow = TaskExecutionWorkflow(
        tmp_path, search=FakeSearch(), service=service,
        code_edit_workflow=code_edit, command_runner=FakeCommandRunner(),
    )

    result = await workflow.run("1")

    assert result.status == "applied_unverified"
    assert result.changed_files == ["app.py"]
    assert service.show_task("1")["task"].status != STATUS_DONE


@pytest.mark.asyncio
async def test_execute_marks_done_only_after_verification_passes(tmp_path):
    service = _service_with_two_tasks(tmp_path)
    code_edit = FakeCodeEditWorkflow(applied=True)
    abstract_service = FakeAbstractService()
    workflow = TaskExecutionWorkflow(
        tmp_path, search=FakeSearch(), service=service,
        code_edit_workflow=code_edit, command_runner=FakeCommandRunner(exit_code=0),
        abstract_service=abstract_service,
    )

    result = await workflow.run("1", verify_command="pytest")

    assert result.status == "done"
    assert service.show_task("1")["task"].status == STATUS_DONE
    assert abstract_service.mark_stale_calls == 1  # code memory marked stale after a real mutation


@pytest.mark.asyncio
async def test_execute_does_not_mark_done_when_verification_fails(tmp_path):
    service = _service_with_two_tasks(tmp_path)
    code_edit = FakeCodeEditWorkflow(applied=True)
    packet = ErrorPacket(command="pytest", exit_code=1, summary="2 tests failed")
    workflow = TaskExecutionWorkflow(
        tmp_path, search=FakeSearch(), service=service,
        code_edit_workflow=code_edit, command_runner=FakeCommandRunner(exit_code=1, error_packet=packet),
    )

    result = await workflow.run("1", verify_command="pytest")

    assert result.status == "failed"
    assert "tests failed" in result.error
    assert service.show_task("1")["task"].status == STATUS_PENDING  # not "done"


@pytest.mark.asyncio
async def test_repeated_failures_eventually_defer_instead_of_looping_forever(tmp_path):
    service = _service_with_two_tasks(tmp_path)
    code_edit = FakeCodeEditWorkflow(applied=True)
    workflow = TaskExecutionWorkflow(
        tmp_path, search=FakeSearch(), service=service,
        code_edit_workflow=code_edit,
        command_runner=FakeCommandRunner(exit_code=1, error_packet=ErrorPacket(command="pytest", exit_code=1, summary="boom")),
    )

    for _ in range(3):
        await workflow.run("1", verify_command="pytest")

    assert service.show_task("1")["task"].status == STATUS_DEFERRED


@pytest.mark.asyncio
async def test_execute_reports_error_when_taskmaster_is_unavailable(tmp_path):
    unavailable_adapter = FakeTaskmasterAdapter(available=False)
    service = TaskmasterService(tmp_path, adapter=unavailable_adapter)
    workflow = TaskExecutionWorkflow(
        tmp_path, search=FakeSearch(), service=service,
        code_edit_workflow=FakeCodeEditWorkflow(), command_runner=FakeCommandRunner(),
    )

    result = await workflow.run("1")

    assert result.status == "error"
    assert "/taskmaster setup" in result.error


@pytest.mark.asyncio
async def test_execute_runs_a_real_planner_step_through_code_edit_workflow(tmp_path):
    """Unlike the other tests in this file (which fake the whole
    CodeEditWorkflow), this one wires a real CodeEditWorkflow so the planner
    step added to it is proven to actually fire when reached through
    TaskExecutionWorkflow, not just when CodeEditWorkflow is used directly."""
    (tmp_path / "app.py").write_text("bug = 1\n", encoding="utf-8")
    service = _service_with_two_tasks(tmp_path)
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-bug = 1\n+bug = 2\n"
    llm = SpecialistAwareLLM(plan_text="Edit app.py to fix the bug.", diff_text=diff)
    real_code_edit = CodeEditWorkflow(
        tmp_path, search=FakeSearch(), llm=llm,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    )
    workflow = TaskExecutionWorkflow(
        tmp_path, search=FakeSearch(), service=service,
        code_edit_workflow=real_code_edit, command_runner=FakeCommandRunner(),
    )

    result = await workflow.run("1")

    assert result.status == "applied_unverified"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "bug = 2\n"
    specialists = [specialist for specialist, _ in llm.calls]
    assert specialists == ["planner", "coder"]
