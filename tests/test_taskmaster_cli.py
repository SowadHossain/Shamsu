from __future__ import annotations

from io import StringIO

from rich.console import Console

from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.cli.repl import (
    _handle_prd_command,
    _handle_taskmaster,
    _handle_tasks,
    _log_prd_parse_result,
    _prd_parse_summary_message,
)


def _console() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def test_taskmaster_status_detects_missing_taskmaster(monkeypatch, tmp_path):
    monkeypatch.delenv("SHAMSU_TASKMASTER_NODE", raising=False)
    monkeypatch.delenv("SHAMSU_TASKMASTER_CMD", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    console, output = _console()

    _handle_taskmaster("taskmaster status", tmp_path, console)

    text = output.getvalue()
    assert "Available: False" in text
    assert "Node.js" in text


def test_taskmaster_unknown_subcommand_shows_usage(tmp_path):
    console, output = _console()

    _handle_taskmaster("taskmaster bogus", tmp_path, console)

    assert "Usage: /taskmaster" in output.getvalue()


def test_prd_parse_requires_taskmaster_to_be_ready(monkeypatch, tmp_path):
    monkeypatch.delenv("SHAMSU_TASKMASTER_NODE", raising=False)
    monkeypatch.delenv("SHAMSU_TASKMASTER_CMD", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    prd = tmp_path / "prd.txt"
    prd.write_text("some prd", encoding="utf-8")
    console, output = _console()

    _handle_prd_command(f"prd parse {prd}", tmp_path, console)

    # H2: Taskmaster is optional now - the refusal is a SIGNPOST, not a wall.
    # It must name the built-in alternative before the setup instructions.
    rendered = output.getvalue()
    assert "Taskmaster Unavailable" in rendered
    assert "do NOT need it" in rendered
    assert "taskmaster setup" in rendered


def test_tasks_requires_taskmaster_to_be_ready(monkeypatch, tmp_path):
    monkeypatch.delenv("SHAMSU_TASKMASTER_NODE", raising=False)
    monkeypatch.delenv("SHAMSU_TASKMASTER_CMD", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    console, output = _console()

    _handle_tasks("tasks", tmp_path, console)

    rendered = output.getvalue()
    assert "Taskmaster Unavailable" in rendered
    assert "do NOT need it" in rendered


def test_prd_parse_summary_message_reports_failure_reuse_and_new_tasks():
    assert "failed" in _prd_parse_summary_message({"ok": False, "error": "boom"})
    assert "unchanged" in _prd_parse_summary_message({"ok": True, "reused_cache": True})
    assert "2 Taskmaster task" in _prd_parse_summary_message(
        {"ok": True, "reused_cache": False, "tasks": [object(), object()]}
    )


def test_log_prd_parse_result_records_prd_parsed_and_tasks_created_events(tmp_path):
    ledger = start_run(tmp_path, "parse the prd")
    set_current_run(ledger)
    try:
        _log_prd_parse_result(tmp_path / "prd.txt", {"ok": True, "reused_cache": False, "tasks": [object()]})
    finally:
        clear_current_run()

    events_text = ledger.events_path.read_text(encoding="utf-8")
    assert "prd.parsed" in events_text
    assert "tasks.created" in events_text


def test_log_prd_parse_result_skips_tasks_created_when_cache_was_reused(tmp_path):
    ledger = start_run(tmp_path, "parse the prd")
    set_current_run(ledger)
    try:
        _log_prd_parse_result(tmp_path / "prd.txt", {"ok": True, "reused_cache": True, "tasks": []})
    finally:
        clear_current_run()

    events_text = ledger.events_path.read_text(encoding="utf-8")
    assert "prd.parsed" in events_text
    assert "tasks.created" not in events_text
