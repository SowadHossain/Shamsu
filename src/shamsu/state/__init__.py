"""Typed state records and the authoritative SQLite store.

Every fact the runtime relies on to make a transition lives here. If it is not
in SQLite, it is not authoritative -- artifacts, memory, and model output are
all derived or advisory.

Two guarantees this package provides that the rest of the runtime builds on:

* **Transitions are validated on write.** `StateStore.advance_task` consults
  `transitions.TRANSITIONS` and raises on an illegal move. A task cannot be put
  into an unreachable state through this API.
* **Evidence is non-forgeable.** Every `EvidenceRecord` carries the id of the
  tool event that produced it, and the schema enforces that key. There is no
  path from "the model said it passed" to a row in the evidence table.

Milestone 2. See plan section 12.
"""

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
    new_id,
    utcnow,
)
from shamsu.state.schema import SCHEMA_VERSION, migrate
from shamsu.state.store import StateStore
from shamsu.state.transitions import (
    TERMINAL,
    TRANSITIONS,
    InvalidTransition,
    allowed_from,
    assert_transition,
    can_transition,
    is_terminal,
    next_after_classification,
    next_after_verification,
    reachable_from,
)

__all__ = [
    "SCHEMA_VERSION",
    "TERMINAL",
    "TRANSITIONS",
    "ApprovalRecord",
    "CheckpointRecord",
    "EvidenceRecord",
    "FailureRecord",
    "InvalidTransition",
    "PlanRecord",
    "PlanStepRecord",
    "ProjectRecord",
    "RunRecord",
    "StateStore",
    "TaskRecord",
    "ToolEventRecord",
    "allowed_from",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "migrate",
    "new_id",
    "next_after_classification",
    "next_after_verification",
    "reachable_from",
    "utcnow",
]
