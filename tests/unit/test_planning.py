"""Planning contracts: turning a proposal into gated, executable state.

The property that matters most here is that a model cannot weaken its own
completion gate. Everything else in this module — the vocabulary, the tool
allowlists, the approval rule — exists to make that one property enforceable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agent.planning import (
    CHANGE_FLOOR,
    CHANGE_TOOLS,
    READ_ONLY_TOOLS,
    Planner,
    PlanRejected,
    allowed_tools_for,
    evidence_floor,
    map_required_evidence,
    materialise,
    render_plan_summary,
    render_step,
    validate_plan,
)
from shamsu.interfaces.enums import EvidenceKind, Risk, StepOutcome
from shamsu.interfaces.ids import ProjectId, TaskId
from shamsu.models.contracts import ImplementationPlan, PlanStepProposal
from shamsu.runtime.limits import ExecutionLimits, LimitExceeded
from shamsu.security.paths import PathSandbox
from shamsu.state import ProjectRecord, StateStore, TaskRecord, new_id


def _plan(*steps: PlanStepProposal, summary: str = "Do the thing.") -> ImplementationPlan:
    return ImplementationPlan(summary=summary, steps=steps or (_step(),))


def _step(**overrides: object) -> PlanStepProposal:
    base: dict[str, object] = {"title": "Fix the adder", "files": ("calc.py",)}
    base.update(overrides)
    return PlanStepProposal.model_validate(base)


@pytest.fixture
def store() -> StateStore:
    return StateStore(":memory:")


@pytest.fixture
def task(store: StateStore) -> TaskRecord:
    project = store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root=str(Path("/workspace")), name="demo")
    )
    return store.create_task(
        TaskRecord(task_id=TaskId(new_id()), project_id=project.project_id, request="fix the adder")
    )


# ---------------------------------------------------------------------------
# Evidence vocabulary
# ---------------------------------------------------------------------------


class TestEvidenceMapping:
    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("targeted authentication tests pass", EvidenceKind.TESTS_PASSED),
            ("pytest is green", EvidenceKind.TESTS_PASSED),
            ("Git diff reviewed", EvidenceKind.GIT_DIFF_REVIEWED),
            ("the build succeeds", EvidenceKind.BUILD_SUCCEEDED),
            ("mypy reports no errors", EvidenceKind.TYPECHECK_PASSED),
            ("ruff is clean", EvidenceKind.LINT_PASSED),
            ("the migration is applied", EvidenceKind.MIGRATION_APPLIED),
            ("schema matches the model", EvidenceKind.SCHEMA_VERIFIED),
            ("health check returns 200", EvidenceKind.HEALTH_CHECK_PASSED),
            ("a checkpoint exists", EvidenceKind.CHECKPOINT_CREATED),
            ("the file is modified", EvidenceKind.FILE_CHANGED),
        ],
    )
    def test_prose_maps_to_the_right_kind(self, phrase: str, expected: EvidenceKind) -> None:
        assert map_required_evidence([phrase]).kinds == frozenset({expected})

    def test_a_smoke_test_is_not_just_a_test(self) -> None:
        """Order in the vocabulary is load-bearing: 'smoke test' contains 'test'."""
        assert map_required_evidence(["smoke tests pass"]).kinds == frozenset(
            {EvidenceKind.SMOKE_TEST_PASSED}
        )

    def test_an_unknown_phrase_is_reported_not_guessed(self) -> None:
        """A wrong mapping opens the gate on the wrong proof."""
        mapping = map_required_evidence(["the reviewer is happy"])
        assert mapping.kinds == frozenset()
        assert mapping.unrecognised == ("the reviewer is happy",)

    def test_blank_phrases_are_ignored(self) -> None:
        mapping = map_required_evidence(["", "   "])
        assert mapping.kinds == frozenset()
        assert mapping.unrecognised == ()

    def test_an_unmapped_phrase_survives_as_an_acceptance_criterion(self) -> None:
        """Nothing the model said is silently discarded."""
        plan = _plan(_step(required_evidence=("the reviewer is happy",)))
        step = materialise(TaskId("t"), plan).steps[0]
        assert "the reviewer is happy" in step.acceptance_criteria


# ---------------------------------------------------------------------------
# The evidence floor
# ---------------------------------------------------------------------------


class TestTheModelCannotLowerItsOwnBar:
    def test_a_change_step_requiring_nothing_still_gets_the_floor(self) -> None:
        step = materialise(TaskId("t"), _plan(_step(required_evidence=()))).steps[0]
        assert set(step.required_evidence) >= CHANGE_FLOOR

    def test_the_floor_is_a_union_not_a_replacement(self) -> None:
        """A plan asking for more gets more."""
        plan = _plan(_step(required_evidence=("tests pass", "the build succeeds")))
        required = set(materialise(TaskId("t"), plan).steps[0].required_evidence)
        assert required == CHANGE_FLOOR | {
            EvidenceKind.TESTS_PASSED,
            EvidenceKind.BUILD_SUCCEEDED,
        }

    def test_declaring_investigate_lowers_the_bar_and_removes_writing(self) -> None:
        """The only discount on evidence costs the ability to change anything."""
        step = materialise(TaskId("t"), _plan(_step(kind="investigate"))).steps[0]
        assert step.required_evidence == ()
        assert "file.patch" not in step.allowed_tools

    def test_change_is_the_default_kind(self) -> None:
        """An omitted field must land on the stricter side."""
        assert PlanStepProposal(title="x").kind == "change"

    def test_a_change_step_can_reach_the_editing_tools(self) -> None:
        step = materialise(TaskId("t"), _plan(_step())).steps[0]
        assert set(step.allowed_tools) == set(CHANGE_TOOLS)

    def test_floors_and_allowlists_agree_on_kinds(self) -> None:
        assert evidence_floor("investigate") == frozenset()
        assert allowed_tools_for("investigate") == READ_ONLY_TOOLS


# ---------------------------------------------------------------------------
# Materialisation
# ---------------------------------------------------------------------------


class TestMaterialise:
    def test_steps_are_ordered_and_uniquely_identified(self) -> None:
        plan = _plan(_step(title="one"), _step(title="two"), _step(title="three"))
        steps = materialise(TaskId("t"), plan).steps
        assert [step.ordinal for step in steps] == [0, 1, 2]
        assert len({step.step_id for step in steps}) == 3
        assert all(step.plan_id == steps[0].plan_id for step in steps)

    def test_risk_carries_over_and_high_risk_demands_approval(self) -> None:
        steps = materialise(
            TaskId("t"), _plan(_step(risk="high"), _step(title="safe", risk="low"))
        ).steps
        assert steps[0].risk is Risk.HIGH
        assert steps[0].approval_required is True
        assert steps[1].approval_required is False

    def test_a_plan_cannot_pre_approve_its_own_step(self) -> None:
        """`PlanStepProposal` has no approval field, and that is the point."""
        assert "approval_required" not in PlanStepProposal.model_fields

    def test_unmapped_phrases_are_reported_on_the_plan(self) -> None:
        plan = _plan(_step(required_evidence=("vibes are good",)))
        assert materialise(TaskId("t"), plan).unmapped_evidence == ("vibes are good",)

    def test_required_evidence_is_sorted_for_stable_records(self) -> None:
        plan = _plan(_step(required_evidence=("tests pass", "diff reviewed")))
        required = materialise(TaskId("t"), plan).steps[0].required_evidence
        assert list(required) == sorted(required, key=lambda kind: kind.value)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_a_path_outside_the_workspace_is_fatal(self, tmp_path: Path) -> None:
        """Caught at plan time, not three decisions later with the budget spent."""
        sandbox = PathSandbox(tmp_path)
        result = validate_plan(_plan(_step(files=("../../etc/passwd",))), sandbox=sandbox)
        assert result.ok is False
        assert "outside the workspace" in result.problems[0]

    def test_a_plan_citing_unread_files_is_fatal(self) -> None:
        plan = ImplementationPlan(summary="s", steps=(_step(),), grounded_in=("never_opened.py",))
        result = validate_plan(plan, files_seen=("calc.py",))
        assert result.ok is False
        assert "never read" in result.problems[0]

    def test_grounding_is_only_checked_when_the_caller_knows_what_was_read(self) -> None:
        """An empty `files_seen` means 'no investigation', not 'nothing was read'."""
        plan = ImplementationPlan(summary="s", steps=(_step(),), grounded_in=("calc.py",))
        assert validate_plan(plan).ok is True

    def test_a_weak_plan_is_noted_not_refused(self) -> None:
        plan = _plan(_step(acceptance_criteria=(), files=()))
        result = validate_plan(plan)
        assert result.ok is True
        assert any("acceptance criteria" in note for note in result.notes)
        assert any("names no files" in note for note in result.notes)

    def test_duplicate_titles_are_noted(self) -> None:
        result = validate_plan(_plan(_step(title="Fix it"), _step(title="fix it")))
        assert result.ok is True
        assert any("repeats an earlier step title" in note for note in result.notes)


# ---------------------------------------------------------------------------
# Persistence and the step gate
# ---------------------------------------------------------------------------


class TestPlanner:
    def test_creating_a_plan_persists_it_and_points_the_task_at_it(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        materialised = Planner(store).create(task, _plan(_step(), _step(title="two")))

        assert store.get_plan(materialised.plan_id) is not None
        assert len(store.get_steps(materialised.plan_id)) == 2

        reloaded = store.get_task(task.task_id)
        assert reloaded is not None
        assert reloaded.plan_id == materialised.plan_id

    def test_an_invalid_plan_writes_nothing(
        self, store: StateStore, task: TaskRecord, tmp_path: Path
    ) -> None:
        planner = Planner(store, sandbox=PathSandbox(tmp_path))

        with pytest.raises(PlanRejected):
            planner.create(task, _plan(_step(files=("/etc/passwd",))))

        assert store.latest_plan(task.task_id) is None
        reloaded = store.get_task(task.task_id)
        assert reloaded is not None and reloaded.plan_id is None

    def test_the_gate_refuses_without_evidence_and_leaves_the_step_pending(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        planner = Planner(store)
        plan = planner.create(task, _plan(_step()))
        step = planner.next_step(plan.plan_id)
        assert step is not None

        closed, result = planner.close_step(step, frozenset())

        assert result.satisfied is False
        assert result.missing == CHANGE_FLOOR
        assert closed.outcome is None
        assert planner.next_step(plan.plan_id) == step

    def test_the_gate_opens_only_on_the_full_set(self, store: StateStore, task: TaskRecord) -> None:
        planner = Planner(store)
        plan = planner.create(task, _plan(_step()))
        step = planner.next_step(plan.plan_id)
        assert step is not None

        _, partial = planner.close_step(step, frozenset({EvidenceKind.FILE_CHANGED}))
        assert partial.satisfied is False

        closed, complete = planner.close_step(step, CHANGE_FLOOR)
        assert complete.satisfied is True
        assert closed.outcome is StepOutcome.PASS
        assert planner.next_step(plan.plan_id) is None

    def test_a_passing_outcome_cannot_be_written_by_hand(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """There is exactly one path to PASS, and it reads the evidence table."""
        planner = Planner(store)
        plan = planner.create(task, _plan(_step()))
        step = planner.next_step(plan.plan_id)
        assert step is not None

        with pytest.raises(ValueError, match="evidence gate"):
            planner.fail_step(step, StepOutcome.PASS)

    def test_a_failed_step_stops_being_offered(self, store: StateStore, task: TaskRecord) -> None:
        planner = Planner(store)
        plan = planner.create(task, _plan(_step()))
        step = planner.next_step(plan.plan_id)
        assert step is not None

        planner.fail_step(step, StepOutcome.BLOCKED)
        assert planner.next_step(plan.plan_id) is None

    def test_attempts_are_bounded(self, store: StateStore, task: TaskRecord) -> None:
        planner = Planner(store, limits=ExecutionLimits(repair_attempts_per_step=1))
        plan = planner.create(task, _plan(_step()))
        step = planner.next_step(plan.plan_id)
        assert step is not None

        step = planner.begin_step(step)  # first attempt
        step = planner.begin_step(step)  # one repair
        with pytest.raises(LimitExceeded, match="repair_attempts_per_step"):
            planner.begin_step(step)

    def test_progress_counts_only_gated_steps(self, store: StateStore, task: TaskRecord) -> None:
        planner = Planner(store)
        plan = planner.create(task, _plan(_step(), _step(title="two")))
        first = planner.next_step(plan.plan_id)
        assert first is not None

        planner.close_step(first, CHANGE_FLOOR)

        progress = planner.progress(plan.plan_id)
        assert (progress.completed, progress.remaining, progress.done) == (1, 1, False)


# ---------------------------------------------------------------------------
# Re-planning
# ---------------------------------------------------------------------------


class TestReplanning:
    def test_replanning_supersedes_rather_than_edits(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        planner = Planner(store)
        first = planner.create(task, _plan(_step(title="original")))

        current = store.get_task(task.task_id)
        assert current is not None
        second = planner.replan(current, _plan(_step(title="different")))

        superseded = store.get_plan(first.plan_id)
        assert superseded is not None
        assert superseded.superseded_by == second.plan_id
        assert second.record.version == 2

        # The old steps are still queryable: "what did it think it was doing?"
        assert [step.title for step in store.get_steps(first.plan_id)] == ["original"]
        assert len(store.plan_history(task.task_id)) == 2

    def test_the_task_points_at_the_new_plan_and_counts_the_replan(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        planner = Planner(store)
        planner.create(task, _plan(_step()))

        current = store.get_task(task.task_id)
        assert current is not None
        second = planner.replan(current, _plan(_step(title="different")))

        reloaded = store.get_task(task.task_id)
        assert reloaded is not None
        assert reloaded.plan_id == second.plan_id
        assert reloaded.current_step_id is None
        assert reloaded.replan_count == 1

    def test_completed_work_crosses_as_titles_not_rows(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """Copying a step would orphan the evidence keyed to its id."""
        planner = Planner(store)
        first = planner.create(task, _plan(_step(title="done already"), _step(title="two")))
        step = planner.next_step(first.plan_id)
        assert step is not None
        planner.close_step(step, CHANGE_FLOOR)

        assert planner.completed_titles(first.plan_id) == ("done already",)

    def test_the_replan_budget_is_enforced_before_anything_is_written(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        planner = Planner(store, limits=ExecutionLimits(replans_per_task=1))
        planner.create(task, _plan(_step()))

        current = store.get_task(task.task_id)
        assert current is not None
        planner.replan(current, _plan(_step(title="second")))

        current = store.get_task(task.task_id)
        assert current is not None
        with pytest.raises(LimitExceeded, match="replans_per_task"):
            planner.replan(current, _plan(_step(title="third")))

        # The refused re-plan left the existing plan intact.
        assert len(store.plan_history(task.task_id)) == 2
        latest = store.latest_plan(task.task_id)
        assert latest is not None and latest.version == 2

    def test_a_rejected_replan_leaves_the_previous_plan_current(
        self, store: StateStore, task: TaskRecord, tmp_path: Path
    ) -> None:
        planner = Planner(store, sandbox=PathSandbox(tmp_path))
        first = planner.create(task, _plan(_step(files=())))

        current = store.get_task(task.task_id)
        assert current is not None
        with pytest.raises(PlanRejected):
            planner.replan(current, _plan(_step(files=("/etc/shadow",))))

        still = store.get_plan(first.plan_id)
        assert still is not None and still.superseded_by is None
        reloaded = store.get_task(task.task_id)
        assert reloaded is not None and reloaded.replan_count == 0


# ---------------------------------------------------------------------------
# Rendering for the prompt
# ---------------------------------------------------------------------------


class TestRendering:
    def test_the_summary_is_titles_only(self, store: StateStore, task: TaskRecord) -> None:
        """A twelve-step plan rendered in full would eat the source budget."""
        planner = Planner(store)
        plan = planner.create(
            task,
            _plan(
                _step(title="one", intent="a long intent that must not appear"),
                _step(title="two"),
            ),
        )
        steps = store.get_steps(plan.plan_id)
        record = store.get_plan(plan.plan_id)
        assert record is not None

        rendered = render_plan_summary(record, steps, current=steps[1].step_id)

        assert "1. one" in rendered and "2. two" in rendered
        assert "a long intent" not in rendered
        assert "← current" in rendered

    def test_a_completed_step_is_marked(self, store: StateStore, task: TaskRecord) -> None:
        planner = Planner(store)
        plan = planner.create(task, _plan(_step(title="one"), _step(title="two")))
        step = planner.next_step(plan.plan_id)
        assert step is not None
        planner.close_step(step, CHANGE_FLOOR)

        record = store.get_plan(plan.plan_id)
        assert record is not None
        assert "[✓] 1. one" in render_plan_summary(record, store.get_steps(plan.plan_id))

    def test_the_current_step_states_what_completes_it(self) -> None:
        step = materialise(TaskId("t"), _plan(_step(intent="make add() correct"))).steps[0]
        rendered = render_step(step)
        assert "make add() correct" in rendered
        assert "calc.py" in rendered
        assert "file_changed" in rendered
