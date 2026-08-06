"""Gap A1: one failed request must never kill the whole REPL.

Every ledger-tracked handler used to re-raise after logging, and `main()` had
no outer catch - a single Ollama stall or handler bug ended the entire session.
These tests pin the new behavior: errors are reported in-band and the loop
survives. Ctrl+C (KeyboardInterrupt) is NOT an `Exception`, so exiting still
works exactly as before.
"""
from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

import shamsu.cli.repl as repl
from shamsu.llm.manager import LLMStalledError
from shamsu.session.manager import SessionManager


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


def test_report_request_error_generic_names_the_error_and_reassures():
    console = Console(record=True, width=100)
    repl._report_request_error(KeyError("boom"), console, None)
    out = console.export_text()
    assert "Request failed" in out
    assert "KeyError" in out
    assert "session is fine" in out


def test_report_request_error_stall_gets_actionable_hint():
    console = Console(record=True, width=100)
    exc = LLMStalledError("deepseek-r1:7b produced no output for 180s")
    repl._report_request_error(exc, console, None)
    out = console.export_text()
    assert "ollama ps" in out
    assert "SHAMSU_LLM_IDLE_TIMEOUT" in out


def test_report_request_error_logs_to_session(tmp_path: Path):
    import json

    logger = SessionManager(tmp_path).create_session("CrashGuard")
    repl._report_request_error(ValueError("bad"), _console(), logger)
    lines = logger.events_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines if "request.failed" in line]
    assert events
    assert events[0]["payload"]["error_type"] == "ValueError"


def test_report_request_error_survives_a_broken_logger():
    """The last-resort handler itself must never raise."""

    class BrokenLogger:
        def log(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("logging is down too")

    repl._report_request_error(ValueError("bad"), _console(), BrokenLogger())


def test_resolve_proceed_survives_a_failing_plan(tmp_path: Path, monkeypatch):
    """A plan that blows up mid-execution is reported, not propagated - and the
    caller is told (True) that something WAS pending, so it doesn't print the
    misleading 'nothing to proceed' message."""
    logger = SessionManager(tmp_path).create_session("FailingPlan")
    logger.set_pending_action(
        {"type": "plan", "awaiting": "plan_approval", "plan_id": "p1", "route": "code_edit"}
    )

    async def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise LLMStalledError("stalled mid-plan")

    monkeypatch.setattr(repl, "_execute_pending_plan", _boom)
    console = Console(record=True, width=100)

    assert repl._resolve_proceed(tmp_path, console, logger) is True

    out = console.export_text()
    assert "Request failed" in out
    # The pending action was consumed - a retry won't ghost-execute it.
    assert logger.get_pending_action().get("awaiting") != "plan_approval"
