"""Claim validation, the final gate, and the evidence report.

Plan §20.7: *the model cannot set completion directly*. Every test here is
ultimately about that one sentence — that there is no argument, no phrasing,
and no accumulation of unrelated proof that gets a task marked done without the
rows to back it.
"""

from __future__ import annotations

import json

import pytest

from shamsu.interfaces.enums import AgentState, EvidenceKind, Phase, StepOutcome
from shamsu.interfaces.ids import (
    EvidenceId,
    PlanId,
    ProjectId,
    RunId,
    StepId,
    TaskId,
    ToolEventId,
)
from shamsu.models.contracts import CompletionClaim, contract_for
from shamsu.state import (
    EvidenceRecord,
    PlanRecord,
    PlanStepRecord,
    ProjectRecord,
    RunRecord,
    StateStore,
    TaskRecord,
    ToolEventRecord,
    new_id,
)
from shamsu.verification.completion import (
    TASK_COMPLETE,
    CompletionGate,
    build_report,
    known_claims,
    next_after_completion_gate,
    validate_claim,
)

FLOOR = frozenset({EvidenceKind.FILE_CHANGED, EvidenceKind.GIT_DIFF_REVIEWED})


@pytest.fixture
def store() -> StateStore:
    return StateStore(":memory:")


@pytest.fixture
def task(store: StateStore) -> TaskRecord:
    project = store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root="/workspace", name="demo")
    )
    return store.create_task(
        TaskRecord(
            task_id=TaskId(new_id()), project_id=project.project_id, request="make add() correct"
        )
    )


@pytest.fixture
def run(store: StateStore, task: TaskRecord) -> RunRecord:
    return store.create_run(
        RunRecord(run_id=RunId(new_id()), project_id=task.project_id, task_id=task.task_id)
    )


def _plan_with(store: StateStore, task: TaskRecord, count: int = 2) -> PlanId:
    plan_id = PlanId(new_id())
    steps = [
        PlanStepRecord(
            step_id=StepId(new_id()),
            plan_id=plan_id,
            ordinal=index,
            title=f"step {index + 1}",
            required_evidence=tuple(sorted(FLOOR, key=lambda kind: kind.value)),
        )
        for index in range(count)
    ]
    store.create_plan(
        PlanRecord(plan_id=plan_id, task_id=task.task_id, version=1, summary="s"), steps
    )
    return plan_id


def _prove(
    store: StateStore,
    run: RunRecord,
    task: TaskRecord,
    step_id: StepId | None,
    kinds: frozenset[EvidenceKind],
    *,
    tool: str = "file.patch",
    path: str = "calc.py",
    ok: bool = True,
) -> ToolEventId:
    """Register evidence the only way it can be registered: behind a tool event."""
    event = store.record_tool_event(
        ToolEventRecord(
            event_id=ToolEventId(new_id()),
            run_id=run.run_id,
            task_id=task.task_id,
            step_id=step_id,
            tool=tool,
            phase=Phase.AUTHOR,
            arguments_json=json.dumps({"path": path}),
            ok=ok,
            output=f"did {tool}",
        )
    )
    for kind in kinds:
        store.record_evidence(
            EvidenceRecord(
                evidence_id=EvidenceId(new_id()),
                task_id=task.task_id,
                step_id=step_id,
                kind=kind,
                source_event_id=event.event_id,
                detail=f"{tool} ok",
            )
        )
    return event.event_id


def _pass_steps(store: StateStore, plan_id: PlanId) -> None:
    for step in store.get_steps(plan_id):
        store.save_step(step.model_copy(update={"outcome": StepOutcome.PASS}))


# ---------------------------------------------------------------------------
# Claim validation
# ---------------------------------------------------------------------------


class TestClaimValidation:
    def test_a_supported_claim_is_accepted(self) -> None:
        verdict = validate_claim("tests_pass", frozenset({EvidenceKind.TESTS_PASSED}))
        assert verdict.accepted is True

    def test_an_unsupported_claim_names_what_is_missing(self) -> None:
        verdict = validate_claim("file_modified", frozenset({EvidenceKind.FILE_CHANGED}))
        assert verdict.accepted is False
        assert verdict.missing == frozenset({EvidenceKind.GIT_DIFF_REVIEWED})
        assert "git_diff_reviewed" in verdict.reason

    def test_a_typo_is_refused_not_defaulted(self) -> None:
        """An empty requirement set is trivially satisfied.

        `requirements_for` returns one for an unknown name, so a mistyped claim
        would otherwise sail through the check that exists to stop it.
        """
        verdict = validate_claim("tests_pas", frozenset({EvidenceKind.TESTS_PASSED}))
        assert verdict.accepted is False
        assert "unknown claim" in verdict.reason
        assert "tests_pass" in verdict.reason  # the real name is offered

    def test_unrelated_evidence_does_not_support_a_claim(self) -> None:
        verdict = validate_claim("build_succeeds", frozenset({EvidenceKind.TESTS_PASSED}))
        assert verdict.accepted is False

    def test_task_completion_is_not_decided_here(self) -> None:
        """Its requirements come from the plan, so they are not a fixed set."""
        verdict = validate_claim(TASK_COMPLETE, frozenset(EvidenceKind))
        assert verdict.accepted is False
        assert "final gate" in verdict.reason

    def test_every_known_claim_is_reachable(self) -> None:
        for claim in known_claims():
            verdict = validate_claim(claim, frozenset(EvidenceKind))
            assert verdict.accepted is (claim != TASK_COMPLETE), claim

    def test_the_claim_contract_carries_no_authority(self) -> None:
        """Nothing in the shape a model emits can set completion."""
        claim = CompletionClaim(claim="tests_pass", evidence_cited=("I ran them, honest",))
        assert contract_for("CompletionClaim") is CompletionClaim
        # The cited "evidence" is prose and is never consulted by the gate.
        assert validate_claim(claim.claim, frozenset()).accepted is False


# ---------------------------------------------------------------------------
# The final gate
# ---------------------------------------------------------------------------


class TestFinalGate:
    def test_a_plan_with_no_steps_cannot_complete(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        plan_id = PlanId(new_id())
        store.create_plan(
            PlanRecord(plan_id=plan_id, task_id=task.task_id, version=1, summary="s"), []
        )
        verdict = CompletionGate(store, task.task_id).check_task(plan_id)
        assert verdict.satisfied is False
        assert "planned nothing" in verdict.reason

    def test_one_thorough_step_does_not_finish_the_others(
        self, store: StateStore, task: TaskRecord, run: RunRecord
    ) -> None:
        """The property the final gate exists for.

        A task-level union check would pass here: every required kind is
        registered somewhere. Judging each step at its own scope is what stops
        a four-step plan completing on one step's work.
        """
        plan_id = _plan_with(store, task, count=4)
        steps = store.get_steps(plan_id)
        _prove(store, run, task, steps[0].step_id, FLOOR)
        _pass_steps(store, plan_id)

        gate = CompletionGate(store, task.task_id)

        # Task-level evidence looks complete...
        assert store.verified_evidence(task.task_id) >= FLOOR
        # ...and the gate still refuses.
        verdict = gate.check_task(plan_id)
        assert verdict.satisfied is False
        assert len(verdict.unfinished) == 0  # every step is marked passed
        assert "missing evidence" in verdict.reason

    def test_an_unfinished_step_blocks_completion(
        self, store: StateStore, task: TaskRecord, run: RunRecord
    ) -> None:
        plan_id = _plan_with(store, task, count=2)
        steps = store.get_steps(plan_id)
        for step in steps:
            _prove(store, run, task, step.step_id, FLOOR)
        store.save_step(steps[0].model_copy(update={"outcome": StepOutcome.PASS}))

        verdict = CompletionGate(store, task.task_id).check_task(plan_id)
        assert verdict.satisfied is False
        assert [step.title for step in verdict.unfinished] == ["step 2"]
        assert "Unfinished steps" in verdict.reason

    def test_a_blocked_step_is_unfinished_not_complete(
        self, store: StateStore, task: TaskRecord, run: RunRecord
    ) -> None:
        """A step that gave up is not a step that is done."""
        plan_id = _plan_with(store, task, count=1)
        step = store.get_steps(plan_id)[0]
        _prove(store, run, task, step.step_id, FLOOR)
        store.save_step(step.model_copy(update={"outcome": StepOutcome.BLOCKED}))

        assert CompletionGate(store, task.task_id).check_task(plan_id).satisfied is False

    def test_a_fully_proven_plan_completes(
        self, store: StateStore, task: TaskRecord, run: RunRecord
    ) -> None:
        plan_id = _plan_with(store, task, count=3)
        for step in store.get_steps(plan_id):
            _prove(store, run, task, step.step_id, FLOOR)
        _pass_steps(store, plan_id)

        verdict = CompletionGate(store, task.task_id).check_task(plan_id)
        assert verdict.satisfied is True
        assert verdict.missing == frozenset()
        assert "3 step(s) passed" in verdict.reason

    def test_the_rows_outrank_the_recorded_outcome(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """A step outcome is a cached decision; evidence is the fact."""
        plan_id = _plan_with(store, task, count=1)
        _pass_steps(store, plan_id)  # marked passed, no evidence ever registered

        verdict = CompletionGate(store, task.task_id).check_task(plan_id)
        assert verdict.satisfied is False
        assert verdict.missing == FLOOR

    def test_the_step_gate_is_scoped_to_its_own_step(
        self, store: StateStore, task: TaskRecord, run: RunRecord
    ) -> None:
        plan_id = _plan_with(store, task, count=2)
        first, second = store.get_steps(plan_id)
        _prove(store, run, task, first.step_id, FLOOR)

        gate = CompletionGate(store, task.task_id)
        assert gate.check_step(first).satisfied is True
        assert gate.check_step(second).satisfied is False

    def test_a_claim_can_be_checked_at_step_scope(
        self, store: StateStore, task: TaskRecord, run: RunRecord
    ) -> None:
        plan_id = _plan_with(store, task, count=2)
        first, second = store.get_steps(plan_id)
        _prove(store, run, task, first.step_id, FLOOR)

        gate = CompletionGate(store, task.task_id)
        assert gate.check_claim("file_modified", step_id=first.step_id).accepted is True
        assert gate.check_claim("file_modified", step_id=second.step_id).accepted is False


# ---------------------------------------------------------------------------
# Where the gate sends the run
# ---------------------------------------------------------------------------


class TestGateRouting:
    def test_a_satisfied_gate_reports(
        self, store: StateStore, task: TaskRecord, run: RunRecord
    ) -> None:
        plan_id = _plan_with(store, task, count=1)
        _prove(store, run, task, store.get_steps(plan_id)[0].step_id, FLOOR)
        _pass_steps(store, plan_id)
        verdict = CompletionGate(store, task.task_id).check_task(plan_id)

        assert next_after_completion_gate(verdict, can_replan=True) is AgentState.FINAL_REPORT

    def test_refusal_with_budget_left_goes_back_to_planning(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        verdict = CompletionGate(store, task.task_id).check_task(_plan_with(store, task))
        assert next_after_completion_gate(verdict, can_replan=True) is AgentState.REPLAN

    def test_refusal_with_no_budget_left_blocks(self, store: StateStore, task: TaskRecord) -> None:
        """'We gave up' must stay distinguishable from 'we ran out of budget'."""
        verdict = CompletionGate(store, task.task_id).check_task(_plan_with(store, task))
        assert next_after_completion_gate(verdict, can_replan=False) is AgentState.BLOCKED


# ---------------------------------------------------------------------------
# The evidence report
# ---------------------------------------------------------------------------


class TestEvidenceReport:
    def test_the_report_is_built_from_rows(
        self, store: StateStore, task: TaskRecord, run: RunRecord
    ) -> None:
        plan_id = _plan_with(store, task, count=1)
        step = store.get_steps(plan_id)[0]
        _prove(store, run, task, step.step_id, frozenset({EvidenceKind.FILE_CHANGED}))
        _prove(
            store,
            run,
            task,
            step.step_id,
            frozenset({EvidenceKind.GIT_DIFF_REVIEWED}),
            tool="git.inspect",
        )
        _pass_steps(store, plan_id)

        report = build_report(store, task.task_id, plan_id)

        assert report.verdict.satisfied is True
        assert report.changed_files == ("calc.py",)
        assert {entry.kind for entry in report.entries} == FLOOR
        assert {entry.tool for entry in report.entries} == {"file.patch", "git.inspect"}
        assert all(entry.step_title == "step 1" for entry in report.entries)

    def test_the_report_states_a_refusal_and_why(self, store: StateStore, task: TaskRecord) -> None:
        plan_id = _plan_with(store, task, count=2)
        rendered = build_report(store, task.task_id, plan_id).render()

        assert "NOT COMPLETE" in rendered
        assert "Unfinished steps" in rendered
        assert "Evidence: none registered." in rendered

    def test_changed_files_come_from_successful_patches_only(
        self, store: StateStore, task: TaskRecord, run: RunRecord
    ) -> None:
        """A list the agent maintains is a list the agent can be wrong about."""
        plan_id = _plan_with(store, task, count=1)
        step = store.get_steps(plan_id)[0]
        _prove(store, run, task, step.step_id, frozenset(), path="failed.py", ok=False)
        _prove(store, run, task, step.step_id, frozenset({EvidenceKind.FILE_CHANGED}))
        _prove(
            store,
            run,
            task,
            step.step_id,
            frozenset(),
            tool="test.run",
            path="not_a_patch.py",
        )

        report = build_report(store, task.task_id, plan_id)
        assert report.changed_files == ("calc.py",)
        assert report.failed_calls == 1
        assert "1 tool call(s) failed" in report.render()

    def test_a_report_for_a_missing_task_is_refused(self, store: StateStore) -> None:
        """A plausible-looking report for a task that never ran is the worst kind."""
        with pytest.raises(KeyError):
            build_report(store, TaskId("nope"), PlanId("nope"))

    def test_the_render_names_the_missing_evidence(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        plan_id = _plan_with(store, task, count=1)
        _pass_steps(store, plan_id)
        rendered = build_report(store, task.task_id, plan_id).render()

        assert "missing: file_changed, git_diff_reviewed" in rendered
        assert "[✗] 1. step 1" in rendered
