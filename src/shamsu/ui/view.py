"""What the interface knows about a run.

A plain data structure updated by feeding it `RunEvent`s, and nothing else. It
holds no terminal, no escape codes, and no I/O — which is what makes the whole
interface testable without a TTY.

The split matters more than it looks. v1's CLI was 18,729 lines with 17,411 in
`repl.py`, because display, input handling, session management, and agent
control all lived in one object. Nothing in it could be tested without driving
a terminal, so in practice none of it was.

Here: `RunView` accumulates state, `render` turns state into lines, and
`terminal` is the only module that touches a file descriptor. Two of those
three are pure functions of their input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from shamsu.interfaces.enums import AgentState, EvidenceKind, Phase, RunStatus
from shamsu.runtime.events import EventKind, RunEvent

#: How many activity lines are kept. The pane scrolls, but an unbounded list on
#: a long run is a memory leak with a nice interface.
MAX_ACTIVITY = 500

#: Which phase each state presents as. The state machine has nineteen states;
#: a user watching a run cares about roughly five things.
_PHASE_OF: dict[AgentState, Phase] = {
    AgentState.RECEIVE_TASK: Phase.INSPECT,
    AgentState.LOAD_PROJECT_STATE: Phase.INSPECT,
    AgentState.INSPECT_PROJECT: Phase.INSPECT,
    AgentState.CLASSIFY_TASK: Phase.PLAN,
    AgentState.CREATE_PLAN: Phase.PLAN,
    AgentState.VALIDATE_PLAN: Phase.PLAN,
    AgentState.APPROVAL_CHECK: Phase.PLAN,
    AgentState.WAIT_APPROVAL: Phase.PLAN,
    AgentState.EXECUTE_CURRENT_STEP: Phase.AUTHOR,
    AgentState.VERIFY_CURRENT_STEP: Phase.VERIFY,
    AgentState.CREATE_CHECKPOINT: Phase.VERIFY,
    AgentState.REPAIR: Phase.REPAIR,
    AgentState.REPLAN: Phase.PLAN,
    AgentState.CHECK_REMAINING_STEPS: Phase.VERIFY,
    AgentState.FINAL_VERIFICATION: Phase.VERIFY,
    AgentState.COMPLETION_GATE: Phase.COMPLETE,
    AgentState.FINAL_REPORT: Phase.COMPLETE,
    AgentState.STOPPED: Phase.COMPLETE,
    AgentState.BLOCKED: Phase.COMPLETE,
}


class Level:
    """Severity of an activity line. Decides the glyph and the colour."""

    STEP = "step"
    OK = "ok"
    FAIL = "fail"
    NOTE = "note"
    STOP = "stop"


@dataclass(frozen=True)
class Activity:
    """One line in the activity pane."""

    level: str
    label: str
    detail: str = ""
    at: datetime | None = None

    def text(self) -> str:
        return f"{self.label}  {self.detail}".rstrip() if self.detail else self.label


@dataclass
class RunView:
    """Everything the interface shows about one run.

    Mutated by `apply`, never by the renderer. A view is a fact about what has
    happened; rendering is a question about how to fit it in a window, and
    keeping those apart is why the renderer can be tested at 40 columns and
    200 without a terminal existing.
    """

    request: str = ""
    workspace: str = ""

    state: AgentState = AgentState.RECEIVE_TASK
    status: RunStatus = RunStatus.PENDING
    activity: list[Activity] = field(default_factory=list)
    evidence: set[EvidenceKind] = field(default_factory=set)

    step_index: int = 0
    step_total: int = 0
    step_title: str = ""

    started_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: str = ""

    #: Set while the run is waiting on the model. The footer shows it, because
    #: a local 7B can take thirty seconds and a frozen-looking interface is how
    #: users conclude a tool has hung.
    waiting_on: str = ""

    @property
    def phase(self) -> Phase:
        return _PHASE_OF.get(self.state, Phase.INSPECT)

    @property
    def running(self) -> bool:
        return self.finished_at is None and self.status not in _FINISHED

    @property
    def cancelling(self) -> bool:
        return self.status is RunStatus.CANCELLING

    def elapsed(self, now: datetime) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at or now
        return max(0.0, (end - self.started_at).total_seconds())

    # -- updating ----------------------------------------------------------

    def note(self, level: str, label: str, detail: str = "", at: datetime | None = None) -> None:
        """Append an activity line, trimming the oldest when the cap is hit."""
        self.activity.append(Activity(level=level, label=label, detail=detail, at=at))
        if len(self.activity) > MAX_ACTIVITY:
            del self.activity[: len(self.activity) - MAX_ACTIVITY]

    def apply(self, event: RunEvent) -> None:
        """Fold one run event into the view.

        Unknown event kinds are ignored rather than rendered as noise. A new
        `EventKind` should not make the interface start printing raw enum names
        at a user; it should be added here deliberately or not shown.
        """
        if event.status is not None:
            self.status = event.status

        if event.kind is EventKind.STARTED:
            self.started_at = event.at
            self.note(Level.NOTE, "started", self.request, at=event.at)

        elif event.kind is EventKind.STATE_CHANGED and event.state is not None:
            previous = self.state
            self.state = event.state
            if self.phase is not _PHASE_OF.get(previous, Phase.INSPECT):
                self.note(Level.STEP, self.phase.value, at=event.at)

        elif event.kind is EventKind.TOOL_INVOKED:
            level = Level.OK if not event.detail.startswith("!") else Level.FAIL
            self.note(level, "tool", event.detail.lstrip("!"), at=event.at)

        elif event.kind is EventKind.EVIDENCE_RECORDED:
            self.note(Level.OK, "evidence", event.detail, at=event.at)

        elif event.kind is EventKind.CHECKPOINT_CREATED:
            self.note(Level.OK, "checkpoint", event.detail, at=event.at)

        elif event.kind is EventKind.CANCEL_REQUESTED:
            self.note(Level.STOP, "cancelling", event.detail, at=event.at)

        elif event.kind in (EventKind.CANCELLED, EventKind.FAILED, EventKind.COMPLETED):
            self.finished_at = event.at
            self.outcome = event.detail
            level = Level.OK if event.kind is EventKind.COMPLETED else Level.STOP
            self.note(level, event.kind.value, event.detail, at=event.at)

        elif event.kind is EventKind.LIMIT_REACHED:
            self.note(Level.FAIL, "limit", event.detail, at=event.at)

        elif event.kind is EventKind.PAUSED:
            self.note(Level.NOTE, "paused", at=event.at)

        elif event.kind is EventKind.RESUMED:
            self.note(Level.NOTE, "resumed", at=event.at)

    def observe_step(self, index: int, total: int, title: str) -> None:
        self.step_index, self.step_total, self.step_title = index, total, title

    def observe_evidence(self, kinds: Sequence[EvidenceKind]) -> None:
        self.evidence.update(kinds)


_FINISHED = frozenset(
    {
        RunStatus.CANCELLED,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.TIMED_OUT,
    }
)


__all__ = ["MAX_ACTIVITY", "Activity", "Level", "RunView"]
