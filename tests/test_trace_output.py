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




def test_summary_records_prompt_tools_and_answer(tmp_path: Path):
    ledger, _logger = _finished_run(tmp_path)
    summary = ledger.summary_log_path.read_text(encoding="utf-8")

    assert "add a healthcheck endpoint" in summary        # the prompt
    assert "app/health.py" in summary                     # what it acted on
    assert "Wrote 12 lines" in summary                    # what happened
    assert "Added a /health endpoint." in summary         # the answer
    assert "success" in summary


def test_summary_and_detail_are_written_side_by_side(tmp_path: Path):
    """Both documents exist, cross-link, and share the session directory with
    the transcript - the layout the whole refactor is for."""
    ledger, logger = _finished_run(tmp_path)
    session_dir = tmp_path / ".shamsu" / "sessions" / logger.session_id

    assert (session_dir / "log-summary.md").is_file()
    assert (session_dir / "log-detailed.md").is_file()
    assert (session_dir / "session.json").is_file()

    summary = (session_dir / "log-summary.md").read_text(encoding="utf-8")
    detail = (session_dir / "log-detailed.md").read_text(encoding="utf-8")
    assert "log-detailed.md" in summary          # summary links out
    assert "log-summary.md" in detail            # detail links back


def test_summary_links_resolve_to_anchors_that_exist(tmp_path: Path):
    """A `detail` link that lands nowhere is worse than no link: it reads as a
    promise the document does not keep."""
    import re

    ledger, _logger = _finished_run(tmp_path)
    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    detail = ledger.detail_log_path.read_text(encoding="utf-8")

    targets = re.findall(r"\(log-detailed\.md#([a-z0-9_-]+)\)", summary)
    assert targets, "no detail links were written at all"
    for anchor in targets:
        assert f'<a id="{anchor}"></a>' in detail, f"dangling link: {anchor}"
    assert len(set(targets)) == len(targets), "an anchor was reused"


def test_log_is_written_even_when_console_is_quiet(tmp_path: Path):
    """Trace mode gates printing, never capture: `/trace quiet` must not cost
    you the log."""
    write_trace_mode(tmp_path, "quiet")
    ledger, _logger = _finished_run(tmp_path)

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    assert "app/health.py" in summary
    assert "Added a /health endpoint." in summary


def test_turns_append_in_order_without_interleaving(tmp_path: Path):
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

    summary = (
        tmp_path / ".shamsu" / "sessions" / logger.session_id / "log-summary.md"
    ).read_text(encoding="utf-8")
    assert summary.index("first task") < summary.index("second task")
    assert summary.count("finished first task") == 1
    assert summary.count("finished second task") == 1


def test_a_second_turn_is_not_treated_as_already_closed(tmp_path: Path):
    """The close marker is per turn. Scanning the whole document for it would
    make every turn after the first one silently skip its own result."""
    from shamsu.action_ledger.context import clear_current_run, set_current_run
    from shamsu.action_ledger.ledger import start_run
    from shamsu.cli.request_lifecycle import finish_current_run

    logger = SessionManager(tmp_path).create_session()
    for prompt in ("one", "two", "three"):
        ledger = start_run(tmp_path, prompt, session_logger=logger)
        set_current_run(ledger)
        try:
            ledger.record_final_response(f"answer to {prompt}")
            finish_current_run(tmp_path, ledger)
        finally:
            clear_current_run()

    summary = (
        tmp_path / ".shamsu" / "sessions" / logger.session_id / "log-summary.md"
    ).read_text(encoding="utf-8")
    for prompt in ("one", "two", "three"):
        assert f"answer to {prompt}" in summary
    assert summary.count("<!-- turn-closed -->") == 3


def test_log_redacts_secrets_from_the_prompt(tmp_path: Path):
    ledger, _logger = _finished_run(tmp_path, prompt="deploy with api_key=sk-livesecret9876")

    for path in (ledger.summary_log_path, ledger.detail_log_path):
        text = path.read_text(encoding="utf-8")
        assert "sk-livesecret9876" not in text
    assert "[REDACTED]" in ledger.summary_log_path.read_text(encoding="utf-8")


def test_layout_readme_explains_the_folder_and_survives_edits(tmp_path: Path):
    from shamsu.ui.turnlog import write_layout_readme

    path = write_layout_readme(tmp_path)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "log-summary.md" in text
    assert "log-detailed.md" in text
    assert "attachments/" in text
    assert "essential" in text and "verbose" in text

    path.write_text("my own notes", encoding="utf-8")
    write_layout_readme(tmp_path)
    assert path.read_text(encoding="utf-8") == "my own notes"


def test_log_closes_even_when_the_run_finished_early(tmp_path: Path):
    """The headless CLI finishes the ledger itself and only then calls
    finish_current_run (shamsu/cli/noninteractive.py), so closing the log must
    not depend on finalize_from_evidence having run."""
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

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    assert "headless answer" in summary
    assert "success" in summary
    assert summary.count("<!-- turn-closed -->") == 1


def test_tools_are_recorded_from_the_ledger_on_every_path(tmp_path: Path):
    """Tool steps come from the ActionLedger, not the trace: only the chat loop
    emits tool trace events, so a composite/scaffold run would otherwise show
    the plan and the answer with no tools in between."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "create hello.py and run it")
    call_id = ledger.log_tool_call("write_file", {"filepath": "hello.py", "content": "print(1)"})
    ledger.log_tool_result(call_id, "write_file", True, "Created hello.py (+1 lines).")
    failed = ledger.log_tool_call("run_command", {"command": "python hello.py"})
    ledger.log_tool_result(failed, "run_command", False, "Command exited with 1.")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    assert "hello.py" in summary
    assert "Created hello.py (+1 lines)." in summary
    assert "python hello.py" in summary
    assert "FAILED" in summary


def test_chat_loop_tools_are_not_recorded_twice(tmp_path: Path):
    """The chat loop emits tool trace events AND logs to the ledger; only one
    of the two may reach the summary."""
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

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    assert summary.count("app.py`") == 1
    assert summary.count("Wrote app.py") == 1


def test_verbose_detail_carries_prompt_context_and_command_output(tmp_path: Path, monkeypatch):
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

    detail = ledger.detail_log_path.read_text(encoding="utf-8")
    assert "Prompt sent" in detail and "inspect app.py" in detail
    assert "Inspect the failing file first." in detail
    assert "Context payload" in detail
    assert "stdout" in detail and "one passed" in detail
    assert "stderr" in detail and "one failed" in detail


def test_essential_detail_keeps_the_answer_and_drops_the_prompt(tmp_path: Path):
    """The two levels still differ, but the line moved.

    With one document, `essential` had to withhold the model's own words to stay
    readable. With two, the summary stays skimmable on its own, so the response
    and the reasoning trace - the two things you actually debug a small model
    with - are kept at both levels. What `essential` still withholds is the
    bulk: the prompt that was sent, and the context payload."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "answer concisely")
    call_id = ledger.log_model_call_started("qa", "m", "private model prompt")
    ledger.log_model_thinking(call_id, "qa", "m", "the reasoning trace")
    ledger.log_model_call_finished("qa", "m", "the model response", call_id=call_id)
    ledger.finish("Public answer.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    detail = ledger.detail_log_path.read_text(encoding="utf-8")

    assert "Public answer." in summary
    assert "private model prompt" not in detail
    assert "the model response" in detail
    assert "the reasoning trace" in detail


# -- The five things the flat report could not show -------------------------


def test_reasoning_is_a_sub_panel_inside_the_model_entry(tmp_path: Path):
    """Not a separate row, and not a separate file: the trace belongs to the
    response it produced, so it renders collapsed inside that entry."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "think about it")
    call_id = ledger.log_model_call_started("coder", "m", "prompt")
    ledger.log_model_thinking(call_id, "coder", "m", "First I check where it is read.")
    ledger.log_model_call_finished("coder", "m", "Checking config.py.", call_id=call_id)
    ledger.finish("Done.", status="success")

    detail = ledger.detail_log_path.read_text(encoding="utf-8")
    entry = detail[detail.index("Model responded") :]
    trace_at = entry.index("First I check where it is read.")
    response_at = entry.index("Checking config.py.")
    assert "<details>" in entry[:trace_at]              # collapsed by default
    assert "Reasoning trace" in entry[:trace_at]
    assert trace_at < response_at                       # trace, then the answer
    assert "reasoning captured" in ledger.summary_log_path.read_text(encoding="utf-8")


def test_inline_thought_tags_are_pulled_out_of_the_answer(tmp_path: Path):
    """A model that was never asked for structured reasoning writes it into the
    response. It still belongs in the panel, not in front of the answer."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "think inline")
    ledger.log_model_call_finished(
        "coder", "m", "<think>weighing the options</think>The answer is 4.",
        call_id=ledger.log_model_call_started("coder", "m", "prompt"),
    )
    ledger.finish("Done.", status="success")

    detail = ledger.detail_log_path.read_text(encoding="utf-8")
    assert "weighing the options" in detail
    assert "Reasoning trace" in detail
    response_block = detail[detail.index("**Response**") :]
    assert "The answer is 4." in response_block
    assert "weighing the options" not in response_block


def test_approvals_get_their_own_row(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "edit the config")
    ledger.log_event(
        "approval_granted",
        action_type="file_edit",
        approved=True,
        decision_scope="once",
        decision_source="interactive_menu",
        request={
            "action_type": "file_edit",
            "risk_level": "medium",
            "target_paths": ["config.py"],
            "preview": "- old line\n+ new line",
        },
    )
    ledger.finish("Edited.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    detail = ledger.detail_log_path.read_text(encoding="utf-8")
    assert "Approval" in summary and "file_edit" in summary
    assert "approved" in summary and "config.py" in summary
    assert "Preview shown for approval" in detail
    assert "+ new line" in detail


def test_a_denied_approval_is_not_reported_as_approved(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "delete everything")
    ledger.log_event(
        "approval_denied",
        action_type="file_delete",
        approved=False,
        request={"action_type": "file_delete", "target_paths": ["app.py"]},
    )
    ledger.finish("Stopped.", status="denied")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    assert "DENIED" in summary
    assert "**approved**" not in summary


def test_a_retry_is_grouped_with_the_attempt_it_superseded(tmp_path: Path):
    """Two rows on the same file with nothing joining them made the reader
    notice the filename matched. One group says it outright."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "fix the syntax error")
    ledger.log_mutation_finished("tx1", "rolled_back", ["config.py"], "SyntaxError")
    ledger.log_verification_result(
        False, "SyntaxError", command='python -c "import config"', exit_code=1, files=["config.py"]
    )
    ledger.log_mutation_finished("tx2", "applied", ["config.py"], "")
    ledger.log_verification_result(
        True, "", command='python -c "import config"', exit_code=0, files=["config.py"]
    )
    ledger.finish("Fixed.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    assert "1 of 2 kept" in summary
    assert "~~Attempt 1~~" in summary          # struck through: it did not survive
    assert "Attempt 2" in summary
    # The verification that decided each attempt sits inside that attempt.
    detail = ledger.detail_log_path.read_text(encoding="utf-8")
    first = detail[detail.index("Attempt 1") : detail.index("Attempt 2")]
    assert "SyntaxError" in first


def test_a_lone_write_is_not_dressed_up_as_a_retry_group(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "write one file")
    ledger.log_mutation_finished("tx1", "applied", ["app.py"], "")
    ledger.finish("Written.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    assert "app.py" in summary
    assert "kept" not in summary
    assert "Attempt" not in summary


def test_writes_to_different_files_are_not_grouped_together(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "write two files")
    ledger.log_mutation_finished("tx1", "applied", ["a.py"], "")
    ledger.log_mutation_finished("tx2", "applied", ["b.py"], "")
    ledger.finish("Written.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    assert "a.py" in summary and "b.py" in summary
    assert "of 2 kept" not in summary


def test_a_telegram_turn_is_badged_with_its_surface(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run

    logger = SessionManager(tmp_path).create_session()
    ledger = start_run(tmp_path, "fix it", session_logger=logger, source="telegram")
    ledger.finish("Fixed.", status="success")

    assert "`telegram`" in ledger.summary_log_path.read_text(encoding="utf-8")


def test_a_local_turn_is_badged_cli_by_default(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "fix it")
    ledger.finish("Fixed.", status="success")

    assert "`cli`" in ledger.summary_log_path.read_text(encoding="utf-8")


def test_an_oversized_tool_result_is_linked_not_inlined(tmp_path: Path):
    """A 900-line file read does not belong in either document."""
    from shamsu.action_ledger.ledger import start_run
    from shamsu.ui.turnlog import OVERFLOW_CHARS

    ledger = start_run(tmp_path, "read the log")
    huge = "\n".join(f"2026-08-20 line {index} boot: KeyError" for index in range(900))
    assert len(huge) > OVERFLOW_CHARS
    call_id = ledger.log_tool_call("read_file", {"filepath": "server.log"})
    ledger.log_tool_result(call_id, "read_file", True, huge)
    ledger.finish("Read.", status="success")

    detail = ledger.detail_log_path.read_text(encoding="utf-8")
    assert "over the inline threshold" in detail
    assert "attachments/" in detail
    assert len(detail) < len(huge), "the payload was inlined anyway"

    spilled = sorted(ledger.log_attachments_dir.glob("*"))
    assert spilled, "nothing was written to attachments/"
    assert spilled[0].read_text(encoding="utf-8") == huge   # full fidelity kept
    # A head of the payload stays inline so the row still says something.
    assert "2026-08-20 line 0 boot" in detail


def test_a_small_tool_result_stays_inline(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "read a small file")
    call_id = ledger.log_tool_call("read_file", {"filepath": "app.py"})
    ledger.log_tool_result(call_id, "read_file", True, "print('hello')")
    ledger.finish("Read.", status="success")

    assert "print('hello')" in ledger.detail_log_path.read_text(encoding="utf-8")
    assert not ledger.log_attachments_dir.exists()


# -- The refactor must not cost the harness its telemetry -------------------


def test_the_transcript_the_evals_read_is_untouched(tmp_path: Path):
    """`evals/harness.py::_turn_telemetry` counts rounds and tool calls out of
    `.shamsu/sessions/*/messages.jsonl`. The logging layout may change; that
    file is the only measurement of what a case COST and must not."""
    import json

    from evals.harness import _turn_telemetry

    logger = SessionManager(tmp_path).create_session()
    session_dir = tmp_path / ".shamsu" / "sessions" / logger.session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "messages.jsonl").write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"role": "user", "content": "go"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}, {"id": "2"}]},
                {"role": "assistant", "content": "done"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _finished_run(tmp_path)   # writes the new logs into the same workspace

    assert _turn_telemetry(tmp_path) == (2, 2)


# -- Defects the first live run exposed -------------------------------------


def test_an_approval_is_one_row_not_two(tmp_path: Path):
    """Live 2026-08-21: three edits produced six approval rows, alternating
    "waiting" and "approved". The request and the answer are one event to a
    reader, and saying it twice made the log twice as long and no clearer."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "edit the config")
    request = {"action_type": "file_edit", "target_paths": ["calc.py"]}
    ledger.log_event("approval_request", request=request)
    ledger.log_event(
        "approval_granted",
        action_type="file_edit",
        approved=True,
        decision_source="approval_callback",
        request=request,
    )
    ledger.finish("Edited.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    assert summary.count("**Approval**") == 1
    assert "**approved**" in summary
    assert "**waiting**" not in summary


def test_an_unanswered_approval_still_gets_a_row(tmp_path: Path):
    """Holding the request must not mean losing it. A turn that died waiting is
    exactly the turn you open the log to understand."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "edit the config")
    ledger.log_event(
        "approval_request",
        request={"action_type": "file_delete", "target_paths": ["app.py"]},
    )
    ledger.finish("Gave up.", status="cancelled")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    assert "**Approval**" in summary
    assert "**waiting**" in summary


def test_a_result_split_from_its_call_names_its_own_tool(tmp_path: Path):
    """Live 2026-08-21: an approval fires from inside `patch_file` and lands
    between the call and its result, so the indented result line read as the
    approval's outcome. When anything came between, the result gets a row that
    says which tool it belongs to."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "patch it")
    call_id = ledger.log_tool_call("patch_file", {"filepath": "calc.py"})
    ledger.log_event(
        "approval_granted",
        action_type="file_edit",
        approved=True,
        request={"action_type": "file_edit", "target_paths": ["calc.py"]},
    )
    ledger.log_tool_result(call_id, "patch_file", True, "Edited calc.py: +2 -0 lines.")
    ledger.finish("Patched.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    result_line = next(
        line for line in summary.splitlines() if "Edited calc.py" in line
    )
    assert not result_line.startswith("    "), "still indented under the approval"
    assert "patch_file" in result_line


def test_an_uninterrupted_result_stays_indented_under_its_call(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "read it")
    call_id = ledger.log_tool_call("read_file", {"filepath": "calc.py"})
    ledger.log_tool_result(call_id, "read_file", True, "Read file.")
    ledger.finish("Read.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    result_line = next(line for line in summary.splitlines() if "Read file." in line)
    assert result_line.startswith("    - ok")


def test_a_summary_row_stays_one_line_however_long_the_result(tmp_path: Path):
    """`patch_file` answers with a whole diff. That belongs in the detail file;
    the summary gets one line of it."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "patch it")
    diff = "Edited calc.py.\n" + "\n".join(f"+ line {index}" for index in range(60))
    call_id = ledger.log_tool_call("patch_file", {"filepath": "calc.py"})
    ledger.log_tool_result(call_id, "patch_file", True, diff)
    ledger.finish("Patched.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    row = next(line for line in summary.splitlines() if "Edited calc.py" in line)
    assert len(row) < 400
    # ...and the whole diff is still recoverable next door.
    assert "+ line 59" in ledger.detail_log_path.read_text(encoding="utf-8")


def test_simple_mode_records_its_tools_and_model_calls(tmp_path: Path):
    """The session log is built from the ActionLedger on the promise that every
    execution path records there. Simple mode - the DEFAULT path - never did, so
    the first live run of this refactor produced a log with approvals and file
    writes in it and no sign of what the agent read, ran, or said."""
    import asyncio

    from shamsu.action_ledger.ledger import start_run
    from shamsu.agents.chat_state import ChatState
    from shamsu.agents.simple_chat import SimpleChatLoop
    from shamsu.agents.simple_prompt import simple_system_prompt
    from shamsu.tools.agent_tools import AgentToolRegistry
    from tests.test_simple_chat import FakeClient, _text

    (tmp_path / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    logger = SessionManager(tmp_path).create_session()
    ledger = start_run(tmp_path, "look at calc.py", session_logger=logger)
    loop = SimpleChatLoop(
        tmp_path,
        client=FakeClient(
            [
                {
                    "message": {
                        "content": "",
                        "thinking": "I should read it before saying anything.",
                        "tool_calls": [
                            {"function": {"name": "read_file", "arguments": {"filepath": "calc.py"}}}
                        ],
                    }
                },
                _text("calc.py defines add()."),
            ]
        ),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        state=ChatState(simple_system_prompt(tmp_path), hydrate=False),
        model_name="qwen3:8b",
        action_ledger=ledger,
        log_turns=False,
    )
    asyncio.run(loop.run("look at calc.py"))

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    detail = ledger.detail_log_path.read_text(encoding="utf-8")
    assert "Model responded" in summary        # the model's turns
    assert "Reading `calc.py`" in summary      # the tool it used
    assert "def add(a, b)" in detail           # and what came back
    # A thinking model's trace reaches the log through the same path.
    assert "I should read it before saying anything." in detail


def test_anchors_stay_unique_across_turns_and_processes(tmp_path: Path):
    """The anchor counter is seeded from the file and then carried in memory.
    Seeding is what keeps a second turn - or a second process - from writing an
    id the document already uses, which would be a summary link that jumps to
    the wrong row."""
    import re

    from shamsu.action_ledger.context import clear_current_run, set_current_run
    from shamsu.action_ledger.ledger import start_run
    from shamsu.cli.request_lifecycle import finish_current_run

    logger = SessionManager(tmp_path).create_session()
    for prompt in ("first", "second", "third"):
        ledger = start_run(tmp_path, prompt, session_logger=logger)
        set_current_run(ledger)
        try:
            for name in ("read_file", "write_file"):
                call_id = ledger.log_tool_call(name, {"filepath": f"{prompt}.py"})
                ledger.log_tool_result(call_id, name, True, f"did {name}")
            ledger.record_final_response(f"done {prompt}")
            finish_current_run(tmp_path, ledger)
        finally:
            clear_current_run()

    detail = (
        tmp_path / ".shamsu" / "sessions" / logger.session_id / "log-detailed.md"
    ).read_text(encoding="utf-8")
    anchors = re.findall(r'<a id="(doc-[a-z0-9_-]+)"></a>', detail)
    assert len(anchors) >= 12
    assert len(set(anchors)) == len(anchors), "an anchor id was written twice"


def test_two_attachments_in_one_block_do_not_collide(tmp_path: Path):
    """A tool result with both a large message and a large data payload spills
    twice from the same detail block. Sharing a filename would mean the second
    silently overwrote the first."""
    from shamsu.action_ledger.ledger import start_run
    from shamsu.ui.turnlog import OVERFLOW_CHARS

    ledger = start_run(tmp_path, "read a lot")
    message = "m" * (OVERFLOW_CHARS + 50)
    data = {"rows": ["d" * 80 for _ in range(60)]}
    call_id = ledger.log_tool_call("read_file", {"filepath": "big.py"})
    ledger.log_tool_result(call_id, "read_file", True, message, data)
    ledger.finish("Read.", status="success")

    spilled = sorted(path.name for path in ledger.log_attachments_dir.glob("*"))
    assert len(spilled) == 2, f"expected two attachments, got {spilled}"
    assert len(set(spilled)) == 2
    bodies = [
        (ledger.log_attachments_dir / name).read_text(encoding="utf-8")
        for name in spilled
    ]
    assert any(body == message for body in bodies)
