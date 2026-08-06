"""Typed state records. These are the authoritative facts.

Everything else in the runtime -- artifacts, memory, model output, compiled
frames -- is derived from or advisory to what is stored here. If a fact is not
in one of these records, the runtime does not know it.

Every record is frozen. State changes produce a new record via
``model_copy(update=...)`` and an explicit store write, so there is no way to
mutate authoritative state without persisting it. v1's per-run guard state lived
in loop-local attributes that vanished on crash; that is exactly what this
prevents.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from shamsu.interfaces.enums import (
    AgentState,
    ApprovalDecision,
    EvidenceKind,
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


def new_id() -> str:
    """A fresh opaque identifier."""
    return uuid.uuid4().hex


def utcnow() -> datetime:
    """Timezone-aware current time.

    Always aware, never naive: naive timestamps compare wrongly across a
    resume, and resume correctness is the whole point of persisting state.
    """
    return datetime.now(UTC)


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class ProjectRecord(_Record):
    """What the agent knows about a repository between runs (plan section 12.1)."""

    project_id: ProjectId
    root: str = Field(description="Absolute path to the repository root.")
    name: str

    languages: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    package_managers: tuple[str, ...] = ()
    database_types: tuple[str, ...] = ()
    test_commands: tuple[str, ...] = ()
    active_branch: str | None = None

    index_version: int = 0
    artifact_version: int = 0

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


class RunRecord(_Record):
    """One invocation of the agent, observable and cancellable throughout."""

    run_id: RunId
    project_id: ProjectId
    task_id: TaskId

    status: RunStatus = RunStatus.PENDING
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None

    wall_clock_limit_seconds: float = Field(
        default=1800.0,
        gt=0,
        description="Hard ceiling. Exceeding it is TIMED_OUT, never a silent continuation.",
    )
    cancel_reason: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            RunStatus.CANCELLED,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.TIMED_OUT,
        )


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


class TaskRecord(_Record):
    """The unit of work, and the counters that bound it (plan section 12.2).

    The counters are here rather than in loop-local variables on purpose. In v1
    they were mutable attributes on the loop object, which meant they could not
    be inspected during a run, could not survive a resume, and could not be
    asserted on in a test without reaching into the loop.
    """

    task_id: TaskId
    project_id: ProjectId
    request: str = Field(description="The user's request, verbatim.")

    kind: TaskKind | None = None
    state: AgentState = AgentState.RECEIVE_TASK
    phase: Phase = Phase.INSPECT

    plan_id: PlanId | None = None
    current_step_id: StepId | None = None

    action_count: int = Field(default=0, ge=0)
    repair_count: int = Field(default=0, ge=0)
    replan_count: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)

    final_result: str | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


class PlanRecord(_Record):
    """A versioned plan. Re-planning creates a new version, never an edit.

    Keeping superseded plans matters for debugging: "what did it think it was
    doing before it re-planned?" is unanswerable if the plan is overwritten.
    """

    plan_id: PlanId
    task_id: TaskId
    version: int = Field(ge=1)
    summary: str = Field(description="Compact form; this is what enters the prompt.")
    superseded_by: PlanId | None = None
    created_at: datetime = Field(default_factory=utcnow)


class PlanStepRecord(_Record):
    """One step, matching the planning contract in plan section 21.

    ``required_evidence`` is the load-bearing field: it is set *before*
    execution, so the completion gate cannot be argued into accepting whatever
    evidence happened to be produced.
    """

    step_id: StepId
    plan_id: PlanId
    ordinal: int = Field(ge=0)
    title: str

    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    required_evidence: tuple[EvidenceKind, ...] = ()

    risk: Risk = Risk.LOW
    approval_required: bool = False

    outcome: StepOutcome | None = None
    attempts: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Tool events and evidence
# ---------------------------------------------------------------------------


class ToolEventRecord(_Record):
    """One tool execution, recorded whether it succeeded or not.

    Failures are recorded with equal fidelity to successes -- a ledger that
    only remembers what worked cannot explain what went wrong.
    """

    event_id: ToolEventId
    run_id: RunId
    task_id: TaskId
    step_id: StepId | None

    tool: str
    phase: Phase
    arguments_json: str

    ok: bool
    output: str = ""
    error: str | None = None
    truncated: bool = False
    original_bytes: int | None = None
    duration_seconds: float = 0.0

    created_at: datetime = Field(default_factory=utcnow)


class EvidenceRecord(_Record):
    """Proof that something actually happened.

    ``source_event_id`` is mandatory and is what makes evidence non-forgeable:
    every piece traces to a tool execution the runtime observed. There is no
    constructor path that produces evidence from a model assertion.
    """

    evidence_id: EvidenceId
    task_id: TaskId
    step_id: StepId | None
    kind: EvidenceKind
    source_event_id: ToolEventId
    detail: str = ""
    recorded_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Approvals, checkpoints, failures
# ---------------------------------------------------------------------------


class ApprovalRecord(_Record):
    """A request for human authorisation.

    ``TIMED_OUT`` is a distinct decision from ``APPROVED``. Silence is never
    consent.
    """

    approval_id: ApprovalId
    task_id: TaskId
    step_id: StepId | None
    reason: str
    risk: Risk
    decision: ApprovalDecision = ApprovalDecision.PENDING
    requested_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None

    @property
    def grants_permission(self) -> bool:
        return self.decision is ApprovalDecision.APPROVED


class CheckpointRecord(_Record):
    """A point the run can be resumed from or rolled back to.

    Initial resume support only needs verified step boundaries (plan section
    12.3), which is why ``step_id`` is the anchor rather than an arbitrary
    instruction pointer.
    """

    checkpoint_id: CheckpointId
    task_id: TaskId
    step_id: StepId | None
    label: str
    git_ref: str | None = Field(
        default=None, description="Commit or stash ref, when the checkpoint is git-backed."
    )
    state_snapshot_json: str = Field(description="Serialised TaskRecord at checkpoint time.")
    created_at: datetime = Field(default_factory=utcnow)


class FailureRecord(_Record):
    """A classified failure and its signature.

    ``signature`` is what stops a repair loop from grinding: two consecutive
    identical signatures means the attempts are not making progress, so the
    runtime stops rather than spending its remaining budget.
    """

    failure_id: FailureId
    task_id: TaskId
    step_id: StepId | None
    kind: FailureKind
    signature: str = Field(description="Stable hash of the error's identity, not its text.")
    expected: str = ""
    actual: str = ""
    detail: str = ""
    attempt: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utcnow)


__all__ = [
    "ApprovalRecord",
    "CheckpointRecord",
    "EvidenceRecord",
    "FailureRecord",
    "PlanRecord",
    "PlanStepRecord",
    "ProjectRecord",
    "RunRecord",
    "TaskRecord",
    "ToolEventRecord",
    "new_id",
    "utcnow",
]
