from __future__ import annotations

from io import StringIO

from rich.console import Console

from shamsu.action_ledger.ledger import start_run
from shamsu.cli import repl
from shamsu.cli.approval_ui import make_approval_manager
from shamsu.cli.arguments import parse_args
from shamsu.cli.request_lifecycle import finish_current_run, log_assistant_message
from shamsu.cli.session_commands import handle_run, handle_runs
from shamsu.safety.approval_context import approval_override


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, width=120)


def test_repl_reexports_modular_argument_and_session_command_contracts():
    assert repl.parse_args is parse_args
    assert repl._handle_run is handle_run
    assert repl._handle_runs is handle_runs


def test_approval_ui_uses_request_scoped_harness_policy(tmp_path):
    calls = []

    with approval_override(lambda request: calls.append(request.action_type) or True):
        manager = make_approval_manager(tmp_path, None, _console())
        approved = manager.ask(
            repl.ApprovalRequest(
                action_type="run_command",
                description="Run tests",
                risk_level="medium",
            )
        )

    assert approved is True
    assert calls == ["run_command"]


def test_request_lifecycle_records_output_and_finishes_from_evidence(tmp_path):
    ledger = start_run(tmp_path, "where am i")
    from shamsu.action_ledger.context import clear_current_run, set_current_run

    set_current_run(ledger)
    try:
        log_assistant_message(None, "You are in the workspace.", workflow_id="workspace.location")
        finish_current_run(tmp_path, ledger)
    finally:
        clear_current_run()

    manifest = repl.action_ledger_store.load_manifest(tmp_path, ledger.run_id)
    assert manifest["status"] == "success"
    assert ledger.final_output_path.read_text(encoding="utf-8") == "You are in the workspace."
