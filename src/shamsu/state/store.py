"""The authoritative SQLite state store.

Two properties this type is responsible for, beyond persistence:

1. **Transitions are validated on write.** ``advance_task`` consults the
   transition table; an illegal move raises instead of being persisted. There
   is no way to put a task into an unreachable state through this API.
2. **Evidence is non-forgeable.** ``record_evidence`` takes a tool event id and
   the schema enforces the foreign key, so evidence cannot exist without an
   observed tool execution behind it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from shamsu.interfaces.enums import (
    AgentState,
    ApprovalDecision,
    EvidenceKind,
    Phase,
    RunStatus,
    StepOutcome,
)
from shamsu.interfaces.ids import (
    EvidenceId,
    PlanId,
    ProjectId,
    RunId,
    StepId,
    TaskId,
    ToolEventId,
)
from shamsu.state.records import (
    ApprovalRecord,
    CheckpointRecord,
    EvidenceRecord,
    FailureRecord,
    PlanRecord,
    PlanStepRecord,
    ProjectRecord,
    RunRecord,
    TaskRecord,
    ToolEventRecord,
    utcnow,
)
from shamsu.state.schema import connect, migrate
from shamsu.state.transitions import assert_transition


def _dump_list(values: Sequence[Any]) -> str:
    return json.dumps([str(value) for value in values])


def _load_tuple(raw: str) -> tuple[str, ...]:
    return tuple(json.loads(raw))


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


class StateStore:
    """SQLite-backed authoritative state.

    Not thread-safe by construction; a run owns its store. Concurrent readers
    are supported through WAL, which is what the status/watch path uses.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = connect(self._path)
        migrate(self._connection)

    # -- lifecycle ---------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group several writes so a partial update cannot be observed."""
        with self._connection:
            yield self._connection

    # -- projects ----------------------------------------------------------

    def upsert_project(self, project: ProjectRecord) -> ProjectRecord:
        record = project.model_copy(update={"updated_at": utcnow()})
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO projects (
                    project_id, root, name, languages, frameworks, package_managers,
                    database_types, test_commands, active_branch, index_version,
                    artifact_version, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id) DO UPDATE SET
                    root=excluded.root, name=excluded.name,
                    languages=excluded.languages, frameworks=excluded.frameworks,
                    package_managers=excluded.package_managers,
                    database_types=excluded.database_types,
                    test_commands=excluded.test_commands,
                    active_branch=excluded.active_branch,
                    index_version=excluded.index_version,
                    artifact_version=excluded.artifact_version,
                    updated_at=excluded.updated_at
                """,
                (
                    record.project_id,
                    record.root,
                    record.name,
                    _dump_list(record.languages),
                    _dump_list(record.frameworks),
                    _dump_list(record.package_managers),
                    _dump_list(record.database_types),
                    _dump_list(record.test_commands),
                    record.active_branch,
                    record.index_version,
                    record.artifact_version,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def get_project(self, project_id: ProjectId) -> ProjectRecord | None:
        row = self._connection.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        return ProjectRecord(
            project_id=ProjectId(row["project_id"]),
            root=row["root"],
            name=row["name"],
            languages=_load_tuple(row["languages"]),
            frameworks=_load_tuple(row["frameworks"]),
            package_managers=_load_tuple(row["package_managers"]),
            database_types=_load_tuple(row["database_types"]),
            test_commands=_load_tuple(row["test_commands"]),
            active_branch=row["active_branch"],
            index_version=row["index_version"],
            artifact_version=row["artifact_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # -- tasks -------------------------------------------------------------

    def create_task(self, task: TaskRecord) -> TaskRecord:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO tasks (
                    task_id, project_id, request, kind, state, phase, plan_id,
                    current_step_id, action_count, repair_count, replan_count,
                    consecutive_failures, final_result, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task.task_id,
                    task.project_id,
                    task.request,
                    task.kind.value if task.kind else None,
                    task.state.value,
                    task.phase.value,
                    task.plan_id,
                    task.current_step_id,
                    task.action_count,
                    task.repair_count,
                    task.replan_count,
                    task.consecutive_failures,
                    task.final_result,
                    task.created_at.isoformat(),
                    task.updated_at.isoformat(),
                ),
            )
        return task

    def get_task(self, task_id: TaskId) -> TaskRecord | None:
        row = self._connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return self._task_from_row(row) if row else None

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        from shamsu.interfaces.enums import TaskKind

        return TaskRecord(
            task_id=TaskId(row["task_id"]),
            project_id=ProjectId(row["project_id"]),
            request=row["request"],
            kind=TaskKind(row["kind"]) if row["kind"] else None,
            state=AgentState(row["state"]),
            phase=Phase(row["phase"]),
            plan_id=PlanId(row["plan_id"]) if row["plan_id"] else None,
            current_step_id=StepId(row["current_step_id"]) if row["current_step_id"] else None,
            action_count=row["action_count"],
            repair_count=row["repair_count"],
            replan_count=row["replan_count"],
            consecutive_failures=row["consecutive_failures"],
            final_result=row["final_result"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_task(self, task: TaskRecord) -> TaskRecord:
        """Persist a task without validating a state change.

        For counter and pointer updates. Use `advance_task` to change `state` --
        this method does not check the transition table.
        """
        record = task.model_copy(update={"updated_at": utcnow()})
        with self._connection:
            self._connection.execute(
                """
                UPDATE tasks SET
                    request=?, kind=?, state=?, phase=?, plan_id=?, current_step_id=?,
                    action_count=?, repair_count=?, replan_count=?,
                    consecutive_failures=?, final_result=?, updated_at=?
                WHERE task_id=?
                """,
                (
                    record.request,
                    record.kind.value if record.kind else None,
                    record.state.value,
                    record.phase.value,
                    record.plan_id,
                    record.current_step_id,
                    record.action_count,
                    record.repair_count,
                    record.replan_count,
                    record.consecutive_failures,
                    record.final_result,
                    record.updated_at.isoformat(),
                    record.task_id,
                ),
            )
        return record

    def advance_task(
        self,
        task: TaskRecord,
        target: AgentState,
        *,
        phase: Phase | None = None,
        cancelling: bool = False,
    ) -> TaskRecord:
        """Move a task to `target`, validating the transition first.

        This is the only supported way to change `TaskRecord.state`.

        Raises:
            InvalidTransition: the move is not in the table. Nothing is written.
        """
        assert_transition(task.state, target, cancelling=cancelling)
        updates: dict[str, Any] = {"state": target}
        if phase is not None:
            updates["phase"] = phase
        return self.save_task(task.model_copy(update=updates))

    # -- runs --------------------------------------------------------------

    def create_run(self, run: RunRecord) -> RunRecord:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO runs (
                    run_id, project_id, task_id, status, started_at, ended_at,
                    wall_clock_limit_seconds, cancel_reason
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    run.run_id,
                    run.project_id,
                    run.task_id,
                    run.status.value,
                    run.started_at.isoformat(),
                    _dt(run.ended_at),
                    run.wall_clock_limit_seconds,
                    run.cancel_reason,
                ),
            )
        return run

    def save_run(self, run: RunRecord) -> RunRecord:
        with self._connection:
            self._connection.execute(
                """
                UPDATE runs SET status=?, ended_at=?, wall_clock_limit_seconds=?,
                                cancel_reason=?
                WHERE run_id=?
                """,
                (
                    run.status.value,
                    _dt(run.ended_at),
                    run.wall_clock_limit_seconds,
                    run.cancel_reason,
                    run.run_id,
                ),
            )
        return run

    def get_run(self, run_id: RunId) -> RunRecord | None:
        row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return RunRecord(
            run_id=RunId(row["run_id"]),
            project_id=ProjectId(row["project_id"]),
            task_id=TaskId(row["task_id"]),
            status=RunStatus(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=_parse_dt(row["ended_at"]),
            wall_clock_limit_seconds=row["wall_clock_limit_seconds"],
            cancel_reason=row["cancel_reason"],
        )

    def active_runs(self) -> Sequence[RunRecord]:
        """Runs that have not finished.

        The basis of "every live run must be observable" -- a run that cannot
        be listed cannot be cancelled.
        """
        rows = self._connection.execute(
            "SELECT run_id FROM runs WHERE status IN (?,?,?,?)",
            (
                RunStatus.PENDING.value,
                RunStatus.RUNNING.value,
                RunStatus.PAUSED.value,
                RunStatus.WAITING_APPROVAL.value,
            ),
        ).fetchall()
        runs = [self.get_run(RunId(row["run_id"])) for row in rows]
        return [run for run in runs if run is not None]

    # -- plans -------------------------------------------------------------

    def create_plan(self, plan: PlanRecord, steps: Sequence[PlanStepRecord]) -> PlanRecord:
        """Insert a plan and its steps atomically.

        A plan without its steps is worse than no plan: the runtime would think
        it had one and find nothing to execute.
        """
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO plans (plan_id, task_id, version, summary, superseded_by, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    plan.plan_id,
                    plan.task_id,
                    plan.version,
                    plan.summary,
                    plan.superseded_by,
                    plan.created_at.isoformat(),
                ),
            )
            for step in steps:
                self._connection.execute(
                    """
                    INSERT INTO plan_steps (
                        step_id, plan_id, ordinal, title, inputs, outputs, constraints,
                        allowed_tools, acceptance_criteria, required_evidence, risk,
                        approval_required, outcome, attempts, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        step.step_id,
                        step.plan_id,
                        step.ordinal,
                        step.title,
                        _dump_list(step.inputs),
                        _dump_list(step.outputs),
                        _dump_list(step.constraints),
                        _dump_list(step.allowed_tools),
                        _dump_list(step.acceptance_criteria),
                        _dump_list([e.value for e in step.required_evidence]),
                        step.risk.value,
                        int(step.approval_required),
                        step.outcome.value if step.outcome else None,
                        step.attempts,
                        step.created_at.isoformat(),
                    ),
                )
        return plan

    def get_steps(self, plan_id: PlanId) -> Sequence[PlanStepRecord]:
        from shamsu.interfaces.enums import Risk

        rows = self._connection.execute(
            "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY ordinal", (plan_id,)
        ).fetchall()
        return [
            PlanStepRecord(
                step_id=StepId(row["step_id"]),
                plan_id=PlanId(row["plan_id"]),
                ordinal=row["ordinal"],
                title=row["title"],
                inputs=_load_tuple(row["inputs"]),
                outputs=_load_tuple(row["outputs"]),
                constraints=_load_tuple(row["constraints"]),
                allowed_tools=_load_tuple(row["allowed_tools"]),
                acceptance_criteria=_load_tuple(row["acceptance_criteria"]),
                required_evidence=tuple(
                    EvidenceKind(value) for value in json.loads(row["required_evidence"])
                ),
                risk=Risk(row["risk"]),
                approval_required=bool(row["approval_required"]),
                outcome=StepOutcome(row["outcome"]) if row["outcome"] else None,
                attempts=row["attempts"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def save_step(self, step: PlanStepRecord) -> PlanStepRecord:
        with self._connection:
            self._connection.execute(
                "UPDATE plan_steps SET outcome=?, attempts=? WHERE step_id=?",
                (
                    step.outcome.value if step.outcome else None,
                    step.attempts,
                    step.step_id,
                ),
            )
        return step

    # -- tool events and evidence -----------------------------------------

    def record_tool_event(self, event: ToolEventRecord) -> ToolEventRecord:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO tool_events (
                    event_id, run_id, task_id, step_id, tool, phase, arguments_json,
                    ok, output, error, truncated, original_bytes, duration_seconds,
                    created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event.event_id,
                    event.run_id,
                    event.task_id,
                    event.step_id,
                    event.tool,
                    event.phase.value,
                    event.arguments_json,
                    int(event.ok),
                    event.output,
                    event.error,
                    int(event.truncated),
                    event.original_bytes,
                    event.duration_seconds,
                    event.created_at.isoformat(),
                ),
            )
        return event

    def record_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord:
        """Register verified evidence.

        The `source_event_id` foreign key is enforced by the schema, so this
        will fail if the tool event does not exist. That is the point: there is
        no path from a model assertion to a row in this table.
        """
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO evidence (
                    evidence_id, task_id, step_id, kind, source_event_id, detail, recorded_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    evidence.evidence_id,
                    evidence.task_id,
                    evidence.step_id,
                    evidence.kind.value,
                    evidence.source_event_id,
                    evidence.detail,
                    evidence.recorded_at.isoformat(),
                ),
            )
        return evidence

    def verified_evidence(
        self, task_id: TaskId, step_id: StepId | None = None
    ) -> frozenset[EvidenceKind]:
        """Evidence kinds actually registered, for the completion gate."""
        if step_id is None:
            rows = self._connection.execute(
                "SELECT DISTINCT kind FROM evidence WHERE task_id = ?", (task_id,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT DISTINCT kind FROM evidence WHERE task_id = ? AND step_id = ?",
                (task_id, step_id),
            ).fetchall()
        return frozenset(EvidenceKind(row["kind"]) for row in rows)

    def evidence_for(
        self, task_id: TaskId, step_id: StepId | None = None
    ) -> Sequence[EvidenceRecord]:
        query = "SELECT * FROM evidence WHERE task_id = ?"
        params: tuple[Any, ...] = (task_id,)
        if step_id is not None:
            query += " AND step_id = ?"
            params += (step_id,)
        rows = self._connection.execute(query + " ORDER BY recorded_at", params).fetchall()
        return [
            EvidenceRecord(
                evidence_id=EvidenceId(row["evidence_id"]),
                task_id=TaskId(row["task_id"]),
                step_id=StepId(row["step_id"]) if row["step_id"] else None,
                kind=EvidenceKind(row["kind"]),
                source_event_id=ToolEventId(row["source_event_id"]),
                detail=row["detail"],
                recorded_at=datetime.fromisoformat(row["recorded_at"]),
            )
            for row in rows
        ]

    # -- approvals ---------------------------------------------------------

    def request_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO approvals (
                    approval_id, task_id, step_id, reason, risk, decision,
                    requested_at, decided_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    approval.approval_id,
                    approval.task_id,
                    approval.step_id,
                    approval.reason,
                    approval.risk.value,
                    approval.decision.value,
                    approval.requested_at.isoformat(),
                    _dt(approval.decided_at),
                ),
            )
        return approval

    def decide_approval(
        self, approval: ApprovalRecord, decision: ApprovalDecision
    ) -> ApprovalRecord:
        record = approval.model_copy(update={"decision": decision, "decided_at": utcnow()})
        with self._connection:
            self._connection.execute(
                "UPDATE approvals SET decision=?, decided_at=? WHERE approval_id=?",
                (record.decision.value, _dt(record.decided_at), record.approval_id),
            )
        return record

    def pending_approvals(self, task_id: TaskId) -> Sequence[ApprovalRecord]:
        from shamsu.interfaces.enums import Risk
        from shamsu.interfaces.ids import ApprovalId

        rows = self._connection.execute(
            "SELECT * FROM approvals WHERE task_id = ? AND decision = ?",
            (task_id, ApprovalDecision.PENDING.value),
        ).fetchall()
        return [
            ApprovalRecord(
                approval_id=ApprovalId(row["approval_id"]),
                task_id=TaskId(row["task_id"]),
                step_id=StepId(row["step_id"]) if row["step_id"] else None,
                reason=row["reason"],
                risk=Risk(row["risk"]),
                decision=ApprovalDecision(row["decision"]),
                requested_at=datetime.fromisoformat(row["requested_at"]),
                decided_at=_parse_dt(row["decided_at"]),
            )
            for row in rows
        ]

    # -- checkpoints -------------------------------------------------------

    def create_checkpoint(self, checkpoint: CheckpointRecord) -> CheckpointRecord:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO checkpoints (
                    checkpoint_id, task_id, step_id, label, git_ref,
                    state_snapshot_json, created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.task_id,
                    checkpoint.step_id,
                    checkpoint.label,
                    checkpoint.git_ref,
                    checkpoint.state_snapshot_json,
                    checkpoint.created_at.isoformat(),
                ),
            )
        return checkpoint

    def latest_checkpoint(self, task_id: TaskId) -> CheckpointRecord | None:
        """The most recent checkpoint -- the resume point."""
        from shamsu.interfaces.ids import CheckpointId

        row = self._connection.execute(
            "SELECT * FROM checkpoints WHERE task_id = ? ORDER BY created_at DESC, rowid DESC"
            " LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return None
        return CheckpointRecord(
            checkpoint_id=CheckpointId(row["checkpoint_id"]),
            task_id=TaskId(row["task_id"]),
            step_id=StepId(row["step_id"]) if row["step_id"] else None,
            label=row["label"],
            git_ref=row["git_ref"],
            state_snapshot_json=row["state_snapshot_json"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def resume_task(self, task_id: TaskId) -> TaskRecord | None:
        """Reconstruct a task from its latest checkpoint.

        Returns None when there is nothing to resume from. The caller decides
        whether to start fresh -- silently starting over would discard work the
        user may have wanted back.
        """
        checkpoint = self.latest_checkpoint(task_id)
        if checkpoint is None:
            return None
        return TaskRecord.model_validate_json(checkpoint.state_snapshot_json)

    # -- failures ----------------------------------------------------------

    def record_failure(self, failure: FailureRecord) -> FailureRecord:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO failures (
                    failure_id, task_id, step_id, kind, signature, expected, actual,
                    detail, attempt, created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    failure.failure_id,
                    failure.task_id,
                    failure.step_id,
                    failure.kind.value,
                    failure.signature,
                    failure.expected,
                    failure.actual,
                    failure.detail,
                    failure.attempt,
                    failure.created_at.isoformat(),
                ),
            )
        return failure

    def repeated_failure(self, task_id: TaskId, signature: str, *, threshold: int = 2) -> bool:
        """Whether `signature` has recurred enough to stop repairing.

        The check that ends a grinding repair loop: identical signatures mean
        the attempts are not making progress, so further budget is wasted.
        """
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM failures WHERE task_id = ? AND signature = ?",
            (task_id, signature),
        ).fetchone()
        return int(row["n"]) >= threshold

    def failure_count(self, task_id: TaskId) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM failures WHERE task_id = ?", (task_id,)
        ).fetchone()
        return int(row["n"])


__all__ = ["StateStore"]
