"""Milestone 6: a bounded multi-file task executed step by step.

The exit condition, stated as a property: *a plan step is only ever marked
complete when the evidence table says the work behind it happened.* Everything
here runs against real files, a real git repository, and a real SQLite store —
the only fake is the model, because this box has no GPU and because the point
of v2's architecture is that the runtime holds together without one.

The task deliberately spans two files. A single-file change can be faked by a
lucky patch; a two-step plan where step 2 depends on step 1 cannot.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.fake_model import FakeModelClient

from shamsu.agent.planning import Planner, render_plan_summary, render_step
from shamsu.agent.readonly import ReadOnlyAgent
from shamsu.context.compiler import ContextCompiler, FrameInputs
from shamsu.interfaces.cancellation import NullCancellationToken
from shamsu.interfaces.enums import AgentState, EvidenceKind, Phase, StepOutcome
from shamsu.interfaces.ids import ProjectId, RunId, TaskId
from shamsu.interfaces.tools import ToolPolicyViolation, ToolRequest
from shamsu.models.contracts import ImplementationPlan, PlanStepProposal
from shamsu.state import ProjectRecord, RunRecord, StateStore, TaskRecord, new_id
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git
from shamsu.verification import (
    CompletionGate,
    EvidenceRecorder,
    build_report,
    next_after_completion_gate,
)

pytestmark = pytest.mark.integration

CALC = '''"""Arithmetic."""


def add(a: int, b: int) -> int:
    return a - b
'''

REPORT = '''"""Reporting."""

from calc import add


def total(values: list[int]) -> int:
    result = 0
    for value in values:
        result = add(result, value)
    return result
'''

TESTS = """from calc import add
from report import total


def test_add() -> None:
    assert add(2, 3) == 5


def test_total() -> None:
    assert total([1, 2, 3]) == 6
"""

#: What the model is scripted to propose. Note it asks for *less* than the
#: runtime will require — the floor is what makes that safe.
PLAN_JSON = json.dumps(
    {
        "summary": "Fix addition, then confirm the reporting total follows.",
        "steps": [
            {
                "title": "Correct add() in calc.py",
                "intent": "add() subtracts; it must sum.",
                "files": ["calc.py"],
                "acceptance_criteria": ["add(2, 3) == 5"],
                "required_evidence": ["targeted tests pass"],
                "risk": "low",
            },
            {
                "title": "Confirm total() in report.py aggregates correctly",
                "intent": "total() depends on add(); verify the fix reaches it.",
                "files": ["report.py"],
                "acceptance_criteria": ["total([1, 2, 3]) == 6"],
                "required_evidence": ["the suite is green"],
                "risk": "low",
            },
        ],
        "grounded_in": ["calc.py", "report.py"],
    }
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "calc.py").write_text(CALC, encoding="utf-8")
    (root / "report.py").write_text(REPORT, encoding="utf-8")
    (root / "test_all.py").write_text(TESTS, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    run_git(root, "config", "user.email", "agent@shamsu.local")
    run_git(root, "config", "user.name", "SHAMSU")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-m", "initial", "--no-verify")
    return root


@pytest.fixture
def store() -> StateStore:
    return StateStore(":memory:")


@pytest.fixture
def task(store: StateStore, repo: Path) -> TaskRecord:
    project = store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root=str(repo), name="demo")
    )
    return store.create_task(
        TaskRecord(
            task_id=TaskId(new_id()),
            project_id=project.project_id,
            request="fix addition so the report totals correctly",
        )
    )


@pytest.fixture
def run(store: StateStore, task: TaskRecord) -> RunRecord:
    return store.create_run(
        RunRecord(run_id=RunId(new_id()), project_id=task.project_id, task_id=task.task_id)
    )


def _invoke(gateway: ToolGateway, tool: str, phase: Phase, **arguments: object):
    request = ToolRequest(tool=tool, arguments=arguments)
    with gateway.decision():
        result = asyncio.run(gateway.invoke(request, phase, NullCancellationToken()))
    return request, result


class TestPlannedExecution:
    def test_a_two_step_plan_runs_to_completion_on_evidence_alone(
        self, store: StateStore, task: TaskRecord, run: RunRecord, repo: Path
    ) -> None:
        gateway = ToolGateway(authoring_tools(repo), require_read_before_edit=False)
        recorder = EvidenceRecorder(store, run.run_id, task.task_id)
        planner = Planner(store)

        plan = ImplementationPlan.model_validate_json(PLAN_JSON)
        materialised = planner.create(task, plan, files_seen=("calc.py", "report.py"))

        # The model asked only for tests. The runtime added the floor.
        first = planner.next_step(materialised.plan_id)
        assert first is not None
        assert set(first.required_evidence) == {
            EvidenceKind.FILE_CHANGED,
            EvidenceKind.GIT_DIFF_REVIEWED,
            EvidenceKind.TESTS_PASSED,
        }

        # Nothing has happened yet, so the gate is shut.
        _, before = planner.close_step(first, recorder.verified(first.step_id))
        assert before.satisfied is False
        assert planner.next_step(materialised.plan_id) == first

        planner.begin_step(first)

        request, result = _invoke(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="calc.py",
            find="return a - b",
            replace="return a + b",
        )
        assert result.ok is True
        recorder.record(request, result, Phase.AUTHOR, step_id=first.step_id)

        request, result = _invoke(gateway, "test.run", Phase.VERIFY, command="pytest")
        assert result.ok is True, result.error
        recorder.record(request, result, Phase.VERIFY, step_id=first.step_id)

        request, result = _invoke(gateway, "git.inspect", Phase.VERIFY, what="diff")
        assert result.ok is True
        recorder.record(request, result, Phase.VERIFY, step_id=first.step_id)

        closed, after = planner.close_step(first, recorder.verified(first.step_id))
        assert after.satisfied is True
        assert closed.outcome is StepOutcome.PASS

        # Step 2 is now current, and it is a different step.
        second = planner.next_step(materialised.plan_id)
        assert second is not None and second.step_id != first.step_id

        planner.begin_step(second)
        request, result = _invoke(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="report.py",
            find="    result = 0",
            replace="    result: int = 0",
        )
        recorder.record(request, result, Phase.AUTHOR, step_id=second.step_id)

        request, result = _invoke(gateway, "test.run", Phase.VERIFY, command="pytest")
        assert result.ok is True, result.error
        recorder.record(request, result, Phase.VERIFY, step_id=second.step_id)

        request, result = _invoke(gateway, "git.inspect", Phase.VERIFY, what="diff")
        recorder.record(request, result, Phase.VERIFY, step_id=second.step_id)

        _, final = planner.close_step(second, recorder.verified(second.step_id))
        assert final.satisfied is True

        progress = planner.progress(materialised.plan_id)
        assert progress.done is True
        assert planner.next_step(materialised.plan_id) is None

    def test_evidence_from_one_step_does_not_open_another_steps_gate(
        self, store: StateStore, task: TaskRecord, run: RunRecord, repo: Path
    ) -> None:
        """Evidence is scoped to the step that earned it.

        Without this, a plan's first step could satisfy every later step, and a
        twelve-step plan would complete on one patch.
        """
        gateway = ToolGateway(authoring_tools(repo), require_read_before_edit=False)
        recorder = EvidenceRecorder(store, run.run_id, task.task_id)
        planner = Planner(store)

        materialised = planner.create(task, ImplementationPlan.model_validate_json(PLAN_JSON))
        steps = store.get_steps(materialised.plan_id)

        request, result = _invoke(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="calc.py",
            find="return a - b",
            replace="return a + b",
        )
        recorder.record(request, result, Phase.AUTHOR, step_id=steps[0].step_id)

        assert EvidenceKind.FILE_CHANGED in recorder.verified(steps[0].step_id)
        assert EvidenceKind.FILE_CHANGED not in recorder.verified(steps[1].step_id)

    def test_a_failing_test_run_registers_no_evidence(
        self, store: StateStore, task: TaskRecord, run: RunRecord, repo: Path
    ) -> None:
        """The bug is still there; the gate must stay shut."""
        gateway = ToolGateway(authoring_tools(repo), require_read_before_edit=False)
        recorder = EvidenceRecorder(store, run.run_id, task.task_id)
        planner = Planner(store)

        materialised = planner.create(task, ImplementationPlan.model_validate_json(PLAN_JSON))
        step = planner.next_step(materialised.plan_id)
        assert step is not None

        request, result = _invoke(gateway, "test.run", Phase.VERIFY, command="pytest")
        assert result.ok is False
        recorded = recorder.record(request, result, Phase.VERIFY, step_id=step.step_id)

        assert recorded.produced_evidence is False
        # The event itself is still on the record — a ledger that only
        # remembers successes cannot explain a failure.
        assert store.get_task(task.task_id) is not None
        _, gate = planner.close_step(step, recorder.verified(step.step_id))
        assert gate.satisfied is False
        assert EvidenceKind.TESTS_PASSED in gate.missing

    def test_an_investigate_step_cannot_reach_a_mutating_tool(
        self, store: StateStore, task: TaskRecord, repo: Path
    ) -> None:
        """The discount on evidence is paid for in capability."""
        planner = Planner(store)
        plan = ImplementationPlan(
            summary="Look first.",
            steps=(
                PlanStepProposal(
                    title="Read the calculator", kind="investigate", files=("calc.py",)
                ),
            ),
        )
        # An investigation-shaped request, to match an investigation-only plan.
        # `validate_plan` now refuses a plan that cannot carry out a *change*
        # request, and the shared fixture's "fix addition ..." would trip it —
        # which is a different property from the one under test here.
        looking = store.create_task(
            task.model_copy(update={"task_id": TaskId(new_id()), "request": "show me calc.py"})
        )
        materialised = planner.create(looking, plan)
        step = planner.next_step(materialised.plan_id)
        assert step is not None

        assert step.required_evidence == ()
        assert "file.patch" not in step.allowed_tools

        # And the phase allowlist backs it up independently of the step record.
        gateway = ToolGateway(authoring_tools(repo), require_read_before_edit=False)
        with pytest.raises(ToolPolicyViolation):
            _invoke(gateway, "file.patch", Phase.INSPECT, path="calc.py", find="a", replace="b")


class TestReplanningMidTask:
    def test_a_replan_keeps_finished_work_and_tells_the_next_plan_about_it(
        self, store: StateStore, task: TaskRecord, run: RunRecord, repo: Path
    ) -> None:
        gateway = ToolGateway(authoring_tools(repo), require_read_before_edit=False)
        recorder = EvidenceRecorder(store, run.run_id, task.task_id)
        planner = Planner(store)

        first_plan = planner.create(task, ImplementationPlan.model_validate_json(PLAN_JSON))
        step = planner.next_step(first_plan.plan_id)
        assert step is not None

        for tool, phase, arguments in (
            (
                "file.patch",
                Phase.AUTHOR,
                {"path": "calc.py", "find": "return a - b", "replace": "return a + b"},
            ),
            ("test.run", Phase.VERIFY, {"command": "pytest"}),
            ("git.inspect", Phase.VERIFY, {"what": "diff"}),
        ):
            request, result = _invoke(gateway, tool, phase, **arguments)
            recorder.record(request, result, phase, step_id=step.step_id)

        closed, gate = planner.close_step(step, recorder.verified(step.step_id))
        assert gate.satisfied is True

        current = store.get_task(task.task_id)
        assert current is not None
        completed = planner.completed_titles(first_plan.plan_id)
        assert completed == (step.title,)

        second_plan = planner.replan(
            current,
            ImplementationPlan(
                summary="Different approach to the reporting half.",
                steps=(
                    PlanStepProposal(title="Rewrite total() without add()", files=("calc.py",)),
                ),
            ),
        )

        # The evidence from the finished step is untouched and still keyed to
        # the step that earned it, which is why nothing was copied forward.
        assert EvidenceKind.TESTS_PASSED in recorder.verified(closed.step_id)
        assert planner.progress(second_plan.plan_id).completed == 0
        assert planner.completed_titles(first_plan.plan_id) == completed

    def test_the_replan_prompt_states_what_not_to_redo(self) -> None:
        """A model shown only the failure re-proposes the steps that worked."""
        model = FakeModelClient([PLAN_JSON])
        agent = ReadOnlyAgent(model, ToolGateway([]), ContextCompiler(model))

        plan = asyncio.run(
            agent.replan(
                "fix addition",
                previous_summary="Original plan.",
                completed=("Correct add() in calc.py",),
                reason="the reporting step could not be verified",
            )
        )

        assert plan is not None
        prompt = model.requests[0].messages[0].content
        assert "do NOT repeat" in prompt
        assert "Correct add() in calc.py" in prompt
        assert "the reporting step could not be verified" in prompt

    def test_a_model_that_cannot_produce_a_plan_yields_none(self) -> None:
        """An honest absence, not a fabricated plan."""
        model = FakeModelClient(["not json at all"])
        agent = ReadOnlyAgent(model, ToolGateway([]), ContextCompiler(model))

        plan = asyncio.run(
            agent.replan("fix addition", previous_summary="Original.", reason="it failed")
        )
        assert plan is None


class TestFinalCompletion:
    def test_a_finished_task_completes_and_reports_what_it_actually_did(
        self, store: StateStore, task: TaskRecord, run: RunRecord, repo: Path
    ) -> None:
        """The end of the line: plan → steps → gate → report, from rows only."""
        gateway = ToolGateway(authoring_tools(repo), require_read_before_edit=False)
        recorder = EvidenceRecorder(store, run.run_id, task.task_id)
        planner = Planner(store)

        materialised = planner.create(task, ImplementationPlan.model_validate_json(PLAN_JSON))
        gate = CompletionGate(store, task.task_id)

        # Nothing done yet: the final gate refuses before the first step runs.
        assert gate.check_task(materialised.plan_id).satisfied is False

        edits = (
            {"path": "calc.py", "find": "return a - b", "replace": "return a + b"},
            {"path": "report.py", "find": "    result = 0", "replace": "    result: int = 0"},
        )
        for edit in edits:
            step = planner.next_step(materialised.plan_id)
            assert step is not None
            planner.begin_step(step)

            for tool, phase, arguments in (
                ("file.patch", Phase.AUTHOR, edit),
                ("test.run", Phase.VERIFY, {"command": "pytest"}),
                ("git.inspect", Phase.VERIFY, {"what": "diff"}),
            ):
                request, result = _invoke(gateway, tool, phase, **arguments)
                assert result.ok is True, result.error
                recorder.record(request, result, phase, step_id=step.step_id)

            _, closed = planner.close_step(step, recorder.verified(step.step_id))
            assert closed.satisfied is True

        verdict = gate.check_task(materialised.plan_id)
        assert verdict.satisfied is True
        assert next_after_completion_gate(verdict, can_replan=True) is AgentState.FINAL_REPORT

        report = build_report(store, task.task_id, materialised.plan_id)
        rendered = report.render()

        assert report.changed_files == ("calc.py", "report.py")
        assert "COMPLETE" in rendered and "NOT COMPLETE" not in rendered
        assert "tests_passed via test.run" in rendered
        assert "git_diff_reviewed via git.inspect" in rendered
        assert report.failed_calls == 0

    def test_finishing_one_step_of_two_does_not_finish_the_task(
        self, store: StateStore, task: TaskRecord, run: RunRecord, repo: Path
    ) -> None:
        """A thorough first step satisfies every evidence kind the plan needs."""
        gateway = ToolGateway(authoring_tools(repo), require_read_before_edit=False)
        recorder = EvidenceRecorder(store, run.run_id, task.task_id)
        planner = Planner(store)

        materialised = planner.create(task, ImplementationPlan.model_validate_json(PLAN_JSON))
        step = planner.next_step(materialised.plan_id)
        assert step is not None

        for tool, phase, arguments in (
            (
                "file.patch",
                Phase.AUTHOR,
                {"path": "calc.py", "find": "return a - b", "replace": "return a + b"},
            ),
            ("test.run", Phase.VERIFY, {"command": "pytest"}),
            ("git.inspect", Phase.VERIFY, {"what": "diff"}),
        ):
            request, result = _invoke(gateway, tool, phase, **arguments)
            recorder.record(request, result, phase, step_id=step.step_id)

        planner.close_step(step, recorder.verified(step.step_id))

        # Every required kind now exists at task scope. The gate still refuses.
        assert recorder.verified() >= {
            EvidenceKind.FILE_CHANGED,
            EvidenceKind.GIT_DIFF_REVIEWED,
            EvidenceKind.TESTS_PASSED,
        }
        verdict = CompletionGate(store, task.task_id).check_task(materialised.plan_id)
        assert verdict.satisfied is False
        assert [item.title for item in verdict.unfinished] == [
            "Confirm total() in report.py aggregates correctly"
        ]


class TestPlanEntersTheFrame:
    def test_the_current_step_and_plan_summary_survive_budgeting(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """Both are hot context: a step the model cannot see cannot be executed."""
        model = FakeModelClient()
        planner = Planner(store)
        materialised = planner.create(task, ImplementationPlan.model_validate_json(PLAN_JSON))

        record = store.get_plan(materialised.plan_id)
        steps = store.get_steps(materialised.plan_id)
        assert record is not None

        frame = ContextCompiler(model).compile(
            FrameInputs(
                phase=Phase.AUTHOR,
                task=task.request,
                output_contract="InvestigationStep",
                acceptance_criteria=steps[0].acceptance_criteria,
                current_step=render_step(steps[0]),
                plan_summary=render_plan_summary(record, steps, current=steps[0].step_id),
            ),
            (),
        )

        rendered = frame.render()
        assert "Correct add() in calc.py" in rendered
        assert "add(2, 3) == 5" in rendered
        assert "file_changed" in rendered
        assert "current step" not in frame.dropped_sections
