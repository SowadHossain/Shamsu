"""Shared request-run lifecycle helpers used by interactive and headless callers."""

from __future__ import annotations

from pathlib import Path

from shamsu.action_ledger import store as action_ledger_store
from shamsu.action_ledger.context import get_current_run
from shamsu.action_ledger.ledger import ActionLedger
from shamsu.abstract.service import AbstractService
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
    _refresh_code_memory_after_mutations(workspace, ledger)
    manifest = action_ledger_store.load_manifest(workspace, ledger.run_id)
    if manifest and manifest.get("status") == "running":
        ledger.finalize_from_evidence()
    _close_run_narrative(workspace, ledger)


def _close_run_narrative(workspace: Path, ledger: ActionLedger) -> None:
    """Seal this turn in the session log and let go of its writer.

    The single close point for both interactive and headless callers, mirroring
    `start_run`'s single open point. Best-effort: the log is observability,
    never a reason to fail a finished run."""
    if not ledger.enabled:
        return
    try:
        from shamsu.action_ledger.ledger import verdict_reason
        from shamsu.ui.turnlog import release_writer

        summary = action_ledger_store.load_summary(workspace, ledger.run_id) or {}
        manifest = action_ledger_store.load_manifest(workspace, ledger.run_id) or {}
        writer = ledger._turn_log_writer()
        writer.close_turn(
            final=action_ledger_store.load_final_output(workspace, ledger.run_id),
            status=str(summary.get("status") or manifest.get("status") or "unknown"),
            reason=verdict_reason(summary),
        )
        # A closed turn is never written to again, and its writer is holding a
        # buffer the next turn must not inherit.
        release_writer(writer)
    except Exception:
        pass


def _refresh_code_memory_after_mutations(workspace: Path, ledger: ActionLedger) -> None:
    mutations = action_ledger_store.load_mutations(workspace, ledger.run_id)
    if not any(item.get("status") == "applied" for item in mutations):
        return
    try:
        service = AbstractService(workspace)
        before = service.index_metadata()
        if not before.get("stale"):
            return
        gate = service.ensure_ready()
        after = service.index_metadata()
        refreshed = bool(after.get("exists") and not after.get("stale"))
        ledger.log_event(
            "code_memory_refresh_finished",
            ok=refreshed,
            retrieval_mode=(
                gate.status.retrieval_mode if gate.status is not None else "local"
            ),
            before=before,
            after=after,
            reason=gate.reason,
        )
    except Exception as exc:
        ledger.log_event(
            "code_memory_refresh_failed",
            error=f"{type(exc).__name__}: {exc}",
        )
