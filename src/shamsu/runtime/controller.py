"""The run controller.

This is the component v1 had and never connected. `runtime/run_control.py`
implemented registration, cancellation, feedback injection, and in-flight model
cancellation; the live loop imported none of it, so `cancel_run` could be
called and nothing would happen.

The structural difference here is that the controller owns the token, and the
token is a required parameter on every blocking call in the system. A component
cannot forget to observe cancellation, because it cannot block without being
handed the thing that reports it.

Responsibilities: registration, status, cancellation, pause/resume, feedback
injection, wall-clock enforcement, and the event log.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from shamsu.interfaces.cancellation import Cancelled
from shamsu.interfaces.enums import AgentState, RunStatus
from shamsu.interfaces.ids import ProjectId, RunId, TaskId
from shamsu.runtime.events import EventKind, RunEvent
from shamsu.runtime.limits import DEFAULT_LIMITS, ExecutionLimits
from shamsu.runtime.tokens import RunToken
from shamsu.state.records import RunRecord, new_id, utcnow
from shamsu.state.store import StateStore


class UnknownRun(Exception):
    """No run with that id is registered."""


class RunAlreadyFinished(Exception):
    """The run has already reached a terminal status."""


@dataclass
class _LiveRun:
    """In-process state for one registered run.

    Deliberately separate from `RunRecord`: the record is the durable fact,
    this is the live control surface. Conflating them would mean either
    persisting an asyncio primitive or losing the ability to cancel.
    """

    run_id: RunId
    token: RunToken
    limits: ExecutionLimits
    started_monotonic: float
    events: list[RunEvent] = field(default_factory=list)
    # Set means "running". Cleared means "paused" -- inverted so the common
    # path (not paused) needs no await to proceed.
    resume_gate: asyncio.Event | None = None
    paused: bool = False


class RunController:
    """Registers, observes, and controls live runs.

    One controller per process. Cancellation and feedback are safe to call from
    any thread; everything else assumes the owning event loop.
    """

    def __init__(self, store: StateStore, limits: ExecutionLimits | None = None) -> None:
        self._store = store
        self._limits = limits or DEFAULT_LIMITS
        self._live: dict[RunId, _LiveRun] = {}
        self._listeners: list[Callable[[RunEvent], None]] = []
        # Guards `_live` and each run's event list. `StateStore` has its own
        # lock, so persistence is safe independently of this one.
        self._lock = threading.RLock()

    # -- registration ------------------------------------------------------

    def register(
        self,
        project_id: ProjectId,
        task_id: TaskId,
        *,
        run_id: RunId | None = None,
        limits: ExecutionLimits | None = None,
    ) -> RunId:
        """Register a run and return its id.

        The run is persisted immediately, before any work starts, so a crash
        leaves an observable record rather than a run that never existed.
        """
        effective = limits or self._limits
        identifier = run_id or RunId(new_id())

        self._store.create_run(
            RunRecord(
                run_id=identifier,
                project_id=project_id,
                task_id=task_id,
                status=RunStatus.PENDING,
                wall_clock_limit_seconds=effective.wall_clock_seconds,
            )
        )

        with self._lock:
            self._live[identifier] = _LiveRun(
                run_id=identifier,
                token=RunToken(),
                limits=effective,
                started_monotonic=time.monotonic(),
            )
        self._emit(identifier, EventKind.REGISTERED, status=RunStatus.PENDING)
        return identifier

    def start(self, run_id: RunId) -> None:
        """Mark a registered run as running."""
        self._set_status(run_id, RunStatus.RUNNING)
        live = self._require(run_id)
        live.started_monotonic = time.monotonic()
        self._emit(run_id, EventKind.STARTED, status=RunStatus.RUNNING)

    # -- observation -------------------------------------------------------

    def token(self, run_id: RunId) -> RunToken:
        """The run's cancellation token.

        Pass this to every blocking call the run makes. That is the whole
        mechanism -- there is no separate "check for cancellation" step to
        remember.
        """
        return self._require(run_id).token

    def limits(self, run_id: RunId) -> ExecutionLimits:
        return self._require(run_id).limits

    def status(self, run_id: RunId) -> RunStatus:
        record = self._store.get_run(run_id)
        if record is None:
            raise UnknownRun(run_id)
        return record.status

    def events(self, run_id: RunId) -> Sequence[RunEvent]:
        with self._lock:
            return tuple(self._require(run_id).events)

    def active(self) -> Sequence[RunId]:
        """Every run that has not finished."""
        return tuple(run.run_id for run in self._store.active_runs())

    def elapsed_seconds(self, run_id: RunId) -> float:
        return time.monotonic() - self._require(run_id).started_monotonic

    def remaining_seconds(self, run_id: RunId) -> float:
        live = self._require(run_id)
        return max(0.0, live.limits.wall_clock_seconds - self.elapsed_seconds(run_id))

    # -- cancellation ------------------------------------------------------

    def cancel(self, run_id: RunId, reason: str = "cancelled by user") -> None:
        """Request cancellation. Safe from any thread; idempotent.

        Returns as soon as the request is delivered. The run stops at its next
        checkpoint or mid-call, depending on whether it is awaiting the token.
        """
        live = self._require(run_id)
        if live.token.cancelled:
            return

        # The token is woken first and handles the cross-thread hop itself.
        # A paused run does not need its gate touched here: `wait_if_paused`
        # races the gate against the token, so cancelling releases it without
        # this method reaching into an asyncio primitive from another thread.
        live.token.request(reason)
        self._emit(run_id, EventKind.CANCEL_REQUESTED, detail=reason)
        self._set_status(run_id, RunStatus.CANCELLING, cancel_reason=reason)

    def finish_cancelled(self, run_id: RunId) -> None:
        """Record that a cancelled run has actually stopped.

        Separate from `cancel` on purpose: CANCELLING means the request was
        delivered, CANCELLED means the run acknowledged it. Collapsing the two
        would let status claim a stop that has not happened.
        """
        live = self._require(run_id)
        self._set_status(run_id, RunStatus.CANCELLED, cancel_reason=live.token.reason, ended=True)
        self._emit(run_id, EventKind.CANCELLED, status=RunStatus.CANCELLED)

    # -- pause and resume --------------------------------------------------

    def pause(self, run_id: RunId) -> None:
        live = self._require(run_id)
        if live.paused:
            return
        if live.resume_gate is None:
            live.resume_gate = asyncio.Event()
        live.resume_gate.clear()
        live.paused = True
        self._set_status(run_id, RunStatus.PAUSED)
        self._emit(run_id, EventKind.PAUSED, status=RunStatus.PAUSED)

    def resume(self, run_id: RunId) -> None:
        live = self._require(run_id)
        if not live.paused:
            return
        live.paused = False
        if live.resume_gate is not None:
            live.resume_gate.set()
        self._set_status(run_id, RunStatus.RUNNING)
        self._emit(run_id, EventKind.RESUMED, status=RunStatus.RUNNING)

    def is_paused(self, run_id: RunId) -> bool:
        return self._require(run_id).paused

    async def wait_if_paused(self, run_id: RunId) -> None:
        """Block while the run is paused. Awaited at step boundaries.

        Races the resume gate against cancellation, so a paused run cannot sit
        in the gate forever after the user gives up and cancels. Waiting only
        on the gate would make pause a way to defeat cancellation.

        Raises:
            Cancelled: the run was cancelled, before or during the pause.
        """
        live = self._require(run_id)
        live.token.raise_if_cancelled()
        if not live.paused or live.resume_gate is None:
            return

        resumed = asyncio.ensure_future(live.resume_gate.wait())
        cancelled = asyncio.ensure_future(live.token.wait_cancelled())
        try:
            await asyncio.wait({resumed, cancelled}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (resumed, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(resumed, cancelled, return_exceptions=True)

        live.token.raise_if_cancelled()

    # -- feedback ----------------------------------------------------------

    def submit_feedback(self, run_id: RunId, feedback: str) -> None:
        """Inject user feedback into a live run. Safe from any thread."""
        live = self._require(run_id)
        live.token.submit_feedback(feedback)
        self._emit(run_id, EventKind.FEEDBACK_RECEIVED, detail=feedback)

    def take_feedback(self, run_id: RunId) -> list[str]:
        return self._require(run_id).token.take_feedback()

    # -- wall clock --------------------------------------------------------

    def check_wall_clock(self, run_id: RunId) -> None:
        """Enforce the run's time budget.

        Raises:
            Cancelled: the budget is spent. Modelled as a cancellation because
                the run must unwind the same way -- an exhausted run is stopped,
                not failed, and the two must not be confused in a report.
        """
        if self.remaining_seconds(run_id) > 0.0:
            return
        live = self._require(run_id)
        limit = live.limits.wall_clock_seconds
        reason = f"wall-clock limit of {limit:.0f}s exceeded"
        if not live.token.cancelled:
            live.token.request(reason)
        self._emit(run_id, EventKind.WALL_CLOCK_EXCEEDED, detail=reason)
        self._set_status(run_id, RunStatus.TIMED_OUT, cancel_reason=reason, ended=True)
        raise Cancelled(reason)

    async def checkpoint(self, run_id: RunId) -> None:
        """The standard control check between units of work.

        Order matters: cancellation first (a cancelled run should not wait for
        an unpause), then the pause gate, then the wall clock.
        """
        live = self._require(run_id)
        live.token.raise_if_cancelled()
        await self.wait_if_paused(run_id)
        self.check_wall_clock(run_id)

    # -- completion --------------------------------------------------------

    def complete(self, run_id: RunId) -> None:
        self._set_status(run_id, RunStatus.COMPLETED, ended=True)
        self._emit(run_id, EventKind.COMPLETED, status=RunStatus.COMPLETED)

    def fail(self, run_id: RunId, detail: str) -> None:
        self._set_status(run_id, RunStatus.FAILED, ended=True)
        self._emit(run_id, EventKind.FAILED, detail=detail, status=RunStatus.FAILED)

    def record_state(self, run_id: RunId, state: AgentState) -> None:
        """Note a state machine transition on the run's timeline."""
        self._emit(run_id, EventKind.STATE_CHANGED, detail=state.value, state=state)

    def note(self, run_id: RunId, kind: EventKind, detail: str = "") -> None:
        """Record an arbitrary event."""
        self._emit(run_id, kind, detail=detail)

    # -- internals ---------------------------------------------------------

    def _require(self, run_id: RunId) -> _LiveRun:
        with self._lock:
            live = self._live.get(run_id)
        if live is None:
            raise UnknownRun(run_id)
        return live

    def subscribe(self, listener: Callable[[RunEvent], None]) -> Callable[[], None]:
        """Observe events as they happen. Returns a function that unsubscribes.

        This is the only seam a user interface needs, and it points one way: the
        runtime does not know a UI exists. A listener that raises is dropped
        rather than allowed to break the run — a broken display is not a reason
        to abandon work the agent has already done.
        """
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def _emit(
        self,
        run_id: RunId,
        kind: EventKind,
        *,
        detail: str = "",
        state: AgentState | None = None,
        status: RunStatus | None = None,
    ) -> None:
        event = RunEvent(run_id=run_id, kind=kind, detail=detail, state=state, status=status)
        with self._lock:
            live = self._live.get(run_id)
            if live is not None:
                live.events.append(event)
            listeners = list(self._listeners)

        # Outside the lock: a listener that paints a terminal must not hold the
        # lock cancellation needs.
        for listener in listeners:
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - see subscribe()
                self._listeners = [item for item in self._listeners if item is not listener]

    def _set_status(
        self,
        run_id: RunId,
        status: RunStatus,
        *,
        cancel_reason: str | None = None,
        ended: bool = False,
    ) -> None:
        record = self._store.get_run(run_id)
        if record is None:
            raise UnknownRun(run_id)
        if record.is_terminal:
            raise RunAlreadyFinished(f"{run_id} is already {record.status.value}")

        updates: dict[str, object] = {"status": status}
        if cancel_reason is not None:
            updates["cancel_reason"] = cancel_reason
        if ended:
            updates["ended_at"] = utcnow()
        self._store.save_run(record.model_copy(update=updates))


__all__ = ["RunAlreadyFinished", "RunController", "UnknownRun"]
