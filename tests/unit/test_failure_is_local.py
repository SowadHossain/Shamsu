"""One failed step must not end the task.

`StepOutcome.BLOCKED` used to map straight onto the terminal `BLOCKED` state,
so the first unprovable step ended the run with every later step unattempted.
The arithmetic of that is what makes it worth a test file: a five-step plan at
85% per step finishes 44% of the time, and a ten-step plan 20% — and the steps
thrown away are frequently unrelated to whatever went wrong.

Three properties are asserted here, and the third is the one that keeps the
change honest:

1. A step that fails takes its *dependents* down and nothing else.
2. Work that already passed keeps its outcome and its evidence.
3. The task still cannot be reported complete. Continuing past a failure buys a
   fuller attempt and a fuller report — never a weaker gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agent.planning import Planner, materialise
from shamsu.interfaces.enums import AgentState, EvidenceKind, StepOutcome
from shamsu.interfaces.ids import ProjectId, TaskId
from shamsu.models.contracts import ImplementationPlan, PlanStepProposal
from shamsu.state.records import ProjectRecord, TaskRecord, new_id
from shamsu.state.store import StateStore
from shamsu.state.transitions import can_transition, next_after_verification


def _plan() -> ImplementationPlan:
    """Four steps whose dependencies are *derivable* from the files they name.

    1 and 2 share `a.py`; 2 and 3 share `b.py`; 4 shares nothing. So failing 1
    must take 2 and 3 with it — transitively — and leave 4 alone. Written this
    way because the model is not asked for dependencies: it emitted
    `"depends_on": [0, 1, 2, ... 74` on a live PRD and destroyed the JSON around
    it, so the field is derived from file overlap instead.

    The file tuples differ at every step, so `coalesce_by_file` does not merge
    any of them and the four stay four.
    """
    return ImplementationPlan(
        summary="four steps, one of them independent",
        steps=(
            PlanStepProposal(title="Add the parser", files=("a.py",)),
            PlanStepProposal(title="Wire the parser in", files=("a.py", "b.py")),
            PlanStepProposal(title="Add parser tests", files=("b.py",)),
            PlanStepProposal(title="Update the changelog", files=("CHANGELOG.md",)),
        ),
    )


@pytest.fixture
def planner(tmp_path: Path) -> tuple[Planner, StateStore, TaskRecord]:
    store = StateStore(tmp_path / "state.db")
    project = store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root=str(tmp_path), name="p")
    )
    task = store.create_task(
        TaskRecord(task_id=TaskId(new_id()), project_id=project.project_id, request="do it")
    )
    return Planner(store), store, task


class TestDependenciesSurviveMaterialisation:
    def test_dependencies_are_derived_from_shared_files(self) -> None:
        built = materialise(TaskId("t"), _plan())
        assert built.steps[0].depends_on == ()
        assert built.steps[1].depends_on == (1,), "step 2 shares a.py with step 1"
        assert built.steps[2].depends_on == (2,), "step 3 shares b.py with step 2"
        assert built.steps[3].depends_on == (), "the changelog shares nothing"

    def test_dependencies_only_ever_point_backwards(self) -> None:
        """A step cannot need one that has not run yet."""
        built = materialise(TaskId("t"), _plan())
        for step in built.steps:
            assert all(position <= step.ordinal for position in step.depends_on)

    def test_a_step_naming_no_files_is_independent(self) -> None:
        plan = ImplementationPlan(
            summary="s",
            steps=(
                PlanStepProposal(title="Add a thing", files=("a.py",)),
                PlanStepProposal(title="Review the result", kind="investigate"),
            ),
        )
        assert materialise(TaskId("t"), plan).steps[1].depends_on == ()

    def test_dependencies_are_computed_after_merging(self) -> None:
        """Positions must refer to the *merged* plan, not the proposed one.

        Steps 2 and 3 target one file and become one step, so anything derived
        afterwards has to be numbered against what actually executes.
        """
        plan = ImplementationPlan(
            summary="s",
            steps=(
                PlanStepProposal(title="Read the spec", kind="investigate"),
                PlanStepProposal(title="Create tasks.py", files=("tasks.py",)),
                PlanStepProposal(title="Add the add method", files=("tasks.py",)),
                PlanStepProposal(title="Update tasks.py docs", files=("tasks.py", "README.md")),
            ),
        )
        built = materialise(TaskId("t"), plan)
        assert len(built.steps) == 3, "steps 2 and 3 target one file and should merge"
        assert built.steps[2].depends_on == (2,), "the merged step is position 2"


class TestSkippingIsTransitive:
    def test_only_dependents_are_skipped(
        self, planner: tuple[Planner, StateStore, TaskRecord]
    ) -> None:
        agent, store, task = planner
        built = agent.create(task, _plan())

        # The order `_abandon_step` uses: close the step that failed, then take
        # down what needed it. `skip_dependents` deliberately does not close
        # its argument — the caller knows *why* it failed and that reason
        # belongs in the outcome.
        agent.fail_step(built.steps[0], StepOutcome.BLOCKED)
        skipped = agent.skip_dependents(built.steps[0])

        assert [step.title for step in skipped] == ["Wire the parser in", "Add parser tests"], (
            "3 needs 2 which needs 1, so both are unreachable"
        )
        remaining = agent.next_step(built.plan_id)
        assert remaining is not None
        assert remaining.title == "Update the changelog", "the independent step must survive"

    def test_a_passed_step_is_never_skipped(
        self, planner: tuple[Planner, StateStore, TaskRecord]
    ) -> None:
        """Proven work stays proven, whatever fails afterwards."""
        agent, store, task = planner
        built = agent.create(task, _plan())

        closed, _ = agent.close_step(built.steps[1], frozenset(built.steps[1].required_evidence))
        assert closed.outcome is StepOutcome.PASS

        agent.skip_dependents(built.steps[0])

        after = store.get_step(built.steps[1].step_id)
        assert after is not None
        assert after.outcome is StepOutcome.PASS

    def test_an_independent_failure_skips_nothing(
        self, planner: tuple[Planner, StateStore, TaskRecord]
    ) -> None:
        agent, _store, task = planner
        built = agent.create(task, _plan())
        agent.fail_step(built.steps[3], StepOutcome.BLOCKED)
        assert agent.skip_dependents(built.steps[3]) == ()


class TestTheMachineAllowsIt:
    def test_verify_may_move_on_to_the_next_step(self) -> None:
        assert can_transition(AgentState.VERIFY_CURRENT_STEP, AgentState.CHECK_REMAINING_STEPS), (
            "a failed step has to be able to hand off to whatever is still worth trying"
        )

    def test_an_exhausted_repair_may_move_on_too(self) -> None:
        assert can_transition(AgentState.REPAIR, AgentState.CHECK_REMAINING_STEPS)

    def test_a_skipped_step_does_not_terminate(self) -> None:
        assert next_after_verification(StepOutcome.SKIPPED) is AgentState.CHECK_REMAINING_STEPS

    def test_blocked_is_still_reachable(self) -> None:
        """Continuing is a choice the runtime makes, not one the table forces.

        When nothing independent remains there is no honest successor but
        BLOCKED, so the edge has to stay.
        """
        assert next_after_verification(StepOutcome.BLOCKED) is AgentState.BLOCKED


class TestTheGateIsUnchanged:
    def test_a_task_with_a_failed_step_cannot_complete(
        self, planner: tuple[Planner, StateStore, TaskRecord]
    ) -> None:
        """The whole point of continuing is a fuller report, not a weaker gate."""
        from shamsu.verification.completion import CompletionGate

        agent, store, task = planner
        built = agent.create(task, _plan())

        agent.fail_step(built.steps[0], StepOutcome.BLOCKED)
        agent.skip_dependents(built.steps[0])
        agent.close_step(built.steps[3], frozenset(built.steps[3].required_evidence))

        verdict = CompletionGate(store, task.task_id).check_task(built.plan_id)
        assert not verdict.satisfied
        assert len(verdict.unfinished) == 3

    def test_a_skipped_step_still_carries_its_requirements(
        self, planner: tuple[Planner, StateStore, TaskRecord]
    ) -> None:
        """Skipping records that work did not happen; it does not excuse it."""
        agent, store, task = planner
        built = agent.create(task, _plan())
        agent.skip_dependents(built.steps[0])

        skipped = store.get_step(built.steps[1].step_id)
        assert skipped is not None
        assert skipped.outcome is StepOutcome.SKIPPED
        assert EvidenceKind.FILE_CHANGED in skipped.required_evidence
