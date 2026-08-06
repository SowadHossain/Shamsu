"""In-process run control for active native tool-calling agent runs."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.session.manager import SessionLogger
from shamsu.types import RunStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ControlledRun:
    run_id: str
    status: RunStatus = RunStatus.QUEUED
    feedback_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    feedback_event: asyncio.Event = field(default_factory=asyncio.Event)
    current_model_task: asyncio.Task[Any] | None = None
    started_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    iterations: int = 0
    last_message: str = ""
    session_logger: SessionLogger | None = None
    action_ledger: ActionLedger | None = None

    def mark(self, status: RunStatus, message: str = "") -> None:
        self.status = status
        self.updated_at = _now()
        if message:
            self.last_message = message


_RUNS: dict[str, ControlledRun] = {}


def register_run(
    run_id: str,
    *,
    session_logger: SessionLogger | None = None,
    action_ledger: ActionLedger | None = None,
) -> ControlledRun:
    run = ControlledRun(
        run_id=run_id,
        status=RunStatus.RUNNING,
        session_logger=session_logger,
        action_ledger=action_ledger,
    )
    _RUNS[run_id] = run
    if session_logger:
        session_logger.log("agent.run.started", {"run_id": run_id}, "Controlled run started", workflow_id=run_id)
    if action_ledger:
        action_ledger.log_event("controlled_run_started", controlled_run_id=run_id)
    return run


def get_run(run_id: str) -> ControlledRun | None:
    return _RUNS.get(run_id)


def cancel_run(run_id: str) -> bool:
    run = _RUNS.get(run_id)
    if run is None:
        return False
    run.mark(RunStatus.CANCELLING, "Cancellation requested.")
    run.cancel_event.set()
    if run.current_model_task and not run.current_model_task.done():
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


def add_feedback(run_id: str, text: str) -> bool:
    run = _RUNS.get(run_id)
    if run is None or run.status not in {RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.CANCELLING}:
        return False
    run.feedback_queue.put_nowait(text)
    run.feedback_event.set()
    run.updated_at = _now()
    if run.current_model_task and not run.current_model_task.done():
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


def get_run_status(run_id: str) -> dict[str, Any] | None:
    run = _RUNS.get(run_id)
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "status": run.status.value,
        "started_at": run.started_at,
        "updated_at": run.updated_at,
        "iterations": run.iterations,
        "feedback_pending": run.feedback_queue.qsize(),
        "cancel_requested": run.cancel_event.is_set(),
        "last_message": run.last_message,
    }


def complete_run(run_id: str, status: RunStatus, message: str = "") -> None:
    run = _RUNS.get(run_id)
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
