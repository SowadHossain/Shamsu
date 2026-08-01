from __future__ import annotations

from pathlib import Path

from rich.console import Console

from shamsu.session.manager import SessionManager
from shamsu.ui.trace import (
    emit_trace,
    format_trace_line,
    read_trace_mode,
    sanitize_payload,
    should_emit,
    write_trace_mode,
)


def test_trace_mode_defaults_to_normal_and_roundtrips(tmp_path: Path):
    assert read_trace_mode(tmp_path) == "normal"

    write_trace_mode(tmp_path, "verbose")
    assert read_trace_mode(tmp_path) == "verbose"

    write_trace_mode(tmp_path, "quiet")
    assert read_trace_mode(tmp_path) == "quiet"


def test_should_emit_matrix():
    # quiet prints nothing.
    assert should_emit("quiet", "normal") is False
    assert should_emit("quiet", "verbose") is False
    # normal prints normal-level events, hides verbose-level ones.
    assert should_emit("normal", "normal") is True
    assert should_emit("normal", "verbose") is False
    # verbose prints both.
    assert should_emit("verbose", "normal") is True
    assert should_emit("verbose", "verbose") is True


def test_sanitize_payload_truncates_and_redacts():
    payload = sanitize_payload({"big": "a" * 500, "secret": 'SECRET_KEY = "django-insecure-x"'})

    assert len(payload["big"]) < 500
    assert "truncated" in payload["big"]
    assert "django-insecure-x" not in payload["secret"]


def test_format_trace_line_labels_and_verbose_extras():
    normal = format_trace_line("route.detected", "qa", {"confidence": "0.90"}, "normal")
    verbose = format_trace_line("route.detected", "qa", {"confidence": "0.90"}, "verbose")

    assert normal == "Route: qa"
    assert verbose == "Route: qa [confidence=0.90]"


def test_emit_trace_prints_in_normal_hides_verbose_level(tmp_path: Path):
    write_trace_mode(tmp_path, "normal")
    console = Console(record=True)

    emit_trace(console, None, tmp_path, "route.detected", "qa", {"confidence": "0.9"}, level="normal")
    emit_trace(console, None, tmp_path, "tool.started", "read_file file=x", {"raw": "y"}, level="verbose")

    output = console.export_text()
    assert "Route: qa" in output
    # A verbose-level event is not printed while in normal mode.
    assert "read_file" not in output


def test_emit_trace_quiet_prints_nothing_but_still_logs(tmp_path: Path):
    write_trace_mode(tmp_path, "quiet")
    logger = SessionManager(tmp_path).create_session("Trace")
    console = Console(record=True)

    emit_trace(console, logger, tmp_path, "route.detected", "qa", {}, level="normal")

    assert console.export_text().strip() == ""
    event_types = [event["event_type"] for event in logger.tail(5)]
    assert "trace.route.detected" in event_types


def test_emit_trace_verbose_shows_sanitized_args(tmp_path: Path):
    write_trace_mode(tmp_path, "verbose")
    console = Console(record=True)

    emit_trace(
        console,
        None,
        tmp_path,
        "tool.started",
        "read_file",
        {"file": "src/App.tsx", "blob": "z" * 500},
        level="verbose",
    )

    output = console.export_text()
    assert "read_file" in output
    assert "src/App.tsx" in output
    # The 500-char blob is truncated to the 300-char cap before printing.
    assert output.count("z") <= 305


# -- Human-readable run reports ---------------------------------------------


def _finished_run(tmp_path: Path, prompt: str = "add a healthcheck endpoint"):
    """Drive one complete turn and return (workspace, ledger, session logger)."""
    from shamsu.action_ledger.context import clear_current_run, set_current_run
    from shamsu.action_ledger.ledger import start_run
    from shamsu.cli.request_lifecycle import finish_current_run

    logger = SessionManager(tmp_path).create_session()
    ledger = start_run(tmp_path, prompt, session_logger=logger)
    set_current_run(ledger)
    try:
        emit_trace(None, logger, tmp_path, "route.detected", "code_edit")
        # Tools reach the narrative through the ledger, which every execution
        # path uses - not through the trace, which only the chat loop emits.
        call_id = ledger.log_tool_call("write_file", {"filepath": "app/health.py"})
        ledger.log_tool_result(call_id, "write_file", True, "Wrote 12 lines")
        ledger.record_final_response("Added a /health endpoint.")
        finish_current_run(tmp_path, ledger)
    finally:
        clear_current_run()
    return ledger, logger


def test_report_records_prompt_tools_and_answer(tmp_path: Path):
    ledger, _logger = _finished_run(tmp_path)
    narrative = ledger.narrative_path.read_text(encoding="utf-8")

    assert "add a healthcheck endpoint" in narrative       # the prompt
    assert "write_file" in narrative                        # the tool it used
    assert "app/health.py" in narrative                     # what it used it on
    assert "Wrote 12 lines" in narrative                    # what happened
    assert "Added a /health endpoint." in narrative         # the answer
    assert "**Status:** success" in narrative


def test_report_is_written_even_when_console_is_quiet(tmp_path: Path):
    """Trace mode gates printing, never capture: `/trace quiet` must not cost
    you the log."""
    write_trace_mode(tmp_path, "quiet")
    ledger, _logger = _finished_run(tmp_path)

    narrative = ledger.narrative_path.read_text(encoding="utf-8")
    assert "write_file" in narrative
    assert "Added a /health endpoint." in narrative


def test_session_report_rolls_up_turns_in_order_without_interleaving(tmp_path: Path):
    from shamsu.action_ledger.context import clear_current_run, set_current_run
    from shamsu.action_ledger.ledger import start_run
    from shamsu.cli.request_lifecycle import finish_current_run

    logger = SessionManager(tmp_path).create_session()
    for prompt in ("first task", "second task"):
        ledger = start_run(tmp_path, prompt, session_logger=logger)
        set_current_run(ledger)
        try:
            emit_trace(None, logger, tmp_path, "tool.started", f"tool for {prompt}")
            ledger.record_final_response(f"finished {prompt}")
            finish_current_run(tmp_path, ledger)
        finally:
            clear_current_run()

    roll_up = (
        tmp_path / ".shamsu" / "sessions" / logger.session_id / "report.md"
    ).read_text(encoding="utf-8")
    assert roll_up.index("first task") < roll_up.index("second task")
    assert roll_up.count("finished first task") == 1
    assert roll_up.count("finished second task") == 1


def test_report_redacts_secrets_from_the_prompt(tmp_path: Path):
    ledger, _logger = _finished_run(tmp_path, prompt="deploy with api_key=sk-livesecret9876")
    narrative = ledger.narrative_path.read_text(encoding="utf-8")

    assert "sk-livesecret9876" not in narrative
    assert "[REDACTED]" in narrative


def test_layout_readme_explains_the_folder_and_survives_edits(tmp_path: Path):
    from shamsu.ui.narrative import write_layout_readme

    path = write_layout_readme(tmp_path)
    assert path is not None and "report.md" in path.read_text(encoding="utf-8")
    assert "essential" in path.read_text(encoding="utf-8")
    assert "verbose" in path.read_text(encoding="utf-8")

    path.write_text("my own notes", encoding="utf-8")
    write_layout_readme(tmp_path)
    assert path.read_text(encoding="utf-8") == "my own notes"


def test_report_closes_even_when_the_run_finished_early(tmp_path: Path):
    """The headless CLI finishes the ledger itself and only then calls
    finish_current_run (shamsu/cli/noninteractive.py), so closing the narrative
    must not depend on finalize_from_evidence having run."""
    from shamsu.action_ledger.context import clear_current_run, set_current_run
    from shamsu.action_ledger.ledger import start_run
    from shamsu.cli.request_lifecycle import finish_current_run

    logger = SessionManager(tmp_path).create_session()
    ledger = start_run(tmp_path, "headless task", session_logger=logger)
    set_current_run(ledger)
    try:
        emit_trace(None, logger, tmp_path, "tool.started", "run_command")
        ledger.finish("headless answer", status="success")
        finish_current_run(tmp_path, ledger)
    finally:
        clear_current_run()

    narrative = ledger.narrative_path.read_text(encoding="utf-8")
    assert "headless answer" in narrative
    assert "**Status:** success" in narrative
    assert (
        tmp_path / ".shamsu" / "sessions" / logger.session_id / "report.md"
    ).exists()


def test_report_records_tools_from_the_ledger_on_every_path(tmp_path: Path):
    """Tool steps come from the ActionLedger, not the trace: only the chat loop
    emits tool trace events, so a composite/scaffold run would otherwise show
    the plan and the answer with no tools in between."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "create hello.py and run it")
    call_id = ledger.log_tool_call("write_file", {"filepath": "hello.py", "content": "print(1)"})
    ledger.log_tool_result(call_id, "write_file", True, "Created hello.py (+1 lines).")
    failed = ledger.log_tool_call("run_command", {"command": "python hello.py"})
    ledger.log_tool_result(failed, "run_command", False, "Command exited with 1.")

    narrative = ledger.narrative_path.read_text(encoding="utf-8")
    assert "write_file" in narrative and "filepath=hello.py" in narrative
    assert "Created hello.py (+1 lines)." in narrative
    assert "run_command" in narrative and "command=python hello.py" in narrative
    assert "FAILED" in narrative


def test_report_does_not_double_record_chat_loop_tools(tmp_path: Path):
    """The chat loop emits tool trace events AND logs to the ledger; only one
    of the two may reach the narrative."""
    from shamsu.action_ledger.context import clear_current_run, set_current_run
    from shamsu.action_ledger.ledger import start_run

    logger = SessionManager(tmp_path).create_session()
    ledger = start_run(tmp_path, "edit a file", session_logger=logger)
    set_current_run(ledger)
    try:
        call_id = ledger.log_tool_call("write_file", {"filepath": "app.py"})
        emit_trace(
            None, logger, tmp_path, "tool.started", "write_file",
            {"tool": "write_file", "filepath": "app.py"},
        )
        ledger.log_tool_result(call_id, "write_file", True, "Wrote app.py")
        emit_trace(
            None, logger, tmp_path, "tool.finished", "Wrote app.py",
            {"tool": "write_file", "ok": True},
        )
    finally:
        clear_current_run()

    narrative = ledger.narrative_path.read_text(encoding="utf-8")
    assert narrative.count("write_file") == 1
    assert narrative.count("Wrote app.py") == 1


def test_verbose_report_contains_model_context_and_command_details(tmp_path: Path, monkeypatch):
    from shamsu.action_ledger.ledger import start_run

    monkeypatch.setenv("SHAMSU_LOG_LEVEL", "verbose")
    ledger = start_run(tmp_path, "diagnose the build")
    call_id = ledger.log_model_call_started("coder", "m", "inspect app.py")
    ledger.log_context_preview(
        {"task_id": "t1", "specialist": "coder", "token_estimate": 42, "snippets": []},
        model_call_id=call_id,
    )
    ledger.log_model_thinking(call_id, "coder", "m", "Inspect the failing file first.")
    ledger.log_model_call_finished("coder", "m", "Use a guarded lookup.", call_id=call_id)
    command_id = ledger.log_command_start("pytest -q", tmp_path)
    ledger.log_command_finish(command_id, "pytest -q", tmp_path, 1, "one passed", "one failed")
    ledger.finish("Fixed and verified.", status="success")

    report = ledger.narrative_path.read_text(encoding="utf-8")
    assert "Prompt sent to model" in report
    assert "Inspect the failing file first." in report
    assert "Context payload" in report
    assert "stdout" in report and "one passed" in report
    assert "stderr" in report and "one failed" in report


def test_essential_report_omits_verbose_model_payloads(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "answer concisely")
    call_id = ledger.log_model_call_started("qa", "m", "private model prompt")
    ledger.log_model_thinking(call_id, "qa", "m", "private reasoning trace")
    ledger.log_model_call_finished("qa", "m", "private model response", call_id=call_id)
    ledger.finish("Public answer.", status="success")

    report = ledger.narrative_path.read_text(encoding="utf-8")
    assert "Public answer." in report
    assert "private model prompt" not in report
    assert "private reasoning trace" not in report
    assert "private model response" not in report
