"""Gap A1: one failed request must never kill the whole REPL.

Every ledger-tracked handler used to re-raise after logging, and `main()` had
no outer catch - a single Ollama stall or handler bug ended the entire session.
These tests pin the new behavior: errors are reported in-band and the loop
survives.

The same guarantee now covers Ctrl+C. Because KeyboardInterrupt is not an
`Exception`, it used to slip past that catch-all, escape the per-request
`asyncio.run`, and end `main()` - cancelling an operation cancelled the whole
session. `_RequestRunner` keeps the loop alive and cancels only the active
request; a second interrupt within two seconds still exits.
"""
from __future__ import annotations

import asyncio
import io
import signal
from pathlib import Path

import pytest
from rich.console import Console

import shamsu.cli.repl as repl
from shamsu.llm.manager import LLMStalledError
from shamsu.session.manager import SessionManager
from shamsu.types import RunStatus


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


# --- Ctrl+C cancels the operation, not the session ----------------------------


def _interrupting_request(runner: repl._RequestRunner):
    """A request that raises the interrupt on itself, then would never finish."""

    async def _never_finishes():
        runner._on_interrupt(signal.SIGINT, None)
        await asyncio.sleep(30)

    return _never_finishes()


def test_interrupt_cancels_the_request_and_the_session_survives():
    runner = repl._RequestRunner(_console())
    try:
        assert runner.run(_interrupting_request(runner)) is False
        assert runner.cancelled is True

        # The whole point: the same runner still serves the next prompt.
        async def _next_request():
            return "served"

        assert runner.run(_next_request()) is True
        assert runner.cancelled is False
    finally:
        runner.close()


def test_cancelled_request_leaves_no_orphaned_tasks():
    """No 'Task exception was never retrieved' after an interrupt."""
    runner = repl._RequestRunner(_console())
    try:
        runner.run(_interrupting_request(runner))
        assert asyncio.all_tasks(runner._loop) == set()
    finally:
        runner.close()


def test_interrupt_asks_the_active_run_to_stop_before_cancelling():
    """The run's own cancel path is what records CANCELLED and returns partial work."""
    from shamsu.runtime import run_control

    run = run_control.register_run("run-interrupt-test")
    runner = repl._RequestRunner(_console())
    try:
        runner._on_interrupt(signal.SIGINT, None)
        assert run.cancel_event.is_set()
        assert run.status is RunStatus.CANCELLING
    finally:
        runner.close()
        run_control.complete_run("run-interrupt-test", RunStatus.CANCELLED)


def test_second_interrupt_within_the_window_exits():
    runner = repl._RequestRunner(_console())
    try:
        runner._on_interrupt(signal.SIGINT, None)
        with pytest.raises(KeyboardInterrupt):
            runner._on_interrupt(signal.SIGINT, None)
    finally:
        runner.close()


def test_interrupt_tells_the_user_what_just_happened():
    console = Console(record=True, width=100)
    runner = repl._RequestRunner(console)
    try:
        runner.run(_interrupting_request(runner))
    finally:
        runner.close()
    out = console.export_text()
    assert "Cancelled that operation" in out
    assert "Ctrl+C again" in out


def test_run_request_without_a_session_runner_still_runs(monkeypatch):
    """Non-REPL callers and tests keep the plain asyncio.run behaviour."""
    monkeypatch.setattr(repl, "_REQUEST_RUNNER", None)
    seen: list[str] = []

    async def _work():
        seen.append("ran")

    assert repl._run_request(_work()) is True
    assert seen == ["ran"]
