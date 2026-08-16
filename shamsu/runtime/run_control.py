"""In-process run control for active native tool-calling agent runs."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.session.manager import SessionLogger
from shamsu.types import RunStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_TERMINAL_STATUSES = {
    RunStatus.CANCELLED,
    RunStatus.TIMED_OUT,
    RunStatus.FAILED,
    RunStatus.COMPLETED,
}


@dataclass
class ControlledRun:
    run_id: str
    status: RunStatus = RunStatus.CREATED
    feedback_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    feedback_event: asyncio.Event = field(default_factory=asyncio.Event)
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    current_model_task: asyncio.Task[Any] | None = None
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    deadline_at: float | None = None
    max_runtime_seconds: float | None = None
    iterations: int = 0
    last_message: str = ""
    session_logger: SessionLogger | None = None
    action_ledger: ActionLedger | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    active: bool = True

    def __post_init__(self) -> None:
        self.pause_event.set()

    def mark(self, status: RunStatus, message: str = "", **payload: Any) -> None:
        previous = self.status
        self.status = status
        self.updated_at = _now()
        if message:
            self.last_message = message
        self.record_event(
            "status_changed",
            previous_status=previous.value,
            status=status.value,
            message=message,
            **payload,
        )

    def record_event(self, event_type: str, **payload: Any) -> None:
        self.updated_at = _now()
        self.events.append(
            {
                "type": event_type,
                "run_id": self.run_id,
                "timestamp": self.updated_at,
                **payload,
            }
        )


_RUNS: dict[str, ControlledRun] = {}
_RUN_HISTORY: dict[str, ControlledRun] = {}
_CURRENT_RUN: contextvars.ContextVar[ControlledRun | None] = contextvars.ContextVar(
    "shamsu_controlled_run",
    default=None,
)


def register_run(
    run_id: str,
    *,
    session_logger: SessionLogger | None = None,
    action_ledger: ActionLedger | None = None,
    max_runtime_seconds: float | None = None,
) -> ControlledRun:
    deadline_at = None
    if max_runtime_seconds is not None and max_runtime_seconds > 0:
        deadline_at = time.monotonic() + max_runtime_seconds
    run = ControlledRun(
        run_id=run_id,
        session_logger=session_logger,
        action_ledger=action_ledger,
        max_runtime_seconds=max_runtime_seconds,
        deadline_at=deadline_at,
    )
    _RUNS[run_id] = run
    _RUN_HISTORY[run_id] = run
    run.record_event(
        "run_created",
        status=RunStatus.CREATED.value,
        max_runtime_seconds=max_runtime_seconds,
    )
    run.mark(RunStatus.RUNNING, "Run started.")
    if session_logger:
        session_logger.log("agent.run.started", {"run_id": run_id}, "Controlled run started", workflow_id=run_id)
    if action_ledger:
        action_ledger.log_event("controlled_run_started", controlled_run_id=run_id)
    return run


@contextlib.contextmanager
def bind_run(run: ControlledRun):
    token = _CURRENT_RUN.set(run)
    try:
        yield run
    finally:
        _CURRENT_RUN.reset(token)


def current_run() -> ControlledRun | None:
    return _CURRENT_RUN.get()


def get_run(run_id: str) -> ControlledRun | None:
    return _RUNS.get(run_id)


def get_known_run(run_id: str) -> ControlledRun | None:
    return _RUNS.get(run_id) or _RUN_HISTORY.get(run_id)


def active_run_ids() -> list[str]:
    return sorted(_RUNS)


def active_runs_for_session(session_id: str) -> list[ControlledRun]:
    return [
        run
        for run in _RUNS.values()
        if run.session_logger is not None and run.session_logger.session_id == session_id
    ]


def _cleanup_run(run: ControlledRun) -> None:
    run.active = False
    run.current_model_task = None
    _RUNS.pop(run.run_id, None)
    run.record_event("run_cleaned_up", status=run.status.value)


def cancel_run(run_id: str) -> bool:
    run = _RUNS.get(run_id)
    if run is None:
        return False
    run.mark(RunStatus.CANCELLING, "Cancellation requested.")
    run.cancel_event.set()
    if run.current_model_task and not run.current_model_task.done():
        run.record_event("model_call_cancel_requested")
        run.current_model_task.cancel()
    if run.session_logger:
        run.session_logger.log(
            "agent.run.cancel_requested",
            {"run_id": run_id},
            "Cancellation requested for active run",
            workflow_id=run_id,
        )
    if run.action_ledger:
        run.action_ledger.log_cancel_requested()
    return True


def pause_run(run_id: str) -> bool:
    run = _RUNS.get(run_id)
    if run is None:
        return False
    run.pause_event.clear()
    run.record_event("pause_requested")
    if run.session_logger:
        run.session_logger.log(
            "agent.run.pause_requested",
            {"run_id": run_id},
            "Pause requested for active run",
            workflow_id=run_id,
        )
    return True


def resume_run(run_id: str) -> bool:
    run = _RUNS.get(run_id)
    if run is None:
        return False
    run.pause_event.set()
    run.record_event("resume_requested")
    if run.session_logger:
        run.session_logger.log(
            "agent.run.resume_requested",
            {"run_id": run_id},
            "Resume requested for active run",
            workflow_id=run_id,
        )
    return True


async def wait_if_paused(run: ControlledRun) -> None:
    if run.pause_event.is_set():
        return
    run.record_event("run_paused")
    await run.pause_event.wait()
    run.record_event("run_resumed")


def add_feedback(run_id: str, text: str) -> bool:
    run = _RUNS.get(run_id)
    if run is None or run.status not in {
        RunStatus.CREATED,
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_FOR_APPROVAL,
        RunStatus.CANCELLING,
    }:
        return False
    run.feedback_queue.put_nowait(text)
    run.feedback_event.set()
    run.updated_at = _now()
    run.record_event("feedback_added", text_preview=text[:500])
    if run.current_model_task and not run.current_model_task.done():
        run.record_event("model_call_interrupted_for_feedback")
        run.current_model_task.cancel()
    if run.session_logger:
        run.session_logger.log(
            "agent.feedback_added",
            {"run_id": run_id, "text": text},
            "Feedback added to active run",
            workflow_id=run_id,
        )
    if run.action_ledger:
        run.action_ledger.log_feedback_added(text)
    return True


def mark_waiting_for_approval(message: str = "Waiting for user approval.") -> RunStatus | None:
    run = current_run()
    if run is None or run.status in _TERMINAL_STATUSES:
        return None
    previous = run.status
    run.mark(RunStatus.WAITING_FOR_APPROVAL, message)
    return previous


def restore_after_approval(previous: RunStatus | None) -> None:
    run = current_run()
    if run is None or previous is None or run.status in _TERMINAL_STATUSES:
        return
    if run.cancel_event.is_set():
        run.mark(RunStatus.CANCELLING, "Cancellation requested.")
        return
    run.mark(RunStatus.RUNNING, "Approval resolved.")


def time_remaining(run: ControlledRun) -> float | None:
    if run.deadline_at is None:
        return None
    return max(0.0, run.deadline_at - time.monotonic())


def timed_out(run: ControlledRun) -> bool:
    remaining = time_remaining(run)
    return remaining is not None and remaining <= 0


def get_run_status(run_id: str) -> dict[str, Any] | None:
    run = get_known_run(run_id)
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "started_at": run.started_at,
        "updated_at": run.updated_at,
        "max_runtime_seconds": run.max_runtime_seconds,
        "deadline_at": run.deadline_at,
        "iterations": run.iterations,
        "feedback_pending": run.feedback_queue.qsize(),
        "cancel_requested": run.cancel_event.is_set(),
        "paused": not run.pause_event.is_set(),
        "active": run.active,
        "current_model_task_active": bool(
            run.current_model_task and not run.current_model_task.done()
        ),
        "last_message": run.last_message,
    }


def get_run_events(run_id: str) -> list[dict[str, Any]]:
    run = get_known_run(run_id)
    if run is None:
        return []
    return list(run.events)


def complete_run(run_id: str, status: RunStatus, message: str = "") -> None:
    run = get_known_run(run_id)
    if run is None:
        return
    run.mark(status, message)
    if run.session_logger:
        run.session_logger.log(
            "agent.run.finished",
            {"run_id": run_id, "status": status.value, "message": message},
            f"Controlled run finished: {status.value}",
            workflow_id=run_id,
        )
    if run.action_ledger and status == RunStatus.CANCELLED:
        run.action_ledger.log_run_cancelled()
    if status in _TERMINAL_STATUSES:
        _cleanup_run(run)
