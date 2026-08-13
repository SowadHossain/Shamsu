"""Repair when the shortfall is evidence, not a failing test.

`_verify_step` used to mark a step REPAIRABLE only when `context.last_digest`
was set — and that is populated by `test.run` alone. So a step that fell short
for any other reason went straight to BLOCKED with its whole repair budget
unspent.

The §31.1 evaluation showed the cost. A run fixed `add()` correctly, registered
`file_changed`, and stopped one `git.inspect` call short of `git_diff_reviewed`
— the work was done, the proof was one call away, and there was no path to make
it.
"""

from __future__ import annotations

import pytest

from shamsu.agent.repair import RepairController
from shamsu.interfaces.enums import EvidenceKind, FailureKind, StepOutcome
from shamsu.interfaces.ids import ProjectId, StepId, TaskId
from shamsu.runtime.limits import ExecutionLimits
from shamsu.state.records import ProjectRecord, TaskRecord, new_id
from shamsu.state.store import StateStore
from shamsu.verification.failure import evidence_capsule

PRODUCERS = {
    EvidenceKind.GIT_DIFF_REVIEWED: ["git.inspect"],
    EvidenceKind.FILE_CHANGED: ["file.patch"],
    EvidenceKind.TESTS_PASSED: ["test.run"],
}


@pytest.fixture
def store() -> StateStore:
    return StateStore(":memory:")


@pytest.fixture
def task(store: StateStore) -> TaskRecord:
    project = store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root="/tmp/x", name="demo")
    )
    return store.create_task(
        TaskRecord(
            task_id=TaskId(new_id()),
            project_id=project.project_id,
            request="fix add()",
        )
    )


def controller(store: StateStore, task: TaskRecord, **overrides: object) -> RepairController:
    return RepairController(
        store,
        task.task_id,
        limits=ExecutionLimits(**overrides),  # type: ignore[arg-type]
    )


class TestTheCapsule:
    def test_it_names_the_missing_evidence_and_its_tool(self) -> None:
        capsule = evidence_capsule([EvidenceKind.GIT_DIFF_REVIEWED], producers=PRODUCERS)
        assert "git_diff_reviewed" in capsule.expected
        assert "git.inspect" in capsule.expected

    def test_it_is_not_a_tool_or_test_failure(self) -> None:
        """Every call may have succeeded; the work is simply unfinished."""
        capsule = evidence_capsule([EvidenceKind.GIT_DIFF_REVIEWED])
        assert capsule.kind is FailureKind.INCOMPLETE_EVIDENCE

    def test_the_signature_is_stable_for_the_same_gap(self) -> None:
        """Two identical refusals must look identical to the stuck detector."""
        first = evidence_capsule([EvidenceKind.GIT_DIFF_REVIEWED, EvidenceKind.FILE_CHANGED])
        second = evidence_capsule([EvidenceKind.FILE_CHANGED, EvidenceKind.GIT_DIFF_REVIEWED])
        assert first.signature == second.signature

    def test_different_gaps_have_different_signatures(self) -> None:
        assert (
            evidence_capsule([EvidenceKind.GIT_DIFF_REVIEWED]).signature
            != evidence_capsule([EvidenceKind.TESTS_PASSED]).signature
        )

    def test_it_says_the_work_may_already_be_done(self) -> None:
        rendered = evidence_capsule([EvidenceKind.GIT_DIFF_REVIEWED]).render()
        assert "missing is the proof" in rendered


class TestTheDecision:
    def test_a_missing_diff_review_is_repairable(self, store: StateStore, task: TaskRecord) -> None:
        decision = controller(store, task).consider_unmet_evidence(
            [EvidenceKind.GIT_DIFF_REVIEWED],
            producers=PRODUCERS,
            step_id=StepId("s1"),
            changed_files=("calc.py",),
        )
        assert decision.proceed is True
        assert decision.outcome is StepOutcome.REPAIRABLE

    def test_a_read_only_remedy_does_not_need_a_writable_scope(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """The eval case: the step's only changed file was a protected test.

        Refusing to retry a missing `git.inspect` because nothing may be edited
        would block on a permission the fix never wanted.
        """
        decision = controller(store, task).consider_unmet_evidence(
            [EvidenceKind.GIT_DIFF_REVIEWED],
            producers=PRODUCERS,
            step_id=StepId("s1"),
            changed_files=("test_slug.py",),
        )
        assert decision.proceed is True

    def test_a_missing_file_change_still_needs_somewhere_to_write(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """`file_changed` cannot be produced without permission to change a file."""
        decision = controller(store, task).consider_unmet_evidence(
            [EvidenceKind.FILE_CHANGED],
            producers=PRODUCERS,
            step_id=StepId("s1"),
            changed_files=("test_slug.py",),
        )
        assert decision.proceed is False

    def test_the_same_gap_twice_stops(self, store: StateStore, task: TaskRecord) -> None:
        """Repeating an attempt that changed nothing will not change it."""
        control = controller(store, task, repair_attempts_per_step=5)
        for _ in range(2):
            control.consider_unmet_evidence(
                [EvidenceKind.GIT_DIFF_REVIEWED],
                producers=PRODUCERS,
                step_id=StepId("s1"),
                changed_files=("calc.py",),
            )
        final = control.consider_unmet_evidence(
            [EvidenceKind.GIT_DIFF_REVIEWED],
            producers=PRODUCERS,
            step_id=StepId("s1"),
            changed_files=("calc.py",),
        )
        assert final.proceed is False
        assert "will not change it" in final.reason

    def test_the_repair_budget_still_applies(self, store: StateStore, task: TaskRecord) -> None:
        control = controller(store, task, repair_attempts_per_step=1)
        control.consider_unmet_evidence(
            [EvidenceKind.GIT_DIFF_REVIEWED],
            producers=PRODUCERS,
            step_id=StepId("s1"),
            changed_files=("calc.py",),
        )
        # A different gap, so the stuck detector does not fire first.
        second = control.consider_unmet_evidence(
            [EvidenceKind.TESTS_PASSED],
            producers=PRODUCERS,
            step_id=StepId("s1"),
            changed_files=("calc.py",),
        )
        assert second.proceed is False
        assert "budget spent" in second.reason

    def test_the_failure_is_recorded_either_way(self, store: StateStore, task: TaskRecord) -> None:
        """A run that stops here still leaves an account of why."""
        controller(store, task).consider_unmet_evidence(
            [EvidenceKind.GIT_DIFF_REVIEWED],
            producers=PRODUCERS,
            step_id=StepId("s1"),
            changed_files=("calc.py",),
        )
        recorded = store.failures_for(task.task_id)
        assert len(recorded) == 1
        assert recorded[0].kind is FailureKind.INCOMPLETE_EVIDENCE
