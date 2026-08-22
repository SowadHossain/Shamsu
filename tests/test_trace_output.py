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
    assert "Added a /health endpoint." in summary         # the answer
    assert "success" in summary
    # Titles-only: what the tool SAID is a click away, not on the row.
    assert "Wrote 12 lines" not in summary
    assert "Wrote 12 lines" in ledger.detail_log_path.read_text(encoding="utf-8")


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
    detail = ledger.detail_log_path.read_text(encoding="utf-8")
    assert "hello.py" in summary
    assert "python hello.py" in summary
    assert "FAILED" in summary                # the failed call is marked
    assert "Created hello.py (+1 lines)." in detail


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
    # One action, one row - the call and its result are the same line.
    assert summary.count("app.py`") == 1
    assert ledger.detail_log_path.read_text(encoding="utf-8").count("Wrote app.py") == 1


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
    assert "Context sent to model" in detail
    assert "stdout" in detail and "one passed" in detail
    assert "stderr" in detail and "one failed" in detail


def test_essential_keeps_the_record_and_verbose_adds_the_context_pack(tmp_path: Path):
    """Where the line between the two levels sits, and why it moved twice.

    With one document, `essential` withheld the model's own words to stay
    readable. With two, the summary is skimmable on its own, so the response
    and the reasoning trace moved to both levels - they are what you debug a
    small model with.

    The prompt joined them when `.shamsu/chat-logs/` was deleted. That file had
    been keeping a full copy at every level, unredacted, and removing it for
    the leak would otherwise have taken the record with it. So the prompt is
    kept here, where it goes through the redactor, and the overflow rule keeps
    a large one out of the document body.

    What `verbose` still adds is the context pack: the single largest payload a
    turn produces, and the one thing genuinely reconstructable from elsewhere.
    """
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "answer concisely")
    call_id = ledger.log_model_call_started("qa", "m", "the prompt that was sent")
    ledger.log_context_preview({"task_id": "t1", "token_estimate": 99, "snippets": []})
    ledger.log_model_thinking(call_id, "qa", "m", "the reasoning trace")
    ledger.log_model_call_finished("qa", "m", "the model response", call_id=call_id)
    ledger.finish("Public answer.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    detail = ledger.detail_log_path.read_text(encoding="utf-8")

    assert "Public answer." in summary
    assert "the prompt that was sent" in detail
    assert "the model response" in detail
    assert "the reasoning trace" in detail
    # The pack itself is verbose-only; the fact that one was built is a row.
    assert "Building context" in summary
    assert "Context sent to model" not in detail


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

    assert "via telegram" in ledger.summary_log_path.read_text(encoding="utf-8")


def test_a_local_turn_is_badged_cli_by_default(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "fix it")
    ledger.finish("Fixed.", status="success")

    assert "via cli" in ledger.summary_log_path.read_text(encoding="utf-8")


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


def test_a_call_and_its_result_are_one_row(tmp_path: Path):
    """`log-summary.md` is titles-only: one line per ACTION, and a call plus its
    result is one action. Two lines per tool made a 24-round turn a 50-row wall
    with half the rows quoting diffs."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "read it")
    call_id = ledger.log_tool_call("read_file", {"filepath": "calc.py"})
    ledger.log_tool_result(call_id, "read_file", True, "Read file.")
    ledger.finish("Read.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    rows = [line for line in summary.splitlines() if "calc.py" in line]
    assert len(rows) == 1, rows
    assert rows[0].startswith("- ")            # flush left; nothing is indented
    assert "Read file." not in summary         # the result text is next door
    assert "Read file." in ledger.detail_log_path.read_text(encoding="utf-8")


def test_a_failed_tool_is_marked_on_its_row(tmp_path: Path):
    """The one outcome worth a word in a titles-only summary. A live 3B run
    called `contract_assert_pass` seven times and was refused every time; with
    no marker those are seven identical rows."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "assert it")
    call_id = ledger.log_tool_call("contract_assert_pass", {"assertion_id": "a01"})
    ledger.log_tool_result(call_id, "contract_assert_pass", False, "a01 needs evidence")
    ledger.finish("Stopped.", status="failed")

    row = next(
        line
        for line in ledger.summary_log_path.read_text(encoding="utf-8").splitlines()
        if "contract_assert_pass" in line
    )
    assert "FAILED" in row
    assert "needs evidence" not in row         # marked, not quoted


def test_an_interrupted_call_keeps_the_order_honest(tmp_path: Path):
    """An approval fires from inside `patch_file` and lands between the call and
    its result. The call is flushed first so the rows stay in the order they
    happened, and the result then names its own tool rather than looking like
    the approval's outcome."""
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

    lines = ledger.summary_log_path.read_text(encoding="utf-8").splitlines()
    call_at = next(i for i, line in enumerate(lines) if "Editing `calc.py`" in line)
    approval_at = next(i for i, line in enumerate(lines) if "**Approval**" in line)
    result_at = next(i for i, line in enumerate(lines) if "result" in line.lower())
    assert call_at < approval_at < result_at
    assert not lines[result_at].startswith("    ")


def test_a_summary_row_never_quotes_the_payload(tmp_path: Path):
    """`patch_file` answers with a whole diff. That belongs in the detail file;
    the summary row says which file was edited and nothing else."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "patch it")
    diff = "Edited calc.py.\n" + "\n".join(f"+ line {index}" for index in range(60))
    call_id = ledger.log_tool_call("patch_file", {"filepath": "calc.py"})
    ledger.log_tool_result(call_id, "patch_file", True, diff)
    ledger.finish("Patched.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    row = next(line for line in summary.splitlines() if "calc.py" in line)
    assert len(row) < 200
    assert "+ line 0" not in summary
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
    assert len(anchors) >= 6
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


# -- Aligned to the Turn Log Viewer mockup ----------------------------------


def test_building_context_is_a_row_but_the_pack_is_verbose_only(tmp_path: Path):
    """The mockup lists "Building context" in the summary and the pack itself
    under log-detailed.md only. What was retrieved and what it cost is part of
    the turn; the pack is the largest payload a turn produces."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "why is it slow")
    ledger.log_context_preview(
        {"task_id": "t1", "token_estimate": 1180, "snippets": [{"path": "a.py"}]}
    )
    ledger.finish("Because of the loop.", status="success")

    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    detail = ledger.detail_log_path.read_text(encoding="utf-8")
    assert "Building context" in summary
    assert "1180 tokens" in summary or "~1180" in summary
    assert "Context sent to model" not in detail   # essential mode


def test_a_system_notice_is_a_row_in_both_files(tmp_path: Path):
    """"context is filling" explains why the rows after it look different, so
    it is never collapsed and never a link - it belongs in both documents."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "long turn")
    ledger.log_notice("context is filling; eliding older tool payloads")
    ledger.finish("Done.", status="success")

    for path in (ledger.summary_log_path, ledger.detail_log_path):
        text = path.read_text(encoding="utf-8")
        assert "context is filling; eliding older tool payloads" in text
        assert "log-detailed.md#" not in text.split("context is filling")[1][:80]


def test_the_verdict_carries_its_one_line_reason(tmp_path: Path):
    """"success" alone does not separate a turn that changed two files and
    verified them from one that answered a question."""
    from shamsu.action_ledger.ledger import verdict_reason

    assert verdict_reason({"changed_files": ["a.py", "b.py"]}) == "2 files changed"
    assert (
        verdict_reason(
            {
                "changed_files": ["a.py"],
                "verification": [{"type": "verification_passed"}],
            }
        )
        == "1 file changed, checks passed"
    )
    assert (
        verdict_reason(
            {
                "changed_files": ["a.py"],
                "verification": [{"type": "verification_failed"}],
            }
        )
        == "1 file changed, 1 check failed"
    )
    assert verdict_reason({"tools": [{"tool": "read_file"}]}) == (
        "1 tool call, nothing changed"
    )
    assert verdict_reason({}) == ""
    # A turn that wrote nothing does not get to report "checks passed" as its
    # whole story - live 2026-08-21 that produced "Verdict: failed - checks
    # passed", where the check was a syntax verdict on a file it only read.
    assert verdict_reason(
        {"tools": [{"tool": "read_file"}], "verification": [{"type": "verification_passed"}]}
    ) == "1 tool call, nothing changed"


def test_the_final_output_is_never_behind_a_link(tmp_path: Path):
    """The mockup marks the prompt, the verdict and the agent's final output as
    always visible in both files. A log of a conversation that makes you click
    for the reply is not a log of the conversation."""
    ledger, _logger = _finished_run(tmp_path)

    for path in (ledger.summary_log_path, ledger.detail_log_path):
        text = path.read_text(encoding="utf-8")
        assert "add a healthcheck endpoint" in text      # the prompt
        assert "Verdict:" in text                        # the verdict
        assert "Added a /health endpoint." in text       # the reply


def test_a_self_executed_write_reaches_changed_files(tmp_path: Path):
    """`replace_symbol` and `append_file` are run by the loop itself rather than
    the registry, so nothing journalled the file they changed. Live 2026-08-21:
    calc.py was correctly fixed and the run closed `failed` with
    `changed_files: []`, which also blinded `evidence_outcome`."""
    import asyncio

    from shamsu.action_ledger import store
    from shamsu.action_ledger.ledger import start_run
    from shamsu.agents.chat_state import ChatState
    from shamsu.agents.simple_chat import SimpleChatLoop
    from shamsu.agents.simple_prompt import simple_system_prompt
    from shamsu.tools.agent_tools import AgentToolRegistry
    from tests.test_simple_chat import FakeClient, _text

    (tmp_path / "calc.py").write_text(
        "def divide(a, b):\n    return a / b\n", encoding="utf-8"
    )
    logger = SessionManager(tmp_path).create_session()
    ledger = start_run(tmp_path, "guard the divisor", session_logger=logger)
    loop = SimpleChatLoop(
        tmp_path,
        client=FakeClient(
            [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "replace_symbol",
                                    "arguments": {
                                        "filepath": "calc.py",
                                        "symbol": "divide",
                                        "content": (
                                            "def divide(a, b):\n"
                                            "    if b == 0:\n"
                                            "        raise ValueError('no')\n"
                                            "    return a / b\n"
                                        ),
                                    },
                                }
                            }
                        ],
                    }
                },
                _text("Guarded."),
            ]
        ),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        state=ChatState(simple_system_prompt(tmp_path), hydrate=False),
        model_name="qwen3:8b",
        action_ledger=ledger,
    )
    asyncio.run(loop.run("guard the divisor"))
    ledger.finish("Guarded.", status="success")

    assert "ValueError" in (tmp_path / "calc.py").read_text(encoding="utf-8")
    mutations = store.load_mutations(tmp_path, ledger.run_id)
    assert [m["status"] for m in mutations] == ["applied"]
    assert mutations[0]["touched_files"] == ["calc.py"]
    summary = store.load_summary(tmp_path, ledger.run_id) or {}
    assert summary.get("changed_files") == ["calc.py"]
    assert "calc.py" in ledger.summary_log_path.read_text(encoding="utf-8")


def test_a_registry_write_is_not_journalled_twice(tmp_path: Path):
    """The registry already records its own mutation, with real hashes and a
    rollback. A second record would double-count the file everywhere."""
    import asyncio

    from shamsu.action_ledger import store
    from shamsu.action_ledger.ledger import start_run
    from shamsu.agents.chat_state import ChatState
    from shamsu.agents.simple_chat import SimpleChatLoop
    from shamsu.agents.simple_prompt import simple_system_prompt
    from shamsu.tools.agent_tools import AgentToolRegistry
    from tests.test_simple_chat import FakeClient, _text

    logger = SessionManager(tmp_path).create_session()
    ledger = start_run(tmp_path, "write a file", session_logger=logger)
    loop = SimpleChatLoop(
        tmp_path,
        client=FakeClient(
            [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "write_file",
                                    "arguments": {
                                        "filepath": "app.py",
                                        "content": "print('hi')\n",
                                    },
                                }
                            }
                        ],
                    }
                },
                _text("Written."),
            ]
        ),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True, action_ledger=ledger),
        state=ChatState(simple_system_prompt(tmp_path), hydrate=False),
        model_name="qwen3:8b",
        action_ledger=ledger,
    )
    asyncio.run(loop.run("write a file"))
    ledger.finish("Written.", status="success")

    summary = store.load_summary(tmp_path, ledger.run_id) or {}
    assert summary.get("changed_files") == ["app.py"], summary.get("changed_files")


# -- what `.shamsu/chat-logs/` used to guarantee, now guaranteed here --------
#
# `shamsu/agents/simple_log.py` wrote the exact prompt and the raw reply to
# `.shamsu/chat-logs/<session>.md`. It contained zero calls to `redact`, so it
# was the one path in the project that put a model prompt on disk without going
# through the single secret-pattern enforcement point. It was deleted. These
# are the properties it held that the redacted log has to hold in its place.


def _turn_with(tmp_path: Path, prompt: str, reply, thinking: str = ""):
    """Run one scripted turn and return its ledger."""
    import asyncio

    from shamsu.action_ledger.ledger import start_run
    from shamsu.agents.chat_state import ChatState
    from shamsu.agents.simple_chat import SimpleChatLoop
    from shamsu.agents.simple_prompt import simple_system_prompt
    from shamsu.tools.agent_tools import AgentToolRegistry
    from tests.test_simple_chat import FakeClient

    logger = SessionManager(tmp_path).create_session()
    ledger = start_run(tmp_path, prompt, session_logger=logger)
    loop = SimpleChatLoop(
        tmp_path,
        client=FakeClient([reply]),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        state=ChatState(simple_system_prompt(tmp_path), hydrate=False),
        session_logger=logger,
        action_ledger=ledger,
        model_name="m",
    )
    asyncio.run(loop.run(prompt))
    ledger.finish("done", status="success")
    return ledger


def test_no_secret_in_the_prompt_reaches_any_file_on_disk(tmp_path: Path):
    """The whole reason `chat-logs/` was deleted, asserted over the WHOLE tree.

    Not against a named file: the leak was found by searching every file under
    `.shamsu/`, and a test that only checks the files we remembered to name
    would have missed both the one we knew about and the `model-transcript.csv`
    we did not.
    """
    secret = "sk-livesecret9876"
    _turn_with(
        tmp_path,
        f"deploy with api_key={secret}",
        {"message": {"content": f"I will use api_key={secret}.", "tool_calls": []}},
    )

    leaked = []
    for path in sorted(tmp_path.rglob("*")):
        if not path.is_file():
            continue
        try:
            if secret in path.read_text(encoding="utf-8", errors="ignore"):
                leaked.append(str(path.relative_to(tmp_path)))
        except OSError:
            continue
    assert leaked == [], f"the secret reached disk: {leaked}"


def test_the_prompt_as_sent_is_kept_at_every_level(tmp_path: Path):
    """`chat-logs/` kept the full prompt whatever the log level was, so this
    has to as well - otherwise deleting it for the leak took the record with
    it. Redacted, unlike its predecessor."""
    ledger = _turn_with(
        tmp_path, "make hello.py",
        {"message": {"content": "here you go", "tool_calls": []}},
    )
    detail = ledger.detail_log_path.read_text(encoding="utf-8")

    assert "You are SHAMSU" in detail, "the system prompt must be visible"
    assert "make hello.py" in detail
    assert "here you go" in detail, "the raw response must be visible"


def test_a_pydantic_response_is_not_recorded_as_empty(tmp_path: Path):
    """The client returns a ChatResponse object, not a dict. Reading it with
    `.get()` logged every response as empty while text was plainly produced -
    the bug the deleted module was once fixed for."""

    class Message:
        content = "I created the file."
        thinking = "first I should write it"
        tool_calls: list = []

    class ChatResponse:
        message = Message()

    ledger = _turn_with(tmp_path, "make it", ChatResponse())
    detail = ledger.detail_log_path.read_text(encoding="utf-8")

    assert "I created the file." in detail
    assert "first I should write it" in detail, "the thinking channel is where the time goes"


def test_a_whole_tool_result_survives_rather_than_being_clipped(tmp_path: Path):
    """`chat-logs/` set its truncation limit to zero on purpose: a record that
    clips is not a record. The overflow rule replaces that - the payload leaves
    the document and keeps every byte in `attachments/`."""
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "read the log")
    body = "\n".join(f"line {index} of a very long tool result" for index in range(400))
    call_id = ledger.log_tool_call("read_file", {"filepath": "big.log"})
    ledger.log_tool_result(call_id, "read_file", True, body)
    ledger.finish("read", status="success")

    spilled = sorted(ledger.log_attachments_dir.glob("*"))
    assert spilled, "the oversized result was not kept anywhere"
    kept = spilled[0].read_text(encoding="utf-8")
    assert "line 0 of" in kept and "line 399 of" in kept, "the record was clipped"


def test_a_broken_log_never_breaks_the_turn(tmp_path: Path):
    """Held by the deleted module and still required: logging is observability,
    never a reason to lose a turn."""
    from shamsu.ui.turnlog import TurnLogWriter

    writer = TurnLogWriter(tmp_path / "gone" / "deeper", run_id="r", turn_id="t")
    writer.home.mkdir(parents=True, exist_ok=True)
    writer.summary_path.write_text("", encoding="utf-8")
    # Make the destination unwritable by turning it into a directory.
    writer.summary_path.unlink()
    writer.summary_path.mkdir()

    writer.open_turn("hi")                      # must not raise
    writer.append_tool_call("read_file", {"filepath": "a.py"})
    writer.append_tool_result("read_file", True, "ok")
    writer.close_turn("done", "success")


def test_the_legacy_chat_logs_directory_is_no_longer_written(tmp_path: Path):
    _turn_with(
        tmp_path, "make hello.py",
        {"message": {"content": "here you go", "tool_calls": []}},
    )
    assert not (tmp_path / ".shamsu" / "chat-logs").exists()


def test_a_workspace_with_old_chat_logs_is_warned(tmp_path: Path):
    from shamsu.ui.turnlog import legacy_chat_logs, legacy_chat_logs_warning

    old = tmp_path / ".shamsu" / "chat-logs"
    old.mkdir(parents=True)
    (old / "20260818-1200-a1b2--game.md").write_text("api_key=sk-live", encoding="utf-8")

    assert legacy_chat_logs(tmp_path) == old
    warning = legacy_chat_logs_warning(tmp_path)
    assert "chat-logs" in warning
    assert "did NOT" in warning and "redact" in warning
    assert "1 file" in warning
    assert "Delete the folder" in warning


def test_a_clean_workspace_is_not_warned(tmp_path: Path):
    from shamsu.ui.turnlog import legacy_chat_logs, legacy_chat_logs_warning

    assert legacy_chat_logs(tmp_path) is None
    assert legacy_chat_logs_warning(tmp_path) == ""

    # An empty leftover folder is not a leak and is not worth a panel.
    (tmp_path / ".shamsu" / "chat-logs").mkdir(parents=True)
    assert legacy_chat_logs(tmp_path) is None
    assert legacy_chat_logs_warning(tmp_path) == ""


def test_the_warning_never_deletes_anything(tmp_path: Path):
    """The files may be the only record of sessions someone still wants, and
    silently removing a user's logs to fix our bug is not ours to decide."""
    from shamsu.ui.turnlog import legacy_chat_logs_warning

    old = tmp_path / ".shamsu" / "chat-logs"
    old.mkdir(parents=True)
    kept = old / "thread.md"
    kept.write_text("a whole conversation", encoding="utf-8")

    legacy_chat_logs_warning(tmp_path)

    assert kept.is_file()
    assert kept.read_text(encoding="utf-8") == "a whole conversation"


def test_the_startup_banner_shows_the_warning(tmp_path: Path):
    from rich.console import Console

    from shamsu.cli.repl import _warn_about_legacy_chat_logs

    old = tmp_path / ".shamsu" / "chat-logs"
    old.mkdir(parents=True)
    (old / "thread.md").write_text("secrets", encoding="utf-8")

    console = Console(record=True, width=100)
    _warn_about_legacy_chat_logs(tmp_path, console)
    printed = console.export_text()
    assert "Unredacted logs" in printed
    assert "chat-logs" in printed

    # ...and says nothing at all about a workspace that has none.
    clean = Console(record=True, width=100)
    _warn_about_legacy_chat_logs(tmp_path / "elsewhere", clean)
    assert clean.export_text().strip() == ""


# -- migrating the unredacted chat-logs forward -----------------------------
#
# `.shamsu/chat-logs/` was written by a logger with no calls to `redact`. The
# writer was deleted; the files were not, and they are also the only readable
# record of every session that ran before the session log existed. These
# replay them through TurnLogWriter, which is what makes the copy redacted.

SECRET = "sk-proj1234567890abcdefghijklmnopqrstuvwxyz"


def _legacy(body: str) -> str:
    return (
        "# Session 20260101-000000\n\n"
        "**model** `qwen3.5:9b` - started 2026-01-01 00:00:00\n\n"
        "Every turn of this thread, in order.\n\n---\n\n" + body
    )


def _turn(number: int, when: str, prompt: str, body: str, final: str) -> str:
    return (
        f"# Turn {number} - {when}\n\n"
        f"## What you asked\n\n```\n{prompt}\n```\n\n---\n\n"
        f"{body}\n"
        f"## Final answer\n\n```\n{final}\n```\n\n*1 round(s), 0.1s*\n"
    )


def _write_legacy(workspace: Path, name: str, text: str) -> Path:
    folder = workspace / ".shamsu" / "chat-logs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_heading_inside_the_models_reply_does_not_split_the_turn(tmp_path: Path):
    """The real files contain `## Project Review Summary`, `# Original content:`
    and `### **Priority Fixes Needed**` - all written BY the model, inside its
    own reply. A parser that treats any `#` as structure cuts the turn in half
    at the model's prose."""
    from shamsu.ui.chatlog_migrate import parse_chat_log

    reply = (
        "## Project Review Summary\n\n"
        "### **Critical Issues Found**\n\n"
        "# Original content:\n\n"
        "All three are fine."
    )
    text = _legacy(
        _turn(
            1, "2026-01-01 00:00:00", "review it",
            f"## Round 1 - raw response  (1.0s)\n\n**content**\n\n```\n{reply}\n```\n",
            "reviewed",
        )
    )
    parsed = parse_chat_log(text)

    assert len(parsed.turns) == 1, "the model's own headings split the turn"
    assert parsed.turns[0].rounds[0].content == reply
    assert parsed.turns[0].final == "reviewed"


def test_a_fence_that_grew_to_survive_backticks_is_read_whole(tmp_path: Path):
    """The old writer lengthened its fence until the body no longer contained
    it, so a four-backtick block can hold a three-backtick one."""
    from shamsu.ui.chatlog_migrate import parse_chat_log

    inner = "here is code:\n```js\nconst x = 1;\n```\ndone"
    text = _legacy(
        _turn(
            1, "2026-01-01 00:00:00", "explain",
            f"## Round 1 - raw response  (1.0s)\n\n**content**\n\n````\n{inner}\n````\n",
            "explained",
        )
    )
    parsed = parse_chat_log(text)

    assert parsed.turns[0].rounds[0].content == inner
    assert "const x = 1;" in parsed.turns[0].rounds[0].content


def test_a_tool_is_not_counted_twice(tmp_path: Path):
    """The same call appears in `tool calls requested` AND in its own result
    section. Replaying both printed every tool twice."""
    from shamsu.ui.chatlog_migrate import parse_chat_log

    body = (
        "## Round 1 - raw response  (1.0s)\n\n"
        "**content**\n\n```\n*(empty)*\n```\n\n"
        "**tool calls requested**\n\n```json\n"
        '[{"function": {"name": "read_file", "arguments": {"filepath": "a.py"}}}]\n```\n\n'
        "### tool `read_file` -> ok\n\n"
        '*arguments*\n\n```json\n{"filepath": "a.py"}\n```\n\n'
        "*result*\n\n```\nx = 1\n```\n"
    )
    parsed = parse_chat_log(_legacy(_turn(1, "w", "read it", body, "read")))
    round_ = parsed.turns[0].rounds[0]

    assert len(round_.tools) == 1
    assert len(round_.replayable()) == 1
    assert round_.replayable()[0]["name"] == "read_file"


def test_a_call_that_never_returned_is_still_replayed(tmp_path: Path):
    """A request with no result section is a turn that died mid-round, which is
    exactly the shape worth keeping."""
    from shamsu.ui.chatlog_migrate import parse_chat_log

    body = (
        "## Round 1 - raw response  (1.0s)\n\n"
        "**tool calls requested**\n\n```json\n"
        '[{"function": {"name": "patch_file", "arguments": {"filepath": "a.py"}}}]\n```\n'
    )
    parsed = parse_chat_log(_legacy(_turn(1, "w", "patch it", body, "")))
    round_ = parsed.turns[0].rounds[0]

    assert round_.tools == []
    assert [t["name"] for t in round_.replayable()] == ["patch_file"]


def test_the_migrated_log_is_redacted(tmp_path: Path):
    """The whole point. The source keeps its secret; the copy must not."""
    from shamsu.ui.chatlog_migrate import migrate_workspace

    body = (
        "## Round 1 - prompt sent to the model\n\n### [1] user\n\n"
        f"```\ndeploy with api_key={SECRET}\n```\n\n"
        "## Round 1 - raw response  (1.0s)\n\n**content**\n\n"
        f"```\nI will use {SECRET} now.\n```\n\n"
        "### tool `run_command` -> ok\n\n*result*\n\n"
        f"```\nexported API_KEY={SECRET}\n```\n"
    )
    source = _write_legacy(
        tmp_path, "20260101-000000.md",
        _legacy(_turn(1, "2026-01-01 00:00:00", f"deploy with api_key={SECRET}", body, "done")),
    )

    results = migrate_workspace(tmp_path)
    assert [r.ok for r in results] == [True]

    leaked = [
        str(path.relative_to(tmp_path))
        for path in (tmp_path / ".shamsu" / "sessions").rglob("*")
        if path.is_file() and SECRET in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == [], f"the migrated copy still leaks: {leaked}"

    detail = next((tmp_path / ".shamsu" / "sessions").rglob("log-detailed.md"))
    body_text = detail.read_text(encoding="utf-8")
    assert "[REDACTED]" in body_text
    # The surrounding words survive - it is a redaction, not a deletion.
    assert "I will use" in body_text
    # ...and the ORIGINAL is untouched, because removing it is the user's call.
    assert SECRET in source.read_text(encoding="utf-8")


def test_one_conversation_split_over_two_files_keeps_both_and_their_order(tmp_path: Path):
    """The old writer opened a new file when a session gained a title, so
    `test1` has two files for one session holding turn 1 and turn 2. Migrating
    them independently dropped one and reversed the other."""
    from shamsu.ui.chatlog_migrate import migrate_workspace

    # Named so the alphabetical order is the WRONG order.
    _write_legacy(
        tmp_path, "20260101-000000--aaa-later-title.md",
        _legacy(_turn(2, "2026-01-01 01:00:00", "second thing", "", "did the second")),
    )
    _write_legacy(
        tmp_path, "20260101-000000--zzz-untitled-session.md",
        _legacy(_turn(1, "2026-01-01 00:00:00", "first thing", "", "did the first")),
    )

    results = migrate_workspace(tmp_path)
    assert all(r.ok for r in results), [r.skipped or r.error for r in results]

    summary = (
        tmp_path / ".shamsu" / "sessions" / "20260101-000000" / "log-summary.md"
    ).read_text(encoding="utf-8")
    assert "first thing" in summary and "second thing" in summary
    assert summary.index("first thing") < summary.index("second thing")
    assert summary.count("<!-- turn-closed -->") == 2


def test_the_original_timestamp_is_kept_not_todays_date(tmp_path: Path):
    """A migrated turn stamped with today's date would make the document lie
    about the one thing it is ordered by."""
    from shamsu.ui.chatlog_migrate import migrate_workspace

    _write_legacy(
        tmp_path, "20260101-000000.md",
        _legacy(_turn(1, "2026-01-01 00:00:00", "do it", "", "done")),
    )
    migrate_workspace(tmp_path)

    summary = (
        tmp_path / ".shamsu" / "sessions" / "20260101-000000" / "log-summary.md"
    ).read_text(encoding="utf-8")
    assert "2026-01-01 00:00:00" in summary


def test_a_session_that_already_has_a_log_is_left_alone(tmp_path: Path):
    """The live log is authoritative. Interleaving a replayed history into a
    document being appended to would put turns out of order."""
    from shamsu.ui.chatlog_migrate import migrate_workspace

    session = tmp_path / ".shamsu" / "sessions" / "20260101-000000"
    session.mkdir(parents=True)
    (session / "log-summary.md").write_text("# the live log\n", encoding="utf-8")
    _write_legacy(
        tmp_path, "20260101-000000.md",
        _legacy(_turn(1, "w", "do it", "", "done")),
    )

    results = migrate_workspace(tmp_path)
    assert results[0].skipped == "already has a session log"
    assert (session / "log-summary.md").read_text(encoding="utf-8") == "# the live log\n"


def test_a_session_with_no_directory_still_gets_one(tmp_path: Path):
    """14 of the 21 real files are probe runs that never registered a session.
    The alternative is leaving their only record in the folder we are telling
    people to delete."""
    from shamsu.ui.chatlog_migrate import migrate_workspace

    _write_legacy(
        tmp_path, "20260101-000000.md",
        _legacy(_turn(1, "w", "probe", "", "done")),
    )
    results = migrate_workspace(tmp_path)

    assert results[0].ok
    assert (tmp_path / ".shamsu" / "sessions" / "20260101-000000" / "log-summary.md").is_file()


def test_the_pointer_file_is_not_treated_as_a_transcript(tmp_path: Path):
    from shamsu.ui.chatlog_migrate import legacy_logs

    _write_legacy(tmp_path, "latest.md", "20260101-000000\n")
    _write_legacy(tmp_path, "20260101-000000.md", _legacy(_turn(1, "w", "x", "", "y")))

    assert [p.name for p in legacy_logs(tmp_path)] == ["20260101-000000.md"]


def test_migration_never_deletes_anything(tmp_path: Path):
    from shamsu.ui.chatlog_migrate import migrate_workspace

    source = _write_legacy(
        tmp_path, "20260101-000000.md",
        _legacy(_turn(1, "w", "do it", "", "done")),
    )
    before = source.read_text(encoding="utf-8")
    migrate_workspace(tmp_path)

    assert source.is_file()
    assert source.read_text(encoding="utf-8") == before


def test_a_workspace_with_nothing_to_migrate_is_quiet(tmp_path: Path):
    from shamsu.ui.chatlog_migrate import legacy_logs, migrate_workspace

    assert legacy_logs(tmp_path) == []
    assert migrate_workspace(tmp_path) == []
