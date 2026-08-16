"""SQLite-backed runtime state for resumable production agent tasks.

This is the authoritative task execution state. Chat history, action-ledger
events, and Graphiti memories may help explain a run, but they are not the
source of truth for whether a task is running, cancelled, completed, or what
evidence and file changes have landed.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from shamsu.runtime.failures import FailureRecord, FailureType, make_error_signature, normalize_failure_type
from shamsu.safety.sandbox import Sandbox
from shamsu.types import RunStatus, TaskStepStatus

RUNTIME_STATE_DB = "runtime-state.db"
SCHEMA_VERSION = 1


class RuntimeStateError(RuntimeError):
    pass


class InvalidStateTransition(RuntimeStateError):
    pass


class CorruptRuntimeState(RuntimeStateError):
    pass


class EvidenceType(str, Enum):
    FILE_CHANGED = "file_changed"
    GIT_DIFF_REVIEWED = "git_diff_reviewed"
    TEST_PASSED = "test_passed"
    TYPECHECK_PASSED = "typecheck_passed"
    LINT_PASSED = "lint_passed"
    BUILD_PASSED = "build_passed"
    SERVICE_HEALTHY = "service_healthy"
    MIGRATION_PASSED = "migration_passed"
    SMOKE_TEST_PASSED = "smoke_test_passed"


class EvidenceStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    STALE = "stale"


class PlanStepStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    BLOCKED = "blocked"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _json_default(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=_json_default)


def _loads(raw: str, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CorruptRuntimeState(f"Corrupt {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CorruptRuntimeState(f"Corrupt {label}: expected JSON object")
    return payload


@dataclass
class RunState:
    run_id: str
    status: RunStatus = RunStatus.CREATED
    task_ids: list[str] = field(default_factory=list)
    current_task_id: str = ""
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    deadline_at: float | None = None
    last_checkpoint: str = ""


@dataclass
class StepState:
    step_id: str
    task_id: str
    run_id: str
    status: TaskStepStatus = TaskStepStatus.PENDING
    phase: str = "default"
    description: str = ""
    tool_name: str = ""
    tool_call: dict[str, Any] = field(default_factory=dict)
    tool_result: dict[str, Any] = field(default_factory=dict)
    required_evidence: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    completed_at: str = ""


@dataclass
class RepairState:
    repair_id: str
    task_id: str
    run_id: str
    status: RunStatus = RunStatus.CREATED
    attempt: int = 0
    target_files: list[str] = field(default_factory=list)
    last_error: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass
class EvidenceRecord:
    evidence_id: str
    task_id: str
    step_id: str
    evidence_type: EvidenceType
    source_tool: str
    timestamp: str = field(default_factory=_now)
    status: EvidenceStatus = EvidenceStatus.PASSED
    details: dict[str, Any] = field(default_factory=dict)
    related_files: list[str] = field(default_factory=list)
    related_command: str = ""
    exit_code: int | None = None
    checkpoint_id: str = ""


@dataclass(frozen=True)
class CompletionGateResult:
    ok: bool
    task_id: str
    step_id: str = ""
    missing_evidence: tuple[str, ...] = ()
    stale_evidence: tuple[str, ...] = ()
    failed_evidence: tuple[str, ...] = ()
    incomplete_steps: tuple[str, ...] = ()
    message: str = ""


@dataclass
class PlanStep:
    step_id: str
    title: str
    goal: str
    inputs: list[str] = field(default_factory=list)
    expected_outputs: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    approval_required: bool = False
    status: PlanStepStatus = PlanStepStatus.PENDING
    dependencies: list[str] = field(default_factory=list)


@dataclass
class ExecutionPlan:
    plan_id: str
    task_id: str
    run_id: str
    title: str
    summary: str
    steps: list[PlanStep]
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def compact_summary(self) -> str:
        counts: dict[str, int] = {}
        for step in self.steps:
            counts[step.status.value] = counts.get(step.status.value, 0) + 1
        status = ", ".join(f"{key}:{value}" for key, value in sorted(counts.items()))
        return f"{self.title}: {len(self.steps)} step(s)" + (f" ({status})" if status else "")

    def completed_summary(self) -> str:
        completed = [step.title for step in self.steps if step.status == PlanStepStatus.COMPLETED]
        if not completed:
            return "No completed plan steps yet."
        return "Completed: " + "; ".join(completed)


@dataclass(frozen=True)
class PlanValidationResult:
    ok: bool
    errors: tuple[str, ...] = ()


@dataclass
class TaskState:
    task_id: str
    run_id: str
    project_id: str
    user_request: str
    status: RunStatus = RunStatus.CREATED
    current_phase: str = "initialized"
    current_step_id: str = ""
    plan_id: str = ""
    action_count: int = 0
    repair_count: int = 0
    replan_count: int = 0
    changed_files: list[str] = field(default_factory=list)
    required_evidence: list[str] = field(default_factory=list)
    collected_evidence: list[str] = field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    last_tool_call: dict[str, Any] = field(default_factory=dict)
    last_tool_result: dict[str, Any] = field(default_factory=dict)
    last_checkpoint: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


@dataclass(frozen=True)
class RuntimeTaskContext:
    store: "RuntimeStateStore"
    task_id: str


_CURRENT_TASK: contextvars.ContextVar[RuntimeTaskContext | None] = contextvars.ContextVar(
    "shamsu_runtime_task",
    default=None,
)


@contextlib.contextmanager
def bind_task_state(store: "RuntimeStateStore", task_id: str):
    token = _CURRENT_TASK.set(RuntimeTaskContext(store=store, task_id=task_id))
    try:
        yield
    finally:
        _CURRENT_TASK.reset(token)


def current_task_context() -> RuntimeTaskContext | None:
    return _CURRENT_TASK.get()


class RuntimeStateStore:
    def __init__(self, workspace: Path, db_path: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.db_path = Path(db_path) if db_path is not None else self._default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _default_db_path(self) -> Path:
        return Sandbox(self.workspace).validate(Path(".shamsu") / RUNTIME_STATE_DB)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_phase TEXT NOT NULL,
                    current_step_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_checkpoint TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runtime_tasks_run ON tasks(run_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS steps (
                    step_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (task_id, step_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS repairs (
                    repair_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    evidence_type TEXT NOT NULL,
                    source_tool TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    status TEXT NOT NULL,
                    related_command TEXT NOT NULL,
                    exit_code INTEGER,
                    checkpoint_id TEXT NOT NULL,
                    signature TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_evidence_task_step ON evidence(task_id, step_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_plans (
                    plan_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_execution_plans_task ON execution_plans(task_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS failures (
                    task_id TEXT NOT NULL,
                    error_signature TEXT NOT NULL,
                    failure_type TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    retry_count INTEGER NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (task_id, error_signature)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_failures_task_type ON failures(task_id, failure_type)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    checkpoint_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    task_payload TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_checkpoints_task ON checkpoints(task_id, id)")
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def create_run(
        self,
        run_id: str,
        *,
        status: RunStatus = RunStatus.CREATED,
        deadline_at: float | None = None,
    ) -> RunState:
        state = RunState(run_id=run_id, status=status, deadline_at=deadline_at)
        self.save_run(state)
        return state

    def save_run(self, state: RunState) -> RunState:
        state.updated_at = _now()
        payload = _state_to_dict(state)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, status, started_at, updated_at, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (
                    state.run_id,
                    state.status.value,
                    state.started_at,
                    state.updated_at,
                    _dumps(payload),
                ),
            )
        return state

    def load_run(self, run_id: str) -> RunState | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return _run_from_dict(_loads(row["payload"], label=f"run {run_id}"))

    def create_task(
        self,
        *,
        run_id: str,
        user_request: str,
        project_id: str = "",
        task_id: str | None = None,
        plan_id: str = "",
    ) -> TaskState:
        state = TaskState(
            task_id=task_id or _new_id("task"),
            run_id=run_id,
            project_id=project_id,
            user_request=user_request,
            plan_id=plan_id,
            status=RunStatus.CREATED,
            current_phase="initialized",
        )
        self.save_task(state, checkpoint_kind="task_initialized")
        run = self.load_run(run_id) or self.create_run(run_id)
        if state.task_id not in run.task_ids:
            run.task_ids.append(state.task_id)
        run.current_task_id = state.task_id
        run.last_checkpoint = state.last_checkpoint
        self.save_run(run)
        return state

    def load_task(self, task_id: str, *, recover: bool = True) -> TaskState | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return self.recover_latest_checkpoint(task_id) if recover else None
        try:
            return _task_from_dict(_loads(row["payload"], label=f"task {task_id}"))
        except CorruptRuntimeState:
            if recover:
                return self.recover_latest_checkpoint(task_id)
            raise

    def save_task(
        self,
        state: TaskState,
        *,
        checkpoint_kind: str | None = None,
        expected_previous: RunStatus | None = None,
        allow_completion: bool = False,
    ) -> TaskState:
        existing = self.load_task(state.task_id, recover=False)
        if state.status == RunStatus.COMPLETED and not allow_completion:
            raise InvalidStateTransition(
                "Task completion must go through request_task_complete()."
            )
        if existing is not None:
            if expected_previous is not None and existing.status != expected_previous:
                raise InvalidStateTransition(
                    f"Expected {expected_previous.value}, found {existing.status.value}"
                )
            _validate_transition(existing.status, state.status)
            if not state.created_at:
                state.created_at = existing.created_at
        state.updated_at = _now()
        if checkpoint_kind:
            state.last_checkpoint = f"{checkpoint_kind}:{state.updated_at}"
        payload = _state_to_dict(state)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, run_id, status, current_phase, current_step_id,
                    updated_at, last_checkpoint, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    run_id=excluded.run_id,
                    status=excluded.status,
                    current_phase=excluded.current_phase,
                    current_step_id=excluded.current_step_id,
                    updated_at=excluded.updated_at,
                    last_checkpoint=excluded.last_checkpoint,
                    payload=excluded.payload
                """,
                (
                    state.task_id,
                    state.run_id,
                    state.status.value,
                    state.current_phase,
                    state.current_step_id,
                    state.updated_at,
                    state.last_checkpoint,
                    _dumps(payload),
                ),
            )
            if checkpoint_kind:
                conn.execute(
                    """
                    INSERT INTO checkpoints (
                        task_id, run_id, checkpoint_id, kind, created_at, task_payload
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state.task_id,
                        state.run_id,
                        state.last_checkpoint,
                        checkpoint_kind,
                        state.updated_at,
                        _dumps(payload),
                    ),
                )
        return state

    def update_task_status(
        self,
        task_id: str,
        status: RunStatus,
        *,
        phase: str | None = None,
        checkpoint_kind: str | None = None,
    ) -> TaskState:
        state = self.require_task(task_id)
        state.status = status
        if phase is not None:
            state.current_phase = phase
        return self.save_task(state, checkpoint_kind=checkpoint_kind)

    def require_task(self, task_id: str) -> TaskState:
        state = self.load_task(task_id)
        if state is None:
            raise RuntimeStateError(f"Task state not found: {task_id}")
        return state

    def list_tasks_for_run(self, run_id: str) -> list[TaskState]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM tasks WHERE run_id = ? ORDER BY updated_at",
                (run_id,),
            ).fetchall()
        return [_task_from_dict(_loads(row["payload"], label=f"task for run {run_id}")) for row in rows]

    def record_step(self, step: StepState) -> StepState:
        step.updated_at = _now()
        payload = _state_to_dict(step)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO steps (step_id, task_id, run_id, status, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, step_id) DO UPDATE SET
                    run_id=excluded.run_id,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (
                    step.step_id,
                    step.task_id,
                    step.run_id,
                    step.status.value,
                    step.updated_at,
                    _dumps(payload),
                ),
            )
        return step

    def record_repair(self, repair: RepairState) -> RepairState:
        repair.updated_at = _now()
        payload = _state_to_dict(repair)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO repairs (repair_id, task_id, run_id, status, updated_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(repair_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    run_id=excluded.run_id,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (
                    repair.repair_id,
                    repair.task_id,
                    repair.run_id,
                    repair.status.value,
                    repair.updated_at,
                    _dumps(payload),
                ),
            )
        return repair

    def save_execution_plan(
        self,
        plan: ExecutionPlan,
        *,
        valid_tool_names: set[str] | None = None,
    ) -> ExecutionPlan:
        validation = validate_execution_plan(plan, valid_tool_names=valid_tool_names)
        if not validation.ok:
            raise InvalidStateTransition("; ".join(validation.errors))
        plan.updated_at = _now()
        payload = _state_to_dict(plan)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO execution_plans (plan_id, task_id, run_id, updated_at, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    run_id=excluded.run_id,
                    updated_at=excluded.updated_at,
                    payload=excluded.payload
                """,
                (plan.plan_id, plan.task_id, plan.run_id, plan.updated_at, _dumps(payload)),
            )
        state = self.require_task(plan.task_id)
        state.plan_id = plan.plan_id
        state.current_phase = "planned"
        self.save_task(state, checkpoint_kind="plan_contract_saved")
        return plan

    def load_execution_plan(self, plan_id: str) -> ExecutionPlan | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM execution_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        if row is None:
            return None
        return _execution_plan_from_dict(_loads(row["payload"], label=f"plan {plan_id}"))

    def load_task_plan(self, task_id: str) -> ExecutionPlan | None:
        state = self.load_task(task_id)
        if state is not None and state.plan_id:
            return self.load_execution_plan(state.plan_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload FROM execution_plans
                WHERE task_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return _execution_plan_from_dict(_loads(row["payload"], label=f"plan for {task_id}"))

    def current_active_step(self, task_id: str) -> PlanStep | None:
        plan = self.load_task_plan(task_id)
        if plan is None:
            return None
        active = [step for step in plan.steps if step.status == PlanStepStatus.ACTIVE]
        if active:
            return active[0]
        completed = {step.step_id for step in plan.steps if step.status == PlanStepStatus.COMPLETED}
        blocked_statuses = {
            PlanStepStatus.BLOCKED,
            PlanStepStatus.FAILED,
            PlanStepStatus.CANCELLED,
            PlanStepStatus.VERIFYING,
        }
        for step in plan.steps:
            if step.status in blocked_statuses:
                continue
            if step.status != PlanStepStatus.PENDING:
                continue
            if all(dep in completed for dep in step.dependencies):
                step.status = PlanStepStatus.ACTIVE
                plan.updated_at = _now()
                self._persist_plan(plan)
                if self.load_step(task_id, step.step_id) is None:
                    self.record_step(
                        StepState(
                            step_id=step.step_id,
                            task_id=plan.task_id,
                            run_id=plan.run_id,
                            status=TaskStepStatus.RUNNING,
                            phase="executing_plan",
                            description=step.title,
                            required_evidence=list(step.required_evidence),
                        )
                    )
                state = self.require_task(task_id)
                state.current_step_id = step.step_id
                state.current_phase = "executing_plan"
                self.save_task(state, checkpoint_kind="plan_step_activated")
                return step
        return None

    def update_plan_step_status(
        self,
        task_id: str,
        step_id: str,
        status: PlanStepStatus,
        *,
        checkpoint_kind: str = "plan_step_status_changed",
    ) -> ExecutionPlan:
        plan = self.load_task_plan(task_id)
        if plan is None:
            raise RuntimeStateError(f"Execution plan not found for task: {task_id}")
        target = None
        for step in plan.steps:
            if step.step_id == step_id:
                target = step
                break
        if target is None:
            raise RuntimeStateError(f"Plan step not found: {step_id}")
        _validate_plan_step_transition(target.status, status)
        target.status = status
        plan.updated_at = _now()
        self._persist_plan(plan)
        state = self.require_task(task_id)
        state.current_step_id = step_id
        if status == PlanStepStatus.COMPLETED:
            state.current_phase = "step_completed"
        elif status == PlanStepStatus.BLOCKED:
            state.current_phase = "blocked"
        elif status == PlanStepStatus.ACTIVE:
            state.current_phase = "executing_plan"
        self.save_task(state, checkpoint_kind=checkpoint_kind)
        return plan

    def complete_plan_step(self, task_id: str, step_id: str) -> CompletionGateResult:
        plan = self.load_task_plan(task_id)
        if plan is None:
            return CompletionGateResult(False, task_id, step_id, message="Execution plan not found.")
        step = next((candidate for candidate in plan.steps if candidate.step_id == step_id), None)
        if step is None:
            return CompletionGateResult(False, task_id, step_id, message="Plan step not found.")
        gate = self.request_step_complete(
            task_id,
            step_id,
            required_evidence=step.required_evidence,
        )
        if not gate.ok:
            self.update_plan_step_status(
                task_id,
                step_id,
                PlanStepStatus.VERIFYING,
                checkpoint_kind="plan_step_verifying",
            )
            return gate
        self.update_plan_step_status(
            task_id,
            step_id,
            PlanStepStatus.COMPLETED,
            checkpoint_kind="plan_step_completed",
        )
        return gate

    def _persist_plan(self, plan: ExecutionPlan) -> None:
        payload = _state_to_dict(plan)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE execution_plans
                SET updated_at = ?, payload = ?
                WHERE plan_id = ?
                """,
                (plan.updated_at, _dumps(payload), plan.plan_id),
            )

    def record_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord:
        if not evidence.evidence_id:
            evidence.evidence_id = _new_id("evidence")
        evidence.related_files = sorted(dict.fromkeys(str(path) for path in evidence.related_files))
        payload = _state_to_dict(evidence)
        signature = _evidence_signature(evidence)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT payload FROM evidence WHERE signature = ?",
                (signature,),
            ).fetchone()
            if existing is not None:
                return _evidence_from_dict(
                    _loads(existing["payload"], label=f"evidence {signature}")
                )
            conn.execute(
                """
                INSERT INTO evidence (
                    evidence_id, task_id, step_id, evidence_type, source_tool,
                    timestamp, status, related_command, exit_code, checkpoint_id,
                    signature, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.task_id,
                    evidence.step_id,
                    evidence.evidence_type.value,
                    evidence.source_tool,
                    evidence.timestamp,
                    evidence.status.value,
                    evidence.related_command,
                    evidence.exit_code,
                    evidence.checkpoint_id,
                    signature,
                    _dumps(payload),
                ),
            )
        return evidence

    def list_evidence(
        self,
        task_id: str,
        *,
        step_id: str | None = None,
    ) -> list[EvidenceRecord]:
        sql = "SELECT payload FROM evidence WHERE task_id = ?"
        params: list[Any] = [task_id]
        if step_id is not None:
            sql += " AND step_id = ?"
            params.append(step_id)
        sql += " ORDER BY timestamp, evidence_id"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            _evidence_from_dict(_loads(row["payload"], label=f"evidence for {task_id}"))
            for row in rows
        ]

    def request_step_complete(
        self,
        task_id: str,
        step_id: str,
        *,
        required_evidence: list[EvidenceType | str] | None = None,
    ) -> CompletionGateResult:
        state = self.require_task(task_id)
        step = self.load_step(task_id, step_id)
        if step is None:
            return CompletionGateResult(
                False,
                task_id,
                step_id,
                message=f"Step state not found: {step_id}",
            )
        required = _normalize_evidence_types(required_evidence or step.required_evidence)
        gate = self._check_required_evidence(
            task_id,
            step_id=step_id,
            required=required,
            not_before=step.started_at,
        )
        if not gate.ok:
            return gate
        step.status = TaskStepStatus.DONE
        step.completed_at = _now()
        self.record_step(step)
        state.current_step_id = step_id
        state.current_phase = "running"
        self.save_task(state, checkpoint_kind="step_completed")
        return CompletionGateResult(True, task_id, step_id, message="Step completion verified.")

    def request_task_complete(
        self,
        task_id: str,
        *,
        required_evidence: list[EvidenceType | str] | None = None,
    ) -> CompletionGateResult:
        state = self.require_task(task_id)
        plan = self.load_task_plan(task_id)
        if plan is not None:
            incomplete_plan_steps = [
                step.step_id
                for step in plan.steps
                if step.status != PlanStepStatus.COMPLETED
            ]
            if incomplete_plan_steps:
                return CompletionGateResult(
                    False,
                    task_id,
                    incomplete_steps=tuple(incomplete_plan_steps),
                    message="Execution plan has incomplete steps.",
                )
        steps = self.list_steps(task_id)
        incomplete = [
            step.step_id
            for step in steps
            if step.status not in {TaskStepStatus.DONE, TaskStepStatus.SKIPPED}
        ]
        if incomplete:
            return CompletionGateResult(
                False,
                task_id,
                incomplete_steps=tuple(incomplete),
                message="Task has incomplete steps.",
            )
        required = _normalize_evidence_types(required_evidence or state.required_evidence)
        gate = self._check_required_evidence(task_id, step_id="", required=required)
        if not gate.ok:
            return gate
        state.status = RunStatus.COMPLETED
        state.current_phase = "completed"
        self.save_task(
            state,
            checkpoint_kind="before_final_completion",
            allow_completion=True,
        )
        return CompletionGateResult(True, task_id, message="Task completion verified.")

    def load_step(self, task_id: str, step_id: str) -> StepState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM steps WHERE task_id = ? AND step_id = ?",
                (task_id, step_id),
            ).fetchone()
        if row is None:
            return None
        return _step_from_dict(_loads(row["payload"], label=f"step {task_id}/{step_id}"))

    def list_steps(self, task_id: str) -> list[StepState]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM steps WHERE task_id = ? ORDER BY updated_at, step_id",
                (task_id,),
            ).fetchall()
        return [_step_from_dict(_loads(row["payload"], label=f"step for {task_id}")) for row in rows]

    def _check_required_evidence(
        self,
        task_id: str,
        *,
        step_id: str,
        required: list[EvidenceType],
        not_before: str = "",
    ) -> CompletionGateResult:
        if not required:
            return CompletionGateResult(True, task_id, step_id)
        records = self.list_evidence(task_id, step_id=step_id)
        missing: list[str] = []
        stale: list[str] = []
        failed: list[str] = []
        for evidence_type in required:
            candidates = [
                record for record in records if record.evidence_type == evidence_type
            ]
            if not candidates:
                missing.append(evidence_type.value)
                continue
            fresh = [
                record for record in candidates if not not_before or record.timestamp >= not_before
            ]
            if not fresh:
                stale.append(evidence_type.value)
                continue
            if not any(record.status == EvidenceStatus.PASSED for record in fresh):
                failed.append(evidence_type.value)
        ok = not missing and not stale and not failed
        return CompletionGateResult(
            ok,
            task_id,
            step_id,
            missing_evidence=tuple(missing),
            stale_evidence=tuple(stale),
            failed_evidence=tuple(failed),
            message="Evidence verified." if ok else "Required evidence is missing, stale, or failed.",
        )

    def record_plan_created(self, task_id: str, plan_id: str = "") -> TaskState:
        state = self.require_task(task_id)
        state.status = RunStatus.RUNNING
        state.current_phase = "planned"
        state.plan_id = plan_id or state.plan_id
        return self.save_task(state, checkpoint_kind="plan_created")

    def record_approval_requested(self, task_id: str, request: dict[str, Any]) -> TaskState:
        state = self.require_task(task_id)
        state.status = RunStatus.WAITING_FOR_APPROVAL
        state.current_phase = "approval"
        state.pending_approvals.append(request)
        return self.save_task(state, checkpoint_kind="approval_requested")

    def record_approval_resolved(self, task_id: str, approved: bool) -> TaskState:
        state = self.require_task(task_id)
        state.status = RunStatus.RUNNING
        state.current_phase = "running"
        if state.pending_approvals:
            state.pending_approvals[-1] = {
                **state.pending_approvals[-1],
                "approved": approved,
                "resolved_at": _now(),
            }
        return self.save_task(state, checkpoint_kind="approval_resolved")

    def record_successful_step(
        self,
        task_id: str,
        *,
        step_id: str,
        tool_call: dict[str, Any],
        tool_result: dict[str, Any],
        changed_files: list[str] | None = None,
        required_evidence: list[EvidenceType | str] | None = None,
    ) -> TaskState:
        state = self.require_task(task_id)
        state.status = RunStatus.RUNNING
        state.current_phase = "running"
        state.current_step_id = step_id
        state.action_count += 1
        state.last_tool_call = dict(tool_call)
        state.last_tool_result = dict(tool_result)
        for path in changed_files or []:
            if path and path not in state.changed_files:
                state.changed_files.append(path)
        evidence = str(tool_result.get("message") or tool_result.get("tool") or "")
        if evidence and evidence not in state.collected_evidence:
            state.collected_evidence.append(evidence)
        step = StepState(
            step_id=step_id,
            task_id=state.task_id,
            run_id=state.run_id,
            status=TaskStepStatus.RUNNING,
            phase=state.current_phase,
            description=str(tool_call.get("name") or ""),
            tool_name=str(tool_call.get("name") or ""),
            tool_call=dict(tool_call),
            tool_result=dict(tool_result),
            required_evidence=[item.value if hasattr(item, "value") else str(item) for item in (required_evidence or [])],
        )
        self.record_step(step)
        for evidence in _evidence_from_tool_result(
            state,
            step_id,
            tool_call,
            tool_result,
            list(changed_files or []),
        ):
            self.record_evidence(evidence)
        self.save_task(state, checkpoint_kind="step_succeeded")
        self.request_step_complete(task_id, step_id, required_evidence=required_evidence or [])
        return self.require_task(task_id)

    def record_replan(self, task_id: str, plan_id: str = "") -> TaskState:
        state = self.require_task(task_id)
        state.replan_count += 1
        state.plan_id = plan_id or state.plan_id
        state.current_phase = "replanned"
        return self.save_task(state, checkpoint_kind="replanned")

    def record_repair_attempt(
        self,
        task_id: str,
        *,
        target_files: list[str],
        last_error: str = "",
    ) -> TaskState:
        state = self.require_task(task_id)
        state.repair_count += 1
        repair = RepairState(
            repair_id=_new_id("repair"),
            task_id=state.task_id,
            run_id=state.run_id,
            status=RunStatus.RUNNING,
            attempt=state.repair_count,
            target_files=list(target_files),
            last_error=last_error,
        )
        self.record_repair(repair)
        return self.save_task(state, checkpoint_kind="repair_attempted")

    def record_failure(self, failure: FailureRecord) -> FailureRecord:
        failure.failure_type = normalize_failure_type(failure.failure_type)
        if not failure.error_signature:
            failure.error_signature = make_error_signature(
                failure.failure_type,
                action=failure.action,
                evidence=failure.evidence,
            )
        existing = self.load_failure(failure.task_id, failure.error_signature)
        if existing is not None:
            failure.first_seen = existing.first_seen
            failure.retry_count = existing.retry_count + 1
        payload = failure.to_dict()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO failures (
                    task_id, error_signature, failure_type, step_id, action,
                    retry_count, first_seen, last_seen, payload
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, error_signature) DO UPDATE SET
                    failure_type=excluded.failure_type,
                    step_id=excluded.step_id,
                    action=excluded.action,
                    retry_count=excluded.retry_count,
                    last_seen=excluded.last_seen,
                    payload=excluded.payload
                """,
                (
                    failure.task_id,
                    failure.error_signature,
                    failure.failure_type.value,
                    failure.step_id,
                    failure.action,
                    failure.retry_count,
                    failure.first_seen,
                    failure.last_seen,
                    _dumps(payload),
                ),
            )
        return failure

    def create_failure(
        self,
        task_id: str,
        failure_type: FailureType | str,
        *,
        step_id: str = "",
        action: str = "",
        evidence: list[str] | None = None,
        detail: Any = None,
    ) -> FailureRecord:
        normalized = normalize_failure_type(failure_type)
        return self.record_failure(
            FailureRecord(
                failure_type=normalized,
                task_id=task_id,
                step_id=step_id,
                action=action,
                error_signature=make_error_signature(
                    normalized,
                    action=action,
                    evidence=evidence or [],
                    detail=detail,
                ),
                evidence=list(evidence or []),
            )
        )

    def load_failure(self, task_id: str, error_signature: str) -> FailureRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM failures WHERE task_id = ? AND error_signature = ?",
                (task_id, error_signature),
            ).fetchone()
        if row is None:
            return None
        return _failure_from_dict(_loads(row["payload"], label=f"failure {error_signature}"))

    def list_failures(self, task_id: str, failure_type: FailureType | str | None = None) -> list[FailureRecord]:
        params: list[Any] = [task_id]
        where = "task_id = ?"
        if failure_type is not None:
            where += " AND failure_type = ?"
            params.append(normalize_failure_type(failure_type).value)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT payload FROM failures WHERE {where} ORDER BY last_seen DESC",
                params,
            ).fetchall()
        return [_failure_from_dict(_loads(row["payload"], label=f"failure for {task_id}")) for row in rows]

    def recover_latest_checkpoint(self, task_id: str) -> TaskState | None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_payload FROM checkpoints
                WHERE task_id = ?
                ORDER BY id DESC
                """,
                (task_id,),
            ).fetchall()
        for row in rows:
            try:
                return _task_from_dict(_loads(row["task_payload"], label=f"checkpoint {task_id}"))
            except CorruptRuntimeState:
                continue
        return None

    def corrupt_task_payload_for_test(self, task_id: str, raw_payload: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE tasks SET payload = ? WHERE task_id = ?", (raw_payload, task_id))


def _evidence_from_tool_result(
    state: TaskState,
    step_id: str,
    tool_call: dict[str, Any],
    tool_result: dict[str, Any],
    changed_files: list[str],
) -> list[EvidenceRecord]:
    name = str(tool_call.get("name") or tool_result.get("tool") or "")
    result_data = tool_result.get("data") if isinstance(tool_result.get("data"), dict) else {}
    ok = bool(tool_result.get("ok", False))
    records: list[EvidenceRecord] = []
    if changed_files and name in {"write_file", "edit_file", "append_file", "file.patch", "run_command", "test.run"}:
        records.append(
            EvidenceRecord(
                evidence_id=_new_id("evidence"),
                task_id=state.task_id,
                step_id=step_id,
                evidence_type=EvidenceType.FILE_CHANGED,
                source_tool=name,
                status=EvidenceStatus.PASSED if ok else EvidenceStatus.FAILED,
                details={"message": str(tool_result.get("message", ""))},
                related_files=list(changed_files),
                checkpoint_id=state.last_checkpoint,
            )
        )
    if name not in {"run_command", "test.run"}:
        return records
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    command = str(arguments.get("command") or result_data.get("command") or "")
    exit_code = result_data.get("exit_code")
    try:
        exit_code_int = int(exit_code)
    except (TypeError, ValueError):
        exit_code_int = 0 if ok else 1
    evidence_type = _command_evidence_type(command)
    if evidence_type is not None:
        records.append(
            EvidenceRecord(
                evidence_id=_new_id("evidence"),
                task_id=state.task_id,
                step_id=step_id,
                evidence_type=evidence_type,
                source_tool=name,
                status=EvidenceStatus.PASSED if exit_code_int == 0 and ok else EvidenceStatus.FAILED,
                details={"message": str(tool_result.get("message", ""))},
                related_command=command,
                exit_code=exit_code_int,
                checkpoint_id=state.last_checkpoint,
            )
        )
    return records


def _command_evidence_type(command: str) -> EvidenceType | None:
    lowered = command.lower()
    if "pytest" in lowered or " test" in f" {lowered}" or "npm test" in lowered:
        return EvidenceType.TEST_PASSED
    if "ruff" in lowered or "eslint" in lowered or " lint" in f" {lowered}":
        return EvidenceType.LINT_PASSED
    if "mypy" in lowered or "tsc" in lowered or "typecheck" in lowered:
        return EvidenceType.TYPECHECK_PASSED
    if " build" in f" {lowered}" or "npm run build" in lowered:
        return EvidenceType.BUILD_PASSED
    if "migrate" in lowered or "migration" in lowered:
        return EvidenceType.MIGRATION_PASSED
    if "health" in lowered or "curl" in lowered:
        return EvidenceType.SERVICE_HEALTHY
    return None


def _normalize_evidence_types(values: list[EvidenceType | str]) -> list[EvidenceType]:
    normalized: list[EvidenceType] = []
    for value in values:
        evidence_type = value if isinstance(value, EvidenceType) else EvidenceType(str(value))
        if evidence_type not in normalized:
            normalized.append(evidence_type)
    return normalized


def validate_execution_plan(
    plan: ExecutionPlan,
    *,
    valid_tool_names: set[str] | None = None,
) -> PlanValidationResult:
    errors: list[str] = []
    if not plan.plan_id:
        errors.append("plan_id is required")
    if not plan.task_id:
        errors.append("task_id is required")
    if not plan.steps:
        errors.append("at least one step is required")
    step_ids = [step.step_id for step in plan.steps]
    duplicates = sorted({step_id for step_id in step_ids if step_ids.count(step_id) > 1})
    if duplicates:
        errors.append("duplicate step_id: " + ", ".join(duplicates))
    known_ids = set(step_ids)
    for step in plan.steps:
        if not step.step_id:
            errors.append("step_id is required")
        if not step.title.strip():
            errors.append(f"{step.step_id}: title is required")
        if not step.goal.strip():
            errors.append(f"{step.step_id}: goal is required")
        if not step.acceptance_criteria:
            errors.append(f"{step.step_id}: acceptance_criteria is required")
        if _step_is_mutating(step) and not step.required_evidence:
            errors.append(f"{step.step_id}: mutating step requires evidence")
        for dep in step.dependencies:
            if dep not in known_ids:
                errors.append(f"{step.step_id}: unknown dependency {dep}")
        try:
            RiskLevel(step.risk_level)
        except ValueError:
            errors.append(f"{step.step_id}: invalid risk level {step.risk_level}")
        for evidence in step.required_evidence:
            try:
                EvidenceType(str(evidence))
            except ValueError:
                errors.append(f"{step.step_id}: invalid evidence type {evidence}")
        if valid_tool_names is not None:
            for tool in step.allowed_tools:
                if tool not in valid_tool_names:
                    errors.append(f"{step.step_id}: unknown tool {tool}")
    cycle = _first_dependency_cycle(plan.steps)
    if cycle:
        errors.append("cyclic dependency: " + " -> ".join(cycle))
    return PlanValidationResult(not errors, tuple(errors))


def _step_is_mutating(step: PlanStep) -> bool:
    mutating_tools = {
        "write_file",
        "edit_file",
        "append_file",
        "file.patch",
        "delete_file",
        "run_command",
        "git.checkpoint",
    }
    return step.approval_required or any(tool in mutating_tools for tool in step.allowed_tools)


def _first_dependency_cycle(steps: list[PlanStep]) -> list[str]:
    graph = {step.step_id: list(step.dependencies) for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        if node in visiting:
            start = stack.index(node) if node in stack else 0
            return stack[start:] + [node]
        if node in visited:
            return []
        visiting.add(node)
        stack.append(node)
        for dep in graph.get(node, []):
            cycle = visit(dep)
            if cycle:
                return cycle
        stack.pop()
        visiting.remove(node)
        visited.add(node)
        return []

    for node in graph:
        cycle = visit(node)
        if cycle:
            return cycle
    return []


def _validate_plan_step_transition(previous: PlanStepStatus, next_status: PlanStepStatus) -> None:
    if previous == next_status:
        return
    allowed = {
        PlanStepStatus.PENDING: {
            PlanStepStatus.ACTIVE,
            PlanStepStatus.BLOCKED,
            PlanStepStatus.CANCELLED,
        },
        PlanStepStatus.ACTIVE: {
            PlanStepStatus.BLOCKED,
            PlanStepStatus.VERIFYING,
            PlanStepStatus.COMPLETED,
            PlanStepStatus.FAILED,
            PlanStepStatus.CANCELLED,
        },
        PlanStepStatus.VERIFYING: {
            PlanStepStatus.ACTIVE,
            PlanStepStatus.COMPLETED,
            PlanStepStatus.FAILED,
            PlanStepStatus.BLOCKED,
            PlanStepStatus.CANCELLED,
        },
        PlanStepStatus.BLOCKED: {PlanStepStatus.ACTIVE, PlanStepStatus.CANCELLED},
        PlanStepStatus.FAILED: set(),
        PlanStepStatus.COMPLETED: set(),
        PlanStepStatus.CANCELLED: set(),
    }
    if next_status not in allowed.get(previous, set()):
        raise InvalidStateTransition(
            f"Invalid plan step transition: {previous.value} -> {next_status.value}"
        )


def _evidence_signature(evidence: EvidenceRecord) -> str:
    return _dumps(
        {
            "task_id": evidence.task_id,
            "step_id": evidence.step_id,
            "evidence_type": evidence.evidence_type.value,
            "source_tool": evidence.source_tool,
            "status": evidence.status.value,
            "related_files": sorted(evidence.related_files),
            "related_command": evidence.related_command,
            "exit_code": evidence.exit_code,
            "checkpoint_id": evidence.checkpoint_id,
            "details": evidence.details,
        }
    )


def _validate_transition(previous: RunStatus, next_status: RunStatus) -> None:
    if previous == next_status:
        return
    allowed = {
        RunStatus.CREATED: {
            RunStatus.RUNNING,
            RunStatus.WAITING_FOR_APPROVAL,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.FAILED,
        },
        RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED},
        RunStatus.RUNNING: {
            RunStatus.WAITING_FOR_APPROVAL,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.FAILED,
            RunStatus.COMPLETED,
        },
        RunStatus.WAITING_FOR_APPROVAL: {
            RunStatus.RUNNING,
            RunStatus.CANCELLING,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
            RunStatus.FAILED,
        },
        RunStatus.CANCELLING: {RunStatus.CANCELLED, RunStatus.FAILED},
        RunStatus.CANCELLED: set(),
        RunStatus.TIMED_OUT: set(),
        RunStatus.FAILED: set(),
        RunStatus.COMPLETED: set(),
    }
    if next_status not in allowed.get(previous, set()):
        raise InvalidStateTransition(
            f"Invalid task transition: {previous.value} -> {next_status.value}"
        )


def _state_to_dict(state: Any) -> dict[str, Any]:
    return _normalize_for_json(asdict(state))


def _normalize_for_json(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {str(key): _normalize_for_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_for_json(item) for item in value]
    return value


def _run_from_dict(data: dict[str, Any]) -> RunState:
    data = dict(data)
    data["status"] = RunStatus(data.get("status", RunStatus.CREATED))
    data.setdefault("task_ids", [])
    data.setdefault("current_task_id", "")
    data.setdefault("started_at", _now())
    data.setdefault("updated_at", _now())
    data.setdefault("deadline_at", None)
    data.setdefault("last_checkpoint", "")
    return RunState(**data)


def _step_from_dict(data: dict[str, Any]) -> StepState:
    data = dict(data)
    data["status"] = TaskStepStatus(data.get("status", TaskStepStatus.PENDING))
    data.setdefault("phase", "default")
    data.setdefault("description", "")
    data.setdefault("tool_name", "")
    data.setdefault("tool_call", {})
    data.setdefault("tool_result", {})
    data.setdefault("required_evidence", [])
    data.setdefault("started_at", _now())
    data.setdefault("updated_at", _now())
    data.setdefault("completed_at", "")
    return StepState(**data)


def _evidence_from_dict(data: dict[str, Any]) -> EvidenceRecord:
    data = dict(data)
    data["evidence_type"] = EvidenceType(data.get("evidence_type"))
    data["status"] = EvidenceStatus(data.get("status", EvidenceStatus.PASSED))
    data.setdefault("timestamp", _now())
    data.setdefault("details", {})
    data.setdefault("related_files", [])
    data.setdefault("related_command", "")
    data.setdefault("exit_code", None)
    data.setdefault("checkpoint_id", "")
    return EvidenceRecord(**data)


def _failure_from_dict(data: dict[str, Any]) -> FailureRecord:
    data = dict(data)
    data["failure_type"] = normalize_failure_type(data.get("failure_type", FailureType.UNKNOWN_FAILURE))
    data.setdefault("step_id", "")
    data.setdefault("action", "")
    data.setdefault("error_signature", "")
    data.setdefault("evidence", [])
    data.setdefault("retry_count", 0)
    data.setdefault("first_seen", _now())
    data.setdefault("last_seen", _now())
    return FailureRecord(**data)


def _plan_step_from_dict(data: dict[str, Any]) -> PlanStep:
    data = dict(data)
    data["risk_level"] = RiskLevel(data.get("risk_level", RiskLevel.LOW))
    data["status"] = PlanStepStatus(data.get("status", PlanStepStatus.PENDING))
    for key in (
        "inputs",
        "expected_outputs",
        "constraints",
        "allowed_tools",
        "acceptance_criteria",
        "required_evidence",
        "dependencies",
    ):
        value = data.get(key)
        data[key] = value if isinstance(value, list) else []
    data.setdefault("approval_required", False)
    return PlanStep(**data)


def _execution_plan_from_dict(data: dict[str, Any]) -> ExecutionPlan:
    data = dict(data)
    raw_steps = data.get("steps", [])
    data["steps"] = [
        _plan_step_from_dict(step) for step in raw_steps if isinstance(step, dict)
    ]
    data.setdefault("title", "")
    data.setdefault("summary", "")
    data.setdefault("created_at", _now())
    data.setdefault("updated_at", _now())
    return ExecutionPlan(**data)


def _task_from_dict(data: dict[str, Any]) -> TaskState:
    data = dict(data)
    data["status"] = RunStatus(data.get("status", RunStatus.CREATED))
    for key in (
        "changed_files",
        "required_evidence",
        "collected_evidence",
        "pending_approvals",
    ):
        value = data.get(key)
        data[key] = value if isinstance(value, list) else []
    for key in ("last_tool_call", "last_tool_result"):
        value = data.get(key)
        data[key] = value if isinstance(value, dict) else {}
    defaults = {
        "project_id": "",
        "current_phase": "initialized",
        "current_step_id": "",
        "plan_id": "",
        "action_count": 0,
        "repair_count": 0,
        "replan_count": 0,
        "last_checkpoint": "",
        "created_at": _now(),
        "updated_at": _now(),
    }
    for key, value in defaults.items():
        data.setdefault(key, value)
    return TaskState(**data)
