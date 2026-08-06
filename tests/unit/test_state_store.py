"""The authoritative SQLite state store.

The properties under test are the ones the rest of the runtime is entitled to
assume: transitions cannot be bypassed, evidence cannot be forged, plans are
atomic, and a task can be resumed from a checkpoint.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from shamsu.interfaces.enums import (
    AgentState,
    ApprovalDecision,
    EvidenceKind,
    FactKind,
    FactOrigin,
    FailureKind,
    Phase,
    Risk,
    RunStatus,
    StepOutcome,
    TaskKind,
)
from shamsu.interfaces.ids import (
    ApprovalId,
    CheckpointId,
    EvidenceId,
    FailureId,
    PlanId,
    ProjectId,
    RunId,
    StepId,
    TaskId,
    ToolEventId,
)
from shamsu.memory import MemoryStore
from shamsu.state import (
    ApprovalRecord,
    CheckpointRecord,
    EvidenceRecord,
    FailureRecord,
    InvalidTransition,
    PlanRecord,
    PlanStepRecord,
    ProjectRecord,
    RunRecord,
    StateStore,
    TaskRecord,
    ToolEventRecord,
    new_id,
)
from shamsu.state.schema import (
    MIGRATIONS,
    SCHEMA_VERSION,
    connect,
    current_version,
    migrate,
)


@pytest.fixture
def store() -> StateStore:
    return StateStore(":memory:")


@pytest.fixture
def project(store: StateStore) -> ProjectRecord:
    return store.upsert_project(
        ProjectRecord(
            project_id=ProjectId("p1"),
            root="/workspace/demo",
            name="demo",
            languages=("python",),
            test_commands=("pytest -q",),
        )
    )


@pytest.fixture
def task(store: StateStore, project: ProjectRecord) -> TaskRecord:
    return store.create_task(
        TaskRecord(
            task_id=TaskId("t1"),
            project_id=project.project_id,
            request="Add a login endpoint",
        )
    )


@pytest.fixture
def run(store: StateStore, project: ProjectRecord, task: TaskRecord) -> RunRecord:
    return store.create_run(
        RunRecord(run_id=RunId("r1"), project_id=project.project_id, task_id=task.task_id)
    )


def _event(run: RunRecord, task: TaskRecord, *, ok: bool = True, tool: str = "test.run"):
    return ToolEventRecord(
        event_id=ToolEventId(new_id()),
        run_id=run.run_id,
        task_id=task.task_id,
        step_id=None,
        tool=tool,
        phase=Phase.VERIFY,
        arguments_json=json.dumps({"path": "tests/"}),
        ok=ok,
    )


# ---------------------------------------------------------------------------
# Schema and migrations
# ---------------------------------------------------------------------------


class TestSchema:
    def test_a_fresh_database_is_at_the_current_version(self, store: StateStore) -> None:
        assert current_version(store.connection) == SCHEMA_VERSION

    def test_migration_is_idempotent(self, store: StateStore) -> None:
        """Reopening an existing database must not re-run migrations."""
        assert migrate(store.connection) == SCHEMA_VERSION
        assert migrate(store.connection) == SCHEMA_VERSION

    def test_foreign_keys_are_enforced(self, store: StateStore) -> None:
        """Off by default in SQLite; the evidence guarantee depends on it."""
        row = store.connection.execute("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1

    def test_refuses_a_database_from_a_newer_build(self, tmp_path: Path) -> None:
        """Silently accepting a newer schema risks violating semantics we lack."""
        path = tmp_path / "future.db"
        connection = connect(str(path))
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 99}")
        with pytest.raises(RuntimeError, match="newer than this build"):
            migrate(connection)
        connection.close()

    def test_survives_a_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        with StateStore(path) as first:
            first.upsert_project(ProjectRecord(project_id=ProjectId("p1"), root="/w/x", name="x"))
        with StateStore(path) as second:
            assert second.get_project(ProjectId("p1")) is not None

    def test_creates_missing_parent_directories(self, tmp_path: Path) -> None:
        store = StateStore(tmp_path / "nested" / "deeper" / "state.db")
        assert (tmp_path / "nested" / "deeper" / "state.db").exists()
        store.close()


# ---------------------------------------------------------------------------
# Projects and tasks
# ---------------------------------------------------------------------------


class TestProjects:
    def test_round_trips(self, store: StateStore, project: ProjectRecord) -> None:
        loaded = store.get_project(project.project_id)
        assert loaded is not None
        assert loaded.name == "demo"
        assert loaded.languages == ("python",)
        assert loaded.test_commands == ("pytest -q",)

    def test_unknown_project_is_none_not_an_error(self, store: StateStore) -> None:
        assert store.get_project(ProjectId("nope")) is None

    def test_upsert_updates_in_place(self, store: StateStore, project: ProjectRecord) -> None:
        store.upsert_project(project.model_copy(update={"active_branch": "feature/login"}))
        loaded = store.get_project(project.project_id)
        assert loaded is not None
        assert loaded.active_branch == "feature/login"


class TestTasks:
    def test_round_trips(self, store: StateStore, task: TaskRecord) -> None:
        loaded = store.get_task(task.task_id)
        assert loaded is not None
        assert loaded.request == "Add a login endpoint"
        assert loaded.state is AgentState.RECEIVE_TASK
        assert loaded.phase is Phase.INSPECT

    def test_counters_persist(self, store: StateStore, task: TaskRecord) -> None:
        """v1 kept these on the loop object, where a crash erased them."""
        store.save_task(
            task.model_copy(update={"action_count": 3, "repair_count": 1, "replan_count": 2})
        )
        loaded = store.get_task(task.task_id)
        assert loaded is not None
        assert (loaded.action_count, loaded.repair_count, loaded.replan_count) == (3, 1, 2)

    def test_records_are_frozen(self, task: TaskRecord) -> None:
        """State changes must go through the store, not mutate in place."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            task.action_count = 5  # type: ignore[misc]

    def test_a_task_needs_a_real_project(self, store: StateStore) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            store.create_task(
                TaskRecord(
                    task_id=TaskId("orphan"),
                    project_id=ProjectId("missing"),
                    request="x",
                )
            )


# ---------------------------------------------------------------------------
# Transition validation on write
# ---------------------------------------------------------------------------


class TestAdvanceTask:
    def test_a_legal_move_is_persisted(self, store: StateStore, task: TaskRecord) -> None:
        advanced = store.advance_task(task, AgentState.LOAD_PROJECT_STATE)
        assert advanced.state is AgentState.LOAD_PROJECT_STATE
        reloaded = store.get_task(task.task_id)
        assert reloaded is not None
        assert reloaded.state is AgentState.LOAD_PROJECT_STATE

    def test_an_illegal_move_raises(self, store: StateStore, task: TaskRecord) -> None:
        with pytest.raises(InvalidTransition):
            store.advance_task(task, AgentState.FINAL_REPORT)

    def test_an_illegal_move_writes_nothing(self, store: StateStore, task: TaskRecord) -> None:
        """A rejected transition must leave the stored state untouched."""
        with pytest.raises(InvalidTransition):
            store.advance_task(task, AgentState.FINAL_REPORT)
        reloaded = store.get_task(task.task_id)
        assert reloaded is not None
        assert reloaded.state is AgentState.RECEIVE_TASK

    def test_phase_changes_alongside_state(self, store: StateStore, task: TaskRecord) -> None:
        advanced = store.advance_task(task, AgentState.LOAD_PROJECT_STATE, phase=Phase.PLAN)
        assert advanced.phase is Phase.PLAN

    def test_cancellation_reaches_stopped_from_mid_run(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        walked = task
        for target in (
            AgentState.LOAD_PROJECT_STATE,
            AgentState.INSPECT_PROJECT,
            AgentState.CLASSIFY_TASK,
            AgentState.EXECUTE_CURRENT_STEP,
        ):
            walked = store.advance_task(walked, target)

        stopped = store.advance_task(walked, AgentState.STOPPED, cancelling=True)
        assert stopped.state is AgentState.STOPPED

    def test_cancellation_is_still_refused_for_illegal_targets(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        with pytest.raises(InvalidTransition):
            store.advance_task(task, AgentState.FINAL_REPORT, cancelling=True)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class TestRuns:
    def test_round_trips(self, store: StateStore, run: RunRecord) -> None:
        loaded = store.get_run(run.run_id)
        assert loaded is not None
        assert loaded.status is RunStatus.PENDING
        assert loaded.is_terminal is False

    def test_active_runs_are_listable(self, store: StateStore, run: RunRecord) -> None:
        """A run that cannot be listed cannot be cancelled."""
        store.save_run(run.model_copy(update={"status": RunStatus.RUNNING}))
        assert [r.run_id for r in store.active_runs()] == [run.run_id]

    def test_finished_runs_drop_out_of_the_active_list(
        self, store: StateStore, run: RunRecord
    ) -> None:
        store.save_run(run.model_copy(update={"status": RunStatus.COMPLETED}))
        assert store.active_runs() == []

    @pytest.mark.parametrize(
        "status",
        [RunStatus.CANCELLED, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.TIMED_OUT],
    )
    def test_terminal_statuses(self, run: RunRecord, status: RunStatus) -> None:
        assert run.model_copy(update={"status": status}).is_terminal is True

    def test_a_paused_run_is_still_active(self, store: StateStore, run: RunRecord) -> None:
        store.save_run(run.model_copy(update={"status": RunStatus.PAUSED}))
        assert len(store.active_runs()) == 1

    def test_cancel_reason_is_recorded(self, store: StateStore, run: RunRecord) -> None:
        store.save_run(
            run.model_copy(
                update={"status": RunStatus.CANCELLED, "cancel_reason": "user interrupt"}
            )
        )
        loaded = store.get_run(run.run_id)
        assert loaded is not None
        assert loaded.cancel_reason == "user interrupt"


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def _plan_with_steps(task: TaskRecord) -> tuple[PlanRecord, list[PlanStepRecord]]:
    plan = PlanRecord(plan_id=PlanId("plan1"), task_id=task.task_id, version=1, summary="Add login")
    steps = [
        PlanStepRecord(
            step_id=StepId("s1"),
            plan_id=plan.plan_id,
            ordinal=0,
            title="Add the endpoint",
            allowed_tools=("file.read", "file.patch"),
            acceptance_criteria=("valid credentials succeed",),
            required_evidence=(EvidenceKind.FILE_CHANGED, EvidenceKind.TESTS_PASSED),
            risk=Risk.MEDIUM,
        ),
        PlanStepRecord(
            step_id=StepId("s2"),
            plan_id=plan.plan_id,
            ordinal=1,
            title="Add tests",
            required_evidence=(EvidenceKind.TESTS_PASSED,),
        ),
    ]
    return plan, steps


class TestPlans:
    def test_steps_round_trip_in_order(self, store: StateStore, task: TaskRecord) -> None:
        plan, steps = _plan_with_steps(task)
        store.create_plan(plan, steps)

        loaded = store.get_steps(plan.plan_id)
        assert [s.ordinal for s in loaded] == [0, 1]
        assert loaded[0].allowed_tools == ("file.read", "file.patch")
        assert loaded[0].required_evidence == (
            EvidenceKind.FILE_CHANGED,
            EvidenceKind.TESTS_PASSED,
        )
        assert loaded[0].risk is Risk.MEDIUM

    def test_plan_and_steps_are_written_atomically(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """A plan with no steps would leave the runtime nothing to execute."""
        plan, steps = _plan_with_steps(task)
        steps.append(steps[0])  # duplicate ordinal violates the UNIQUE constraint

        with pytest.raises(sqlite3.IntegrityError):
            store.create_plan(plan, steps)

        assert store.get_steps(plan.plan_id) == []
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM plans WHERE plan_id=?", (plan.plan_id,)
            ).fetchone()[0]
            == 0
        )

    def test_step_outcome_is_persisted(self, store: StateStore, task: TaskRecord) -> None:
        plan, steps = _plan_with_steps(task)
        store.create_plan(plan, steps)
        store.save_step(steps[0].model_copy(update={"outcome": StepOutcome.PASS, "attempts": 2}))
        loaded = store.get_steps(plan.plan_id)
        assert loaded[0].outcome is StepOutcome.PASS
        assert loaded[0].attempts == 2

    def test_replanning_keeps_the_superseded_plan(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """'What did it think it was doing before?' must stay answerable."""
        first, steps = _plan_with_steps(task)
        store.create_plan(first, steps)
        second = PlanRecord(
            plan_id=PlanId("plan2"), task_id=task.task_id, version=2, summary="Revised"
        )
        store.create_plan(second, [])

        rows = store.connection.execute(
            "SELECT version FROM plans WHERE task_id=? ORDER BY version", (task.task_id,)
        ).fetchall()
        assert [row[0] for row in rows] == [1, 2]


# ---------------------------------------------------------------------------
# Evidence -- the completion guarantee
# ---------------------------------------------------------------------------


class TestEvidence:
    def test_evidence_requires_a_real_tool_event(self, store: StateStore, task: TaskRecord) -> None:
        """The core anti-fabrication guarantee.

        There must be no path from "the model said the tests passed" to a row
        in the evidence table.
        """
        with pytest.raises(sqlite3.IntegrityError):
            store.record_evidence(
                EvidenceRecord(
                    evidence_id=EvidenceId(new_id()),
                    task_id=task.task_id,
                    step_id=None,
                    kind=EvidenceKind.TESTS_PASSED,
                    source_event_id=ToolEventId("never-happened"),
                )
            )
        assert store.verified_evidence(task.task_id) == frozenset()

    def test_evidence_from_an_observed_event_is_accepted(
        self, store: StateStore, run: RunRecord, task: TaskRecord
    ) -> None:
        event = store.record_tool_event(_event(run, task))
        store.record_evidence(
            EvidenceRecord(
                evidence_id=EvidenceId(new_id()),
                task_id=task.task_id,
                step_id=None,
                kind=EvidenceKind.TESTS_PASSED,
                source_event_id=event.event_id,
                detail="12 passed",
            )
        )
        assert store.verified_evidence(task.task_id) == frozenset({EvidenceKind.TESTS_PASSED})

    def test_the_completion_rule_is_computable(
        self, store: StateStore, run: RunRecord, task: TaskRecord
    ) -> None:
        """required_evidence <= verified_evidence, straight off the store."""
        required = frozenset({EvidenceKind.FILE_CHANGED, EvidenceKind.TESTS_PASSED})

        event = store.record_tool_event(_event(run, task, tool="file.patch"))
        store.record_evidence(
            EvidenceRecord(
                evidence_id=EvidenceId(new_id()),
                task_id=task.task_id,
                step_id=None,
                kind=EvidenceKind.FILE_CHANGED,
                source_event_id=event.event_id,
            )
        )
        assert not required <= store.verified_evidence(task.task_id)

        event2 = store.record_tool_event(_event(run, task))
        store.record_evidence(
            EvidenceRecord(
                evidence_id=EvidenceId(new_id()),
                task_id=task.task_id,
                step_id=None,
                kind=EvidenceKind.TESTS_PASSED,
                source_event_id=event2.event_id,
            )
        )
        assert required <= store.verified_evidence(task.task_id)

    def test_evidence_is_scoped_per_step(
        self, store: StateStore, run: RunRecord, task: TaskRecord
    ) -> None:
        """Step 1's passing tests must not satisfy step 2's requirements."""
        plan, steps = _plan_with_steps(task)
        store.create_plan(plan, steps)
        event = store.record_tool_event(_event(run, task))
        store.record_evidence(
            EvidenceRecord(
                evidence_id=EvidenceId(new_id()),
                task_id=task.task_id,
                step_id=steps[0].step_id,
                kind=EvidenceKind.TESTS_PASSED,
                source_event_id=event.event_id,
            )
        )
        assert store.verified_evidence(task.task_id, steps[0].step_id) == frozenset(
            {EvidenceKind.TESTS_PASSED}
        )
        assert store.verified_evidence(task.task_id, steps[1].step_id) == frozenset()

    def test_failed_tool_events_are_recorded_too(
        self, store: StateStore, run: RunRecord, task: TaskRecord
    ) -> None:
        """A ledger that only remembers successes cannot explain a failure."""
        event = store.record_tool_event(
            _event(run, task, ok=False).model_copy(update={"error": "2 failed"})
        )
        row = store.connection.execute(
            "SELECT ok, error FROM tool_events WHERE event_id=?", (event.event_id,)
        ).fetchone()
        assert row["ok"] == 0
        assert row["error"] == "2 failed"

    def test_truncation_metadata_survives(
        self, store: StateStore, run: RunRecord, task: TaskRecord
    ) -> None:
        event = store.record_tool_event(
            _event(run, task, tool="file.read").model_copy(
                update={"output": "x" * 10, "truncated": True, "original_bytes": 900_000}
            )
        )
        row = store.connection.execute(
            "SELECT truncated, original_bytes FROM tool_events WHERE event_id=?",
            (event.event_id,),
        ).fetchone()
        assert row["truncated"] == 1
        assert row["original_bytes"] == 900_000


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


class TestApprovals:
    def test_pending_approvals_are_listed(self, store: StateStore, task: TaskRecord) -> None:
        store.request_approval(
            ApprovalRecord(
                approval_id=ApprovalId("a1"),
                task_id=task.task_id,
                step_id=None,
                reason="delete a migration",
                risk=Risk.HIGH,
            )
        )
        pending = store.pending_approvals(task.task_id)
        assert len(pending) == 1
        assert pending[0].grants_permission is False

    def test_approval_grants_permission(self, store: StateStore, task: TaskRecord) -> None:
        approval = store.request_approval(
            ApprovalRecord(
                approval_id=ApprovalId("a1"),
                task_id=task.task_id,
                step_id=None,
                reason="run a migration",
                risk=Risk.HIGH,
            )
        )
        decided = store.decide_approval(approval, ApprovalDecision.APPROVED)
        assert decided.grants_permission is True
        assert decided.decided_at is not None
        assert store.pending_approvals(task.task_id) == []

    @pytest.mark.parametrize("decision", [ApprovalDecision.DENIED, ApprovalDecision.TIMED_OUT])
    def test_silence_and_refusal_are_never_consent(
        self, store: StateStore, task: TaskRecord, decision: ApprovalDecision
    ) -> None:
        approval = store.request_approval(
            ApprovalRecord(
                approval_id=ApprovalId("a1"),
                task_id=task.task_id,
                step_id=None,
                reason="drop a table",
                risk=Risk.CRITICAL,
            )
        )
        assert store.decide_approval(approval, decision).grants_permission is False


# ---------------------------------------------------------------------------
# Checkpoints and resume
# ---------------------------------------------------------------------------


class TestCheckpoints:
    def test_a_task_resumes_from_its_checkpoint(self, store: StateStore, task: TaskRecord) -> None:
        mid = store.save_task(
            task.model_copy(
                update={
                    "state": AgentState.EXECUTE_CURRENT_STEP,
                    "phase": Phase.AUTHOR,
                    "action_count": 3,
                    "kind": TaskKind.PLANNED,
                }
            )
        )
        store.create_checkpoint(
            CheckpointRecord(
                checkpoint_id=CheckpointId("c1"),
                task_id=task.task_id,
                step_id=None,
                label="after step 1",
                git_ref="abc123",
                state_snapshot_json=mid.model_dump_json(),
            )
        )

        resumed = store.resume_task(task.task_id)
        assert resumed is not None
        assert resumed.state is AgentState.EXECUTE_CURRENT_STEP
        assert resumed.phase is Phase.AUTHOR
        assert resumed.action_count == 3
        assert resumed.kind is TaskKind.PLANNED

    def test_resume_returns_none_when_there_is_nothing_to_resume(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """The caller decides whether to start over; the store does not guess."""
        assert store.resume_task(task.task_id) is None

    def test_the_latest_checkpoint_wins(self, store: StateStore, task: TaskRecord) -> None:
        for index, label in enumerate(["first", "second", "third"]):
            store.create_checkpoint(
                CheckpointRecord(
                    checkpoint_id=CheckpointId(f"c{index}"),
                    task_id=task.task_id,
                    step_id=None,
                    label=label,
                    state_snapshot_json=task.model_copy(
                        update={"action_count": index}
                    ).model_dump_json(),
                )
            )
        latest = store.latest_checkpoint(task.task_id)
        assert latest is not None
        assert latest.label == "third"

        resumed = store.resume_task(task.task_id)
        assert resumed is not None
        assert resumed.action_count == 2


# ---------------------------------------------------------------------------
# Failures and repair bounding
# ---------------------------------------------------------------------------


def _failure(task: TaskRecord, signature: str, attempt: int = 1) -> FailureRecord:
    return FailureRecord(
        failure_id=FailureId(new_id()),
        task_id=task.task_id,
        step_id=None,
        kind=FailureKind.TEST_FAILURE,
        signature=signature,
        expected="12 passed",
        actual="10 passed, 2 failed",
        attempt=attempt,
    )


class TestFailures:
    def test_a_single_failure_does_not_stop_repair(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        store.record_failure(_failure(task, "assert-login-401"))
        assert store.repeated_failure(task.task_id, "assert-login-401") is False

    def test_the_same_signature_twice_stops_repair(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """Identical signatures mean the attempts are not making progress."""
        store.record_failure(_failure(task, "assert-login-401", attempt=1))
        store.record_failure(_failure(task, "assert-login-401", attempt=2))
        assert store.repeated_failure(task.task_id, "assert-login-401") is True

    def test_different_signatures_indicate_progress(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        store.record_failure(_failure(task, "assert-login-401"))
        store.record_failure(_failure(task, "assert-login-500"))
        assert store.repeated_failure(task.task_id, "assert-login-401") is False
        assert store.repeated_failure(task.task_id, "assert-login-500") is False
        assert store.failure_count(task.task_id) == 2

    def test_signatures_are_scoped_per_task(
        self, store: StateStore, project: ProjectRecord, task: TaskRecord
    ) -> None:
        """Another task's history must not end this task's repair budget."""
        other = store.create_task(
            TaskRecord(
                task_id=TaskId("t2"), project_id=project.project_id, request="something else"
            )
        )
        store.record_failure(_failure(task, "shared-signature"))
        store.record_failure(_failure(other, "shared-signature"))
        assert store.repeated_failure(task.task_id, "shared-signature") is False


class TestMemoryMigration:
    """Migration 3 must land on an existing database, not only a fresh one."""

    def test_an_existing_database_upgrades_in_place(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"

        # Build a database at the pre-memory schema, with data in it.
        connection = connect(str(path))
        for index in range(2):
            connection.executescript(MIGRATIONS[index])
            connection.execute(f"PRAGMA user_version = {index + 1}")
        connection.execute(
            "INSERT INTO projects (project_id, root, name, created_at, updated_at)"
            " VALUES ('p1', '/w', 'demo', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        connection.commit()
        connection.close()

        with StateStore(path) as store:
            assert current_version(store.connection) == SCHEMA_VERSION
            project = store.get_project(ProjectId("p1"))
            assert project is not None, "existing rows must survive the migration"

            memory = MemoryStore(store, project.project_id)
            fact = memory.learn(FactKind.STACK, "runner", "pytest", origin=FactOrigin.USER)
            assert memory.fact(FactKind.STACK, "runner") == fact

    def test_the_memory_tables_exist_after_migration(self, store: StateStore) -> None:
        names = {
            row["name"]
            for row in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"project_facts", "architecture_decisions", "memory_records"} <= names

    def test_a_fact_cannot_cite_a_tool_event_that_does_not_exist(
        self, store: StateStore, project: ProjectRecord
    ) -> None:
        """The same integrity rule as evidence: provenance is a foreign key."""
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            store.connection.execute(
                "INSERT INTO project_facts (fact_id, project_id, kind, subject, statement,"
                " origin, source_event_id, confidence, created_at, updated_at)"
                " VALUES ('f1', ?, 'stack', 's', 'x', 'observed', 'invented', 0.8,"
                " '2026-01-01T00:00:00', '2026-01-01T00:00:00')",
                (project.project_id,),
            )
