"""Failure capsules, repair scope, and bounded repair.

Two properties carry this module. A repair must stay inside the files the
failure implicates, and a repair that is not making progress must stop. Both
were absent in v1, and both are the difference between "fixed a bug" and
"rewrote four files and still failed".
"""

from __future__ import annotations

import pytest

from shamsu.agent.repair import RepairController, RepairScope, looks_like_a_test
from shamsu.interfaces.enums import FailureKind, StepOutcome
from shamsu.interfaces.ids import PlanId, ProjectId, StepId, TaskId
from shamsu.runtime.limits import ExecutionLimits
from shamsu.state import (
    PlanRecord,
    PlanStepRecord,
    ProjectRecord,
    StateStore,
    TaskRecord,
    new_id,
)
from shamsu.verification.digest import digest_test_output
from shamsu.verification.failure import (
    FailureCapsule,
    RepairAttempt,
    build_capsule,
    classify_failure,
)

FAILING = """\
=================================== FAILURES ===================================
_________________________________ test_add _____________________________________
calc.py:5: in add
    return a - b
tests/test_calc.py:6: in test_add
    assert add(2, 3) == 5
E   assert -1 == 5
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add - assert -1 == 5
========================= 1 failed in 0.12s ==========================
"""

OTHER_FAILURE = FAILING.replace("assert -1 == 5", "assert 9 == 5").replace("test_add", "test_other")


@pytest.fixture
def store() -> StateStore:
    return StateStore(":memory:")


@pytest.fixture
def task(store: StateStore) -> TaskRecord:
    project = store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root="/workspace", name="demo")
    )
    return store.create_task(
        TaskRecord(task_id=TaskId(new_id()), project_id=project.project_id, request="fix add()")
    )


@pytest.fixture
def step_id(store: StateStore, task: TaskRecord) -> StepId:
    plan = PlanId(new_id())
    identifier = StepId(new_id())
    store.create_plan(
        PlanRecord(plan_id=plan, task_id=task.task_id, version=1, summary="s"),
        [PlanStepRecord(step_id=identifier, plan_id=plan, ordinal=0, title="Fix add()")],
    )
    return identifier


def _digest(text: str = FAILING):
    return digest_test_output(text, "", exit_code=1)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ('  File "x.py", line 3\nSyntaxError: invalid syntax', FailureKind.SYNTAX_ERROR),
            ("ModuleNotFoundError: No module named 'requests'", FailureKind.DEPENDENCY_CONFLICT),
            ("PermissionError: [Errno 13] permission denied", FailureKind.PERMISSION_FAILURE),
            ("ConnectionRefusedError: [Errno 111]", FailureKind.NETWORK_FAILURE),
            ("sqlite3.OperationalError: database is locked", FailureKind.DATABASE_FAILURE),
            ("TypeError: add() takes 2 positional arguments", FailureKind.TYPE_ERROR),
            ("E   AssertionError: nope", FailureKind.TEST_FAILURE),
            ("build failed: 3 errors", FailureKind.BUILD_FAILURE),
        ],
    )
    def test_real_output_is_classified(self, text: str, expected: FailureKind) -> None:
        assert classify_failure(text) is expected

    def test_a_specific_pattern_beats_a_general_one(self) -> None:
        """'ModuleNotFoundError' contains 'Error'; order in the table decides."""
        assert (
            classify_failure("ModuleNotFoundError: no module named 'x'\nAssertionError: x")
            is FailureKind.DEPENDENCY_CONFLICT
        )

    def test_the_fall_through_depends_on_the_tool(self) -> None:
        """Guessing harder from less evidence is how repair fixes the wrong thing."""
        assert classify_failure("something went wrong", tool="test.run") is FailureKind.TEST_FAILURE
        assert classify_failure("something went wrong", tool="git.inspect") is (
            FailureKind.TOOL_FAILURE
        )


# ---------------------------------------------------------------------------
# Capsules
# ---------------------------------------------------------------------------


class TestCapsule:
    def test_a_capsule_is_smaller_than_the_output_it_replaces(self) -> None:
        capsule = build_capsule(_digest())
        assert len(capsule.render()) < len(FAILING)

    def test_it_states_what_was_expected_and_what_happened(self) -> None:
        capsule = build_capsule(_digest())
        assert capsule.expected == "tests/test_calc.py::test_add to pass"
        assert "assert -1 == 5" in capsule.actual

    def test_the_implicated_files_come_from_the_traceback(self) -> None:
        capsule = build_capsule(_digest())
        assert "calc.py" in capsule.implicated_files
        assert "tests/test_calc.py" in capsule.implicated_files

    def test_probes_are_suggestions_not_fixes(self) -> None:
        """The runtime does not know the answer; pretending to would be worse."""
        capsule = build_capsule(_digest())
        assert capsule.kind is FailureKind.TEST_FAILURE
        assert any("Read the failing test" in probe for probe in capsule.probes)

    def test_a_repeat_is_flagged_to_the_model_not_just_the_runtime(self) -> None:
        digest = _digest()
        capsule = build_capsule(
            digest,
            previous_attempts=(
                RepairAttempt(attempt=1, signature=digest.signature, summary="tried it"),
            ),
        )
        assert capsule.repeating is True
        assert "SAME failure" in capsule.render()

    def test_a_different_failure_is_progress_not_a_repeat(self) -> None:
        capsule = build_capsule(
            _digest(),
            previous_attempts=(RepairAttempt(attempt=1, signature="deadbeef", summary="tried"),),
        )
        assert capsule.repeating is False

    def test_the_attempt_number_follows_the_history(self) -> None:
        assert build_capsule(_digest()).attempt == 1
        assert (
            build_capsule(
                _digest(),
                previous_attempts=(
                    RepairAttempt(attempt=1, signature="a", summary=""),
                    RepairAttempt(attempt=2, signature="b", summary=""),
                ),
            ).attempt
            == 3
        )

    def test_the_editable_set_is_frames_plus_what_the_step_changed(self) -> None:
        capsule = build_capsule(_digest(), changed_files=("calc.py", "helpers.py"))
        assert capsule.editable() == ("calc.py", "tests/test_calc.py", "helpers.py")


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestRepairScope:
    @pytest.mark.parametrize(
        "path",
        ["tests/test_calc.py", "test_calc.py", "calc_test.py", "src/x.spec.ts", "conftest.py"],
    )
    def test_test_files_are_recognised(self, path: str) -> None:
        assert looks_like_a_test(path) is True

    @pytest.mark.parametrize("path", ["calc.py", "src/latest/thing.py", "contest.py"])
    def test_source_files_are_not(self, path: str) -> None:
        assert looks_like_a_test(path) is False

    def test_the_failing_test_is_protected_by_default(self) -> None:
        """Editing the failing test is indistinguishable from deleting the evidence."""
        scope = RepairScope.for_capsule(build_capsule(_digest()))
        assert scope.permits("calc.py") is True
        assert scope.permits("tests/test_calc.py") is False
        assert "tests/test_calc.py" in scope.protected

    def test_editing_tests_is_the_callers_decision(self) -> None:
        scope = RepairScope.for_capsule(build_capsule(_digest()), allow_test_edits=True)
        assert scope.permits("tests/test_calc.py") is True

    def test_an_unrelated_file_is_refused(self) -> None:
        """This is the 'broad repository rewrite' plan §20.5 blocks."""
        scope = RepairScope.for_capsule(build_capsule(_digest()))
        assert scope.permits("src/unrelated/module.py") is False
        assert scope.permits("README.md") is False

    def test_path_spellings_do_not_open_a_hole(self) -> None:
        scope = RepairScope.for_capsule(build_capsule(_digest()))
        assert scope.permits("./calc.py") is True
        assert scope.permits("calc.py") is True

    def test_the_description_names_what_may_be_edited(self) -> None:
        """It goes into a refusal message the model has to act on."""
        described = RepairScope.for_capsule(build_capsule(_digest())).describe()
        assert "calc.py" in described
        assert "tests/test_calc.py" in described

    def test_an_empty_scope_says_so(self) -> None:
        empty = RepairScope.for_capsule(
            FailureCapsule(kind=FailureKind.TOOL_FAILURE, signature="x")
        )
        assert empty.allowed == frozenset()
        assert "may not modify any file" in empty.describe()


# ---------------------------------------------------------------------------
# Bounded repair
# ---------------------------------------------------------------------------


class TestRepairController:
    def test_a_first_failure_gets_a_repair_attempt(
        self, store: StateStore, task: TaskRecord, step_id: StepId
    ) -> None:
        decision = RepairController(store, task.task_id).consider(_digest(), step_id=step_id)

        assert decision.proceed is True
        assert decision.outcome is StepOutcome.REPAIRABLE
        assert decision.scope is not None and decision.scope.permits("calc.py")

    def test_the_failure_is_recorded_before_the_decision(
        self, store: StateStore, task: TaskRecord, step_id: StepId
    ) -> None:
        """A run that stops here still leaves an account of why."""
        RepairController(store, task.task_id).consider(_digest(), step_id=step_id)

        failures = store.failures_for(task.task_id, step_id=step_id)
        assert len(failures) == 1
        assert failures[0].kind is FailureKind.TEST_FAILURE
        assert failures[0].attempt == 1
        assert "assert -1 == 5" in failures[0].detail

    def test_the_same_failure_twice_stops(
        self, store: StateStore, task: TaskRecord, step_id: StepId
    ) -> None:
        controller = RepairController(store, task.task_id)
        assert controller.consider(_digest(), step_id=step_id).proceed is True

        second = controller.consider(_digest(), step_id=step_id)
        assert second.proceed is False
        assert second.outcome is StepOutcome.BLOCKED
        assert "will not change it" in second.reason

    def test_a_changing_failure_is_progress(
        self, store: StateStore, task: TaskRecord, step_id: StepId
    ) -> None:
        controller = RepairController(store, task.task_id)
        controller.consider(_digest(), step_id=step_id)

        second = controller.consider(_digest(OTHER_FAILURE), step_id=step_id)
        assert second.proceed is True

    def test_same_failure_detection_survives_a_fresh_controller(
        self, store: StateStore, task: TaskRecord, step_id: StepId
    ) -> None:
        """A resumed run must not hand a stuck step its whole budget again."""
        RepairController(store, task.task_id).consider(_digest(), step_id=step_id)

        resumed = RepairController(store, task.task_id)
        assert resumed.consider(_digest(), step_id=step_id).proceed is False

    def test_the_repair_budget_is_bounded(
        self, store: StateStore, task: TaskRecord, step_id: StepId
    ) -> None:
        controller = RepairController(
            store, task.task_id, limits=ExecutionLimits(repair_attempts_per_step=2)
        )
        # Distinct failures each time, so it is the budget that stops it and
        # not same-failure detection.
        assert controller.consider(_digest(), step_id=step_id).proceed is True
        assert controller.consider(_digest(OTHER_FAILURE), step_id=step_id).proceed is True

        third = controller.consider(
            _digest(FAILING.replace("test_add", "test_third")), step_id=step_id
        )
        assert third.proceed is False
        assert "budget spent" in third.reason

    def test_another_steps_failures_do_not_count_against_this_one(
        self, store: StateStore, task: TaskRecord, step_id: StepId
    ) -> None:
        """A task-wide count would block a fresh step for a neighbour's failures."""
        controller = RepairController(store, task.task_id)
        controller.consider(_digest(), step_id=step_id)
        controller.consider(_digest(OTHER_FAILURE), step_id=step_id)

        other = StepId(new_id())
        assert controller.consider(_digest(), step_id=other).proceed is True

    def test_a_failure_implicating_only_tests_does_not_proceed(
        self, store: StateStore, task: TaskRecord, step_id: StepId
    ) -> None:
        """Widening the scope to have something to do is not an option."""
        only_tests = """\
tests/test_calc.py:6: in test_add
    assert 1 == 2
E   assert 1 == 2
FAILED tests/test_calc.py::test_add - assert 1 == 2
"""
        decision = RepairController(store, task.task_id).consider(
            _digest(only_tests), step_id=step_id
        )
        assert decision.proceed is False
        assert "no editable file" in decision.reason
        assert decision.outcome is StepOutcome.BLOCKED

    def test_the_capsule_carries_the_history_forward(
        self, store: StateStore, task: TaskRecord, step_id: StepId
    ) -> None:
        """The second attempt is cheaper than the first, not identical to it."""
        controller = RepairController(store, task.task_id)
        controller.consider(_digest(), step_id=step_id)

        second = controller.consider(_digest(OTHER_FAILURE), step_id=step_id)
        assert second.capsule is not None
        assert second.capsule.attempt == 2
        assert "Already tried:" in second.capsule.render()
