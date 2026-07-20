"""Shared request-run lifecycle helpers used by interactive and headless callers."""

from __future__ import annotations

from pathlib import Path

from shamsu.action_ledger import store as action_ledger_store
from shamsu.action_ledger.context import get_current_run
from shamsu.action_ledger.ledger import ActionLedger
from shamsu.session.manager import SessionLogger


def log_event(
    session_logger: SessionLogger | None,
    event_type: str,
    payload: dict,
    summary: str,
    workflow_id: str | None = None,
) -> None:
    if session_logger:
        session_logger.log(event_type, payload, summary, workflow_id=workflow_id)


def log_assistant_message(
    session_logger: SessionLogger | None,
    message: str,
    workflow_id: str | None = None,
) -> None:
    if session_logger and message:
        session_logger.log(
            "assistant.message",
            {"message": message},
            "Assistant responded",
            workflow_id=workflow_id,
        )
        try:
            session_logger.set_last_assistant_summary(message)
        except Exception:
            pass
    ledger = get_current_run()
    if ledger and message:
        ledger.log_task_classified(workflow_id or "unknown")
        ledger.record_final_response(message)


def finish_current_run(workspace: Path, ledger: ActionLedger) -> None:
    manifest = action_ledger_store.load_manifest(workspace, ledger.run_id)
    if manifest and manifest.get("status") == "running":
        ledger.finalize_from_evidence()
