from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shamsu.action_ledger.ids import new_run_id
from shamsu.action_ledger.ledger import ActionLedger, start_run
from shamsu.action_ledger import store


def _events(ledger: ActionLedger) -> list[dict]:
    return [json.loads(line) for line in ledger.events_path.read_text(encoding="utf-8").splitlines()]


# -- storage ------------------------------------------------------------------


def test_start_run_creates_run_directory(tmp_path: Path):
    ledger = start_run(tmp_path, "fix the bug")

    assert ledger.run_dir == tmp_path / ".shamsu" / "runs" / ledger.run_id
    assert ledger.run_dir.is_dir()


def test_start_run_creates_complete_canonical_artifact_layout(tmp_path: Path):
    ledger = start_run(tmp_path, "answer a read-only question")

    expected_files = (
        ledger.events_path,
        ledger.decisions_path,
        ledger.tool_calls_path,
        ledger.model_calls_path,
        ledger.mutations_dir / "mutations.jsonl",
        ledger.context_preview_path,
    )
    assert all(path.is_file() for path in expected_files)
    assert ledger.commands_dir.is_dir()
    assert ledger.diagnostics_dir.is_dir()
    assert ledger.contexts_dir.is_dir()
    assert store.load_context_records(tmp_path, ledger.run_id) == []


def test_start_run_writes_manifest_json(tmp_path: Path):
    ledger = start_run(tmp_path, "fix the bug")

    manifest = json.loads(ledger.manifest_path.read_text(encoding="utf-8"))
    assert manifest["run_id"] == ledger.run_id
    assert manifest["status"] == "running"
    assert "fix the bug" in manifest["prompt_preview"]


def test_events_are_appended_as_jsonl(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    ledger.log_event("tool_called", tool="read_file")

    lines = ledger.events_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 3  # run_started, user_prompt_received, tool_called
    for line in lines:
        json.loads(line)  # each line is valid standalone JSON


def test_finish_writes_summary_json(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    ledger.finish("All done.", status="success")

    summary = json.loads(ledger.summary_path.read_text(encoding="utf-8"))
    assert summary["run_id"] == ledger.run_id
    assert summary["status"] == "success"
    assert summary["final_output_preview"] == "All done."


def test_run_ids_are_unique_and_sortable():
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = [new_run_id(now=base + timedelta(seconds=i)) for i in range(50)]

    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)
    assert all(run_id.startswith("run_") for run_id in ids)


# -- events ------------------------------------------------------------------


def test_logs_run_started_and_run_finished(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    ledger.finish("done", status="success")

    types = [event["type"] for event in _events(ledger)]
    assert "run_started" in types
    assert "run_finished" in types


def test_logs_user_prompt_received(tmp_path: Path):
    ledger = start_run(tmp_path, "please fix the login bug")

    events = _events(ledger)
    prompt_events = [event for event in events if event["type"] == "user_prompt_received"]
    assert len(prompt_events) == 1
    assert "login bug" in prompt_events[0]["prompt_preview"]


def test_logs_task_classification(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    ledger.log_task_classified("bug_fix", confidence=0.9)

    events = _events(ledger)
    assert any(event["type"] == "task_classified" and event["task_type"] == "bug_fix" for event in events)


def test_logs_tool_call_start_and_end(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    call_id = ledger.log_tool_call("read_file", {"filepath": "app.py"})
    ledger.log_tool_result(call_id, "read_file", True, "ok", {"content": "x"})

    types = [event["type"] for event in _events(ledger)]
    assert "tool_called" in types
    assert "tool_finished" in types
    calls = store.load_tool_calls(tmp_path, ledger.run_id)
    assert calls[0]["tool_call_id"] == call_id
    assert calls[1]["ok"] is True


def test_logs_command_start_and_end(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    cmd_id = ledger.log_command_start("npm run build", tmp_path)
    ledger.log_command_finish(cmd_id, "npm run build", tmp_path, 0, "stdout", "")

    types = [event["type"] for event in _events(ledger)]
    assert "command_started" in types
    assert "command_finished" in types


def test_logs_diagnostics_parsed(tmp_path: Path):
    ledger = start_run(tmp_path, "run the tests")
    ledger.log_diagnostics(["pytest_fallback"], "1 test failed", {"tool": "pytest", "exit_code": 1})

    events = _events(ledger)
    diag_events = [event for event in events if event["type"] == "diagnostics_parsed"]
    assert len(diag_events) == 1
    assert diag_events[0]["parser_chain"] == ["pytest_fallback"]
    assert diag_events[0]["diagnostics_path"]
    packet_path = ledger.run_dir / diag_events[0]["diagnostics_path"]
    assert packet_path.exists()


def test_diagnostics_integrity_links_command_tool_raw_log_and_packet(tmp_path: Path):
    from shamsu.tools.executor import CommandRunner

    ledger = start_run(tmp_path, "run failing command")
    call_id = ledger.log_tool_call("run_command", {"command": "python -c exit(3)"})
    runner = CommandRunner(
        tmp_path,
        approval_func=lambda _request: True,
        action_ledger=ledger,
    )

    code, stdout, stderr = runner.run('python -c "raise ValueError(\'boom\')"', tmp_path)
    packet = runner.last_error_packet
    assert packet is not None
    data = {
        "exit_code": code,
        "stdout": stdout,
        "stderr": stderr,
        "diagnostics_path": runner.last_diagnostics_path,
    }
    ledger.log_tool_result(call_id, "run_command", False, "failed", data)
    ledger.finish("failed", status="failed")

    validation = store.validate_run(tmp_path, ledger.run_id)
    assert validation["ok"] is True, validation["errors"]
    assert (ledger.run_dir / packet.raw_log_path).is_file()
    assert (ledger.run_dir / runner.last_diagnostics_path).is_file()


def test_model_failure_writes_linked_exception_artifact(tmp_path: Path):
    ledger = start_run(tmp_path, "ask model")
    call_id = ledger.log_model_call_started("planner", "fake-model", "plan")

    ledger.log_model_call_finished(
        "planner",
        "fake-model",
        call_id=call_id,
        error="TimeoutError: model stalled",
        traceback_text="Traceback\nTimeoutError: model stalled",
    )
    ledger.finish("failed", status="failed")

    records = store.load_model_calls(tmp_path, ledger.run_id)
    failed = records[-1]
    assert failed["exception_class"] == "TimeoutError"
    assert failed["exception_message"] == "model stalled"
    assert (ledger.run_dir / failed["traceback_path"]).is_file()
    assert store.validate_run(tmp_path, ledger.run_id)["ok"] is True


def test_run_retention_removes_diagnostics_with_stale_run_and_keeps_fresh_run(tmp_path: Path):
    stale = start_run(tmp_path, "old failure")
    stale.log_diagnostics(
        ["python_fallback"],
        "old failure",
        {"classification": "command_failure", "actionable": True},
    )
    stale.finish("failed", status="failed")
    stale_manifest = json.loads(stale.manifest_path.read_text(encoding="utf-8"))
    stale_manifest["started_at"] = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    stale.manifest_path.write_text(json.dumps(stale_manifest), encoding="utf-8")

    fresh = start_run(tmp_path, "fresh failure")
    fresh.log_diagnostics(
        ["python_fallback"],
        "fresh failure",
        {"classification": "command_failure", "actionable": True},
    )
    fresh.finish("failed", status="failed")

    removed = store.clean_runs(tmp_path, retention_days=30)

    assert removed == [stale.run_id]
    assert stale.run_dir.exists() is False
    assert fresh.run_dir.is_dir()
    assert list(fresh.diagnostics_dir.glob("error_packet_*.json"))


def test_logs_patch_and_mutation_events(tmp_path: Path):
    ledger = start_run(tmp_path, "fix it")
    ledger.log_patch_planned(["app.py"])
    ledger.log_patch_applied(["app.py"])
    ledger.log_mutation_started("txn-1", "apply fix")
    ledger.log_mutation_finished("txn-1", "applied", ["app.py"], rollback_available=True)

    types = [event["type"] for event in _events(ledger)]
    assert "patch_planned" in types
    assert "patch_applied" in types
    assert "mutation_started" in types
    assert "mutation_finished" in types
    mutations = store.load_mutations(tmp_path, ledger.run_id)
    assert mutations[0]["transaction_id"] == "txn-1"
    assert mutations[0]["rollback_available"] is True


def test_logs_final_output(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    ledger.finish("The final answer.", status="success")

    assert ledger.final_output_path.read_text(encoding="utf-8") == "The final answer."
    types = [event["type"] for event in _events(ledger)]
    assert "final_response_written" in types


# -- decision records ---------------------------------------------------------


def test_decision_summary_is_saved(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    ledger.log_decision(
        "run_build_after_patch",
        reason_summary="The patch changed source files, so verification is required.",
        evidence=["changed_files: app.py", "policy: mutations require verification"],
        alternatives_considered=["skip verification", "run full test suite"],
        chosen_action="npm run build",
        confidence=0.86,
        outcome="verification_failed",
    )

    decisions = store.load_decisions(tmp_path, ledger.run_id)
    assert len(decisions) == 1
    record = decisions[0]
    assert record["decision"] == "run_build_after_patch"
    assert record["evidence"] == ["changed_files: app.py", "policy: mutations require verification"]
    assert record["chosen_action"] == "npm run build"
    assert record["outcome"] == "verification_failed"


def test_decision_record_does_not_store_raw_chain_of_thought(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    ledger.log_decision(
        "run_build_after_patch",
        reason_summary="Verification required after a source change.",
        chosen_action="npm run build",
    )

    raw_text = ledger.decisions_path.read_text(encoding="utf-8")
    record = json.loads(raw_text.splitlines()[0])
    # Only the documented, human-written fields exist - no hidden/raw reasoning field.
    assert {
        "decision_id", "run_id", "timestamp", "decision", "reason_summary",
        "evidence", "alternatives_considered", "chosen_action", "confidence", "outcome",
        "goal", "observation", "expected_postcondition", "actual_outcome",
    }.issubset(record)
    assert "thinking" not in record
    assert "chain_of_thought" not in record


# -- command logs -------------------------------------------------------------


def test_command_stdout_stderr_saved_as_separate_files(tmp_path: Path):
    ledger = start_run(tmp_path, "run the build")
    cmd_id = ledger.log_command_start("npm run build", tmp_path)
    ledger.log_command_finish(cmd_id, "npm run build", tmp_path, 2, "build output", "an error occurred")

    stdout_path = ledger.commands_dir / f"{cmd_id}.stdout.log"
    stderr_path = ledger.commands_dir / f"{cmd_id}.stderr.log"
    assert stdout_path.read_text(encoding="utf-8") == "build output"
    assert stderr_path.read_text(encoding="utf-8") == "an error occurred"


def test_command_finished_event_references_stdout_stderr_paths(tmp_path: Path):
    ledger = start_run(tmp_path, "run the build")
    cmd_id = ledger.log_command_start("npm run build", tmp_path)
    event = ledger.log_command_finish(cmd_id, "npm run build", tmp_path, 0, "out", "err")

    assert event["stdout_path"] == f"commands/{cmd_id}.stdout.log"
    assert event["stderr_path"] == f"commands/{cmd_id}.stderr.log"


def test_huge_command_output_not_embedded_in_events_jsonl(tmp_path: Path):
    ledger = start_run(tmp_path, "run the build")
    huge = "x" * 50_000
    cmd_id = ledger.log_command_start("npm run build", tmp_path)
    ledger.log_command_finish(cmd_id, "npm run build", tmp_path, 0, huge, "")

    raw_events_text = ledger.events_path.read_text(encoding="utf-8")
    assert huge not in raw_events_text
    assert (ledger.commands_dir / f"{cmd_id}.stdout.log").read_text(encoding="utf-8") == huge


def test_huge_inline_event_fields_are_truncated(tmp_path: Path):
    from shamsu.action_ledger.config import DEFAULT_CONFIG

    ledger = ActionLedger(tmp_path, config={**DEFAULT_CONFIG, "max_inline_event_size": 100})
    ledger.start("do a thing")
    ledger.log_event("tool_finished", data="y" * 5000)

    events = _events(ledger)
    last = events[-1]
    assert len(last["data"]) < 5000
    assert "truncated" in last["data"]


# -- redaction ----------------------------------------------------------------


def test_api_keys_are_redacted(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    cmd_id = ledger.log_command_start("printenv", tmp_path)
    ledger.log_command_finish(cmd_id, "printenv", tmp_path, 0, 'api_key = "sk-abcdefghijklmnopqrstuvwxyz123456"', "")

    stdout = (ledger.commands_dir / f"{cmd_id}.stdout.log").read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in stdout
    assert "[REDACTED]" in stdout


def test_env_style_secrets_are_redacted(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    ledger.log_decision(
        "inspect_env",
        reason_summary='Found SECRET_KEY = "django-insecure-verysecretvalue" in .env',
        chosen_action="none",
    )

    raw_text = ledger.decisions_path.read_text(encoding="utf-8")
    assert "django-insecure-verysecretvalue" not in raw_text
    assert "[REDACTED]" in raw_text


def test_private_keys_are_redacted(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    key_block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA1234567890\n-----END RSA PRIVATE KEY-----"
    cmd_id = ledger.log_command_start("cat id_rsa", tmp_path)
    ledger.log_command_finish(cmd_id, "cat id_rsa", tmp_path, 0, key_block, "")

    stdout = (ledger.commands_dir / f"{cmd_id}.stdout.log").read_text(encoding="utf-8")
    assert "MIIEpAIBAAKCAQEA1234567890" not in stdout
    assert "[REDACTED]" in stdout


def test_authorization_headers_are_redacted(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    cmd_id = ledger.log_command_start("curl -v https://api.example.com", tmp_path)
    ledger.log_command_finish(
        cmd_id, "curl", tmp_path, 0, "Authorization: Bearer sekrit-token-value-123", ""
    )

    stdout = (ledger.commands_dir / f"{cmd_id}.stdout.log").read_text(encoding="utf-8")
    assert "sekrit-token-value-123" not in stdout
    assert "[REDACTED]" in stdout


# -- boundaries ---------------------------------------------------------------


def test_action_ledger_does_not_write_to_graphiti_or_memory(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")
    ledger.log_decision("d", "r", chosen_action="a")
    ledger.log_context_preview({"task_id": "t", "specialist": "qa", "snippets": []})
    ledger.finish("done")

    assert not (tmp_path / ".shamsu" / "memory").exists()
    assert not (tmp_path / ".shamsu" / "abstract").exists()


def test_action_ledger_storage_is_separate_from_memory_and_abstract(tmp_path: Path):
    ledger = start_run(tmp_path, "do a thing")

    assert ledger.run_dir.is_relative_to(tmp_path / ".shamsu" / "runs")
    assert not ledger.run_dir.is_relative_to(tmp_path / ".shamsu" / "memory")
    assert not ledger.run_dir.is_relative_to(tmp_path / ".shamsu" / "abstract")


def test_action_ledger_context_preview_is_not_fed_back_into_model_context(tmp_path: Path):
    """log_context_preview only writes a local file + event - it must not
    return anything a caller could feed into a prompt, and must not touch
    the context pipeline's own output."""
    ledger = start_run(tmp_path, "do a thing")

    result = ledger.log_context_preview({"task_id": "t1", "specialist": "qa", "snippets": []})

    assert result is None  # nothing returned to feed back into a model call
    preview = store.load_context_preview(tmp_path, ledger.run_id)
    assert preview["task_id"] == "t1"


# -- canonical v2 contract ----------------------------------------------------


def test_v2_records_have_correlation_fields_and_global_sequence(tmp_path: Path):
    ledger = ActionLedger(tmp_path, session_id="session-1", turn_id="turn-1")
    ledger.start("inspect it")
    ledger.log_decision("choose_route", chosen_action="qa")
    call_id = ledger.log_tool_call("read_file", {"path": "app.py"})
    ledger.log_tool_result(call_id, "read_file", True, "ok")
    ledger.finish("done")

    records = (
        store.load_events(tmp_path, ledger.run_id)
        + store.load_decisions(tmp_path, ledger.run_id)
        + store.load_tool_calls(tmp_path, ledger.run_id)
    )
    required = {
        "schema_version", "session_id", "turn_id", "run_id", "operation_id",
        "parent_operation_id", "timestamp", "sequence",
    }
    assert records
    assert all(required.issubset(record) for record in records)
    assert all(record["session_id"] == "session-1" for record in records)
    assert all(record["turn_id"] == "turn-1" for record in records)
    sequences = [record["sequence"] for record in records]
    assert len(sequences) == len(set(sequences))


def test_context_records_are_preserved_per_model_call(tmp_path: Path):
    ledger = start_run(tmp_path, "answer twice")
    first_call = ledger.log_model_call_started("qa", "fake", "first")
    ledger.log_context_preview(
        {"task_id": "t1", "specialist": "qa", "snippets": []},
        model_call_id=first_call,
    )
    ledger.log_model_call_finished("qa", "fake", "one", call_id=first_call)
    second_call = ledger.log_model_call_started("coder", "fake", "second")
    ledger.log_context_preview(
        {"task_id": "t2", "specialist": "coder", "snippets": []},
        model_call_id=second_call,
    )
    ledger.log_model_call_finished("coder", "fake", "two", call_id=second_call)

    contexts = store.load_context_records(tmp_path, ledger.run_id)
    assert [item["task_id"] for item in contexts] == ["t1", "t2"]
    assert [item["model_call_id"] for item in contexts] == [first_call, second_call]
    assert store.load_context_preview(tmp_path, ledger.run_id)["task_id"] == "t2"


def test_terminal_failure_cannot_be_overwritten_by_success_response(tmp_path: Path):
    ledger = start_run(tmp_path, "break")
    ledger.fail("patch application failed")

    ledger.record_final_response("I could not apply the patch.")
    ledger.finish("Everything is done.", status="success")

    assert store.load_manifest(tmp_path, ledger.run_id)["status"] == "failed"
    assert store.load_summary(tmp_path, ledger.run_id)["status"] == "failed"
    assert store.load_final_output(tmp_path, ledger.run_id) == "I could not apply the patch."


def test_summary_counts_calls_not_tool_jsonl_records(tmp_path: Path):
    ledger = start_run(tmp_path, "read")
    call_id = ledger.log_tool_call("read_file", {"path": "app.py"})
    ledger.log_tool_result(call_id, "read_file", True, "ok")

    summary = ledger.finish("done")

    assert len(store.load_tool_calls(tmp_path, ledger.run_id)) == 2
    assert summary["tool_call_count"] == 1


def test_finish_closes_dangling_tool_and_model_records(tmp_path: Path):
    ledger = start_run(tmp_path, "interrupted")
    tool_id = ledger.log_tool_call("read_file", {"path": "app.py"})
    model_id = ledger.log_model_call_started("qa", "fake", "question")

    ledger.finish("stopped", status="cancelled")

    tools = store.load_tool_calls(tmp_path, ledger.run_id)
    models = store.load_model_calls(tmp_path, ledger.run_id)
    assert any(item["tool_call_id"] == tool_id and item["phase"] == "finished" for item in tools)
    assert any(item["model_call_id"] == model_id and item["phase"] == "failed" for item in models)
    assert store.validate_run(tmp_path, ledger.run_id)["ok"] is True


def test_session_history_references_the_canonical_run(tmp_path: Path):
    from shamsu.session.manager import SessionManager

    logger = SessionManager(tmp_path).create_session()
    turn = logger.log("user.prompt", {"prompt": "inspect"}, "User submitted prompt")
    ledger = start_run(tmp_path, "inspect", session_logger=logger)

    linked = [event for event in logger.tail(10) if event["event_type"] == "run.linked"]
    assert linked[-1]["payload"] == {"run_id": ledger.run_id, "turn_id": turn["event_id"]}
    assert ledger.session_id == logger.session_id
    assert ledger.turn_id == turn["event_id"]


def test_validate_run_reports_corrupt_jsonl(tmp_path: Path):
    ledger = start_run(tmp_path, "inspect")
    ledger.finish("done")
    with ledger.events_path.open("a", encoding="utf-8") as handle:
        handle.write("{not-json}\n")

    result = store.validate_run(tmp_path, ledger.run_id)

    assert result["ok"] is False
    assert any("invalid JSON" in error for error in result["errors"])


def test_evidence_finalizer_uses_specific_terminal_outcomes(tmp_path: Path):
    needs_input = start_run(tmp_path / "needs", "choose")
    needs_input.log_event("run_needs_input", question="Which database?")
    assert needs_input.finalize_from_evidence()["status"] == "needs_input"

    denied = start_run(tmp_path / "denied", "delete")
    denied.log_event("approval_denied", action_type="file_delete")
    assert denied.finalize_from_evidence()["status"] == "denied"

    patch_failed = start_run(tmp_path / "patch-failed", "edit")
    patch_failed.log_event("patch_apply_failed", error="context mismatch")
    assert patch_failed.finalize_from_evidence()["status"] == "failed"

    missing_mutation = start_run(tmp_path / "missing-mutation", "edit")
    missing_mutation.log_event("mutation_required_but_missing")
    assert missing_mutation.finalize_from_evidence()["status"] == "failed"

    unverified = start_run(tmp_path / "unverified", "edit")
    unverified.log_mutation_finished("txn-1", "applied", ["app.py"])
    assert unverified.finalize_from_evidence()["status"] == "success_unverified"

    patch_unverified = start_run(tmp_path / "patch-unverified", "edit")
    patch_unverified.log_event("patch_apply_succeeded", files=["app.py"])
    assert patch_unverified.finalize_from_evidence()["status"] == "success_unverified"
