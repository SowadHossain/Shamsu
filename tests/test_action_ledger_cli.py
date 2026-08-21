from __future__ import annotations

import zipfile
from io import StringIO
from pathlib import Path

from rich.console import Console

from shamsu.action_ledger.ledger import start_run
from shamsu.action_ledger import store
from shamsu.cli.repl import _handle_run, _handle_runs


def _console() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def _seeded_run(tmp_path: Path):
    ledger = start_run(tmp_path, "fix the login bug")
    ledger.log_decision(
        "run_build_after_patch",
        reason_summary="Verification required after a source change.",
        chosen_action="npm run build",
        outcome="verification_failed",
    )
    call_id = ledger.log_tool_call("read_file", {"filepath": "app.py"})
    ledger.log_tool_result(call_id, "read_file", True, "ok", {})
    cmd_id = ledger.log_command_start("npm run build", tmp_path)
    ledger.log_command_finish(cmd_id, "npm run build", tmp_path, 1, "building...", "error: TS2304")
    ledger.log_context_preview({"task_id": "t1", "specialist": "qa", "snippets": []})
    ledger.finish("I fixed the login bug.", status="success")
    return ledger


def test_runs_lists_recent_runs(tmp_path: Path):
    ledger = _seeded_run(tmp_path)
    console, output = _console()

    _handle_runs("runs", tmp_path, console)

    text = output.getvalue()
    assert ledger.run_id in text
    assert "success" in text


def test_run_last_shows_latest_run(tmp_path: Path):
    ledger = _seeded_run(tmp_path)
    console, output = _console()

    _handle_run("run last", tmp_path, console)

    text = output.getvalue()
    assert ledger.run_id in text
    assert "Status: success" in text
    assert "Decision summary: npm run build" in text
    assert "Tool outcomes: read_file=success" in text
    assert "Verification: not run" in text
    assert "Output: I fixed the login bug." in text


def test_run_timeline_shows_events(tmp_path: Path):
    ledger = _seeded_run(tmp_path)
    console, output = _console()

    _handle_run(f"run timeline {ledger.run_id}", tmp_path, console)

    text = output.getvalue()
    assert "run_started" in text
    assert "command_finished" in text


def test_run_decisions_shows_decision_summaries(tmp_path: Path):
    ledger = _seeded_run(tmp_path)
    console, output = _console()

    _handle_run(f"run decisions {ledger.run_id}", tmp_path, console)

    text = output.getvalue()
    assert "run_build_after_patch" in text
    assert "npm run build" in text


def test_run_tools_shows_tool_calls_and_outcomes(tmp_path: Path):
    ledger = _seeded_run(tmp_path)
    console, output = _console()

    _handle_run(f"run tools {ledger.run_id}", tmp_path, console)

    text = output.getvalue()
    assert "read_file" in text
    assert "called" in text
    assert "finished" in text


def test_run_commands_shows_exit_codes_and_log_paths(tmp_path: Path):
    ledger = _seeded_run(tmp_path)
    console, output = _console()

    _handle_run(f"run commands {ledger.run_id}", tmp_path, console)

    text = output.getvalue()
    assert "npm run build" in text
    assert "cmd_000.stdout.log" in text
    assert "cmd_000.stderr.log" in text


def test_run_context_shows_safe_context_preview(tmp_path: Path):
    ledger = _seeded_run(tmp_path)
    console, output = _console()

    _handle_run(f"run context {ledger.run_id}", tmp_path, console)

    text = output.getvalue()
    assert "t1" in text
    assert "qa" in text


def test_run_validate_reports_integrity(tmp_path: Path):
    ledger = _seeded_run(tmp_path)
    console, output = _console()

    _handle_run(f"run validate {ledger.run_id}", tmp_path, console)

    assert "Integrity: valid" in output.getvalue()


def test_run_export_creates_redacted_zip(tmp_path: Path):
    ledger = start_run(tmp_path, "fix bug password = \"hunter2\"")
    ledger.log_decision("d", "reason with api_key = \"sk-abcdefghijklmnopqrstuvwxyz123456\"", chosen_action="a")
    ledger.finish("done")
    console, output = _console()

    _handle_run(f"run export {ledger.run_id}", tmp_path, console)

    zip_path = tmp_path / ".shamsu" / "runs" / ledger.run_id / "exports" / f"{ledger.run_id}.zip"
    assert zip_path.exists()
    assert "Exported run" in output.getvalue()
    with zipfile.ZipFile(zip_path) as archive:
        contents = "\n".join(archive.read(name).decode("utf-8", errors="replace") for name in archive.namelist())
    assert "hunter2" not in contents
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in contents


def _age_run(tmp_path: Path, ledger, days: int) -> None:
    from datetime import datetime, timedelta, timezone

    manifest = store.load_manifest(tmp_path, ledger.run_id)
    manifest["started_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    ledger._write_json(ledger.manifest_path, manifest)


def test_run_clean_asks_for_confirmation_before_deleting(tmp_path: Path):
    ledger = start_run(tmp_path, "old run")
    ledger.finish("done")
    _age_run(tmp_path, ledger, days=90)

    console, output = _console()
    _handle_run("run clean", tmp_path, console, approval_func=lambda _request: False)

    assert "cancelled" in output.getvalue().lower()
    assert store.list_run_ids(tmp_path) == [ledger.run_id]  # nothing deleted - denied


def test_run_clean_deletes_after_approval(tmp_path: Path):
    ledger = start_run(tmp_path, "old run")
    ledger.finish("done")
    _age_run(tmp_path, ledger, days=90)

    console, output = _console()
    _handle_run("run clean", tmp_path, console, approval_func=lambda _request: True)

    assert "Removed 1 run" in output.getvalue()
    assert store.list_run_ids(tmp_path) == []  # now actually removed


# -- Log discoverability: /logs and /run report|prompt|cot ------------------


def test_logs_command_points_at_human_report_and_the_detail_level(tmp_path: Path):
    from shamsu.cli.session_commands import handle_logs

    console, output = _console()
    handle_logs("logs", tmp_path, console)
    output = output.getvalue()

    assert "log-summary.md" in output
    assert "log-detailed.md" in output
    assert "attachments" in output
    assert "agent-development-log.jsonl" in output
    assert ".evidence" in output
    assert "essential" in output
    assert "SHAMSU_LOG_LEVEL" in output
    # Nothing has run yet, so say so rather than printing an empty table.
    assert "No runs recorded yet" in output


def test_logs_command_lists_recent_runs_once_they_exist(tmp_path: Path):
    from shamsu.cli.session_commands import handle_logs

    start_run(tmp_path, "fix the login bug")
    console, output = _console()
    handle_logs("logs", tmp_path, console)
    output = output.getvalue()

    assert "Recent runs" in output
    assert "fix the login bug" in output
    assert "No runs recorded yet" not in output


def test_logs_open_lists_every_path_including_the_layout_notes(tmp_path: Path):
    from shamsu.cli.session_commands import handle_logs

    console, output = _console()
    handle_logs("logs open", tmp_path, console)
    output = output.getvalue()

    for label in ("runs", "sessions", "one-file log", "audit", "ledger config", "layout notes"):
        assert label in output


def test_logs_mode_updates_workspace_config_for_the_next_run(tmp_path: Path):
    from shamsu.action_ledger.config import load_config
    from shamsu.cli.session_commands import handle_logs

    console, output = _console()
    handle_logs("logs mode verbose", tmp_path, console)

    assert load_config(tmp_path)["log_level"] == "verbose"
    assert "Log mode set to verbose" in output.getvalue()
    assert start_run(tmp_path, "inspect deeply").log_level == "verbose"


def test_run_report_prompt_and_cot_read_back_verbose_artifacts(tmp_path: Path, monkeypatch):
    from shamsu.action_ledger.context import clear_current_run, set_current_run
    from shamsu.action_ledger.ledger import start_run
    from shamsu.cli.request_lifecycle import finish_current_run
    from shamsu.cli.session_commands import handle_run

    monkeypatch.setenv("SHAMSU_LOG_LEVEL", "verbose")
    ledger = start_run(tmp_path, "add a healthcheck endpoint")
    set_current_run(ledger)
    try:
        call_id = ledger.log_model_call_started(
            "coder", "m", system="You are SHAMSU.", messages=[{"role": "user", "content": "go"}]
        )
        ledger.log_model_thinking(call_id, "coder", "m", "First find the router module.")
        ledger.record_final_response("Added a /health endpoint.")
        finish_current_run(tmp_path, ledger)
    finally:
        clear_current_run()

    report, report_out = _console()
    handle_run("run report", tmp_path, report)
    assert "add a healthcheck endpoint" in report_out.getvalue()

    prompt, prompt_out = _console()
    handle_run("run prompt", tmp_path, prompt)
    assert "You are SHAMSU." in prompt_out.getvalue()

    cot, cot_out = _console()
    handle_run("run cot", tmp_path, cot)
    assert "First find the router module." in cot_out.getvalue()


def test_run_cot_explains_itself_when_nothing_was_captured(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run
    from shamsu.cli.session_commands import handle_run

    start_run(tmp_path, "no reasoning here")
    console, output = _console()
    handle_run("run cot", tmp_path, console)

    assert "No chain-of-thought artifacts" in output.getvalue()
