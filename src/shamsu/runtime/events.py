"""Run events: the observable record of what a run is doing.

"Every live run must be observable and cancellable" is one requirement, not
two. A run whose progress cannot be seen cannot be sensibly cancelled either --
the user has no basis on which to decide.

Events are append-only and cheap. They are not the ledger of *what happened to
the repository* (that is `tool_events` in SQLite); they are the ledger of *what
the run is doing right now*, for status output, progress display, and
post-mortem timeline reconstruction.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from shamsu.interfaces.enums import AgentState, RunStatus
from shamsu.interfaces.ids import RunId
from shamsu.state.records import utcnow


class EventKind(StrEnum):
    """What happened.

    Deliberately coarse. These describe run lifecycle, not every internal step;
    a granular firehose is what makes a log unreadable at the moment you most
    need it.
    """

    REGISTERED = "registered"
    STARTED = "started"
    STATE_CHANGED = "state_changed"
    PHASE_CHANGED = "phase_changed"
    TOOL_INVOKED = "tool_invoked"
    MODEL_CALLED = "model_called"
    EVIDENCE_RECORDED = "evidence_recorded"
    CHECKPOINT_CREATED = "checkpoint_created"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    PAUSED = "paused"
    RESUMED = "resumed"
    FEEDBACK_RECEIVED = "feedback_received"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    WALL_CLOCK_EXCEEDED = "wall_clock_exceeded"
    LIMIT_REACHED = "limit_reached"
    FAILED = "failed"
    COMPLETED = "completed"


class RunEvent(BaseModel):
    """One observation about a run."""

    model_config = ConfigDict(frozen=True)

    run_id: RunId
    kind: EventKind
    at: datetime = Field(default_factory=utcnow)
    detail: str = ""

    state: AgentState | None = None
    status: RunStatus | None = None

    def render(self) -> str:
        """A single log line."""
        stamp = self.at.strftime("%H:%M:%S")
        suffix = f" {self.detail}" if self.detail else ""
        return f"[{stamp}] {self.kind.value}{suffix}"


__all__ = ["EventKind", "RunEvent"]
