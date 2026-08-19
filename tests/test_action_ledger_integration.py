"""Integration tests: ActionLedger wired into AgentChatLoop, CommandRunner,
DiagnosticDigest (via CommandRunner), PatchEngine, LLMManager, and the
cli/repl.py request dispatcher - see agent context/prompts/audit_log.md
section 7."""
from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import ActionLedger, start_run
from shamsu.action_ledger import store
from shamsu.agents.chat_loop import AgentChatLoop, AgentLoopResult
from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.diagnostics.types import DiagnosticRecord
from shamsu.llm.manager import LLMManager
from shamsu.patch.engine import PatchEngine
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.tools.executor import CommandRunner
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import WebTool
from shamsu.types import ContextPack, LLMResponse

PASS_CMD = f'"{sys.executable}" -c "print(1)"'


def _events(ledger: ActionLedger) -> list[dict]:
    return [json.loads(line) for line in ledger.events_path.read_text(encoding="utf-8").splitlines()]


class FakeOllamaClient:
    """Minimal stand-in for ollama.AsyncClient: returns one tool call, then a
    plain-text final answer, matching AgentChatLoop's ReAct loop shape."""

    def __init__(self) -> None:
        self._responses = [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "list_files", "arguments": {"path": "."}}}
                    ],
                }
            },
            {"message": {"content": "Done listing files.", "tool_calls": []}},
        ]

    async def chat(self, model, messages, tools, stream, options):
        return self._responses.pop(0)


class FakePlannerLLM:
    """Stands in for AgentChatLoop's `llm` (the planner call) so this test
    never hits a real local model - `client`/`FakeOllamaClient` above remains
    the stand-in for the separate tool-calling client."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        self.calls.append(specialist)
        return LLMResponse(raw="List the current directory contents.", model_used="fake")


@pytest.mark.asyncio
async def test_agent_chat_loop_logs_tool_calls_into_action_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHAMSU_LOG_LEVEL", "verbose")
    ledger = start_run(tmp_path, "list the files here")
    tools = AgentToolRegistry(tmp_path, action_ledger=ledger)
    llm = FakePlannerLLM()
    loop = AgentChatLoop(
        tmp_path,
        client=FakeOllamaClient(),
        tools=tools,
        action_ledger=ledger,
        llm=llm,
    )

    result = await loop.run("list the files here")

    assert result.final == "Done listing files."
    assert llm.calls == ["planner"]
    types = [event["type"] for event in _events(ledger)]
    assert "tool_called" in types
    assert "tool_finished" in types
    tool_calls = store.load_tool_calls(tmp_path, ledger.run_id)
    assert tool_calls[0]["tool"] == "list_files"
    assert tool_calls[1]["ok"] is True
    model_calls = store.load_model_calls(tmp_path, ledger.run_id)
    assert [item["role"] for item in model_calls] == [
        "agent-executor",
        "agent-executor",
        "agent-executor",
        "agent-executor",
    ]
    assert [item["phase"] for item in model_calls] == [
        "started",
        "finished",
        "started",
        "finished",
    ]
    contexts = store.load_context_records(tmp_path, ledger.run_id)
    assert len(contexts) == 2
    assert all(item["specialist"] == "agent-executor" for item in contexts)
    assert all(item["model_call_id"] for item in contexts)


def test_command_runner_writes_command_events_to_action_ledger(tmp_path: Path):
    ledger = start_run(tmp_path, "run a command")
    runner = CommandRunner(tmp_path, approval_func=lambda _request: True, action_ledger=ledger)

    exit_code, stdout, _stderr = runner.run(PASS_CMD, tmp_path)

    assert exit_code == 0
    assert "1" in stdout
    types = [event["type"] for event in _events(ledger)]
    assert "command_started" in types
    assert "command_finished" in types
    finished = [event for event in _events(ledger) if event["type"] == "command_finished"][0]
    assert finished["exit_code"] == 0
    stdout_path = ledger.run_dir / finished["stdout_path"]
    assert stdout_path.exists()


def test_command_runner_writes_diagnostics_events_via_diagnostic_digest(tmp_path: Path):
    """DiagnosticDigest.run() is invoked by CommandRunner for every command;
    when an ActionLedger is attached, the resulting ErrorPacket must be saved
    under the run's diagnostics/ folder and referenced from an event."""
    ledger = start_run(tmp_path, "run a command")
    runner = CommandRunner(tmp_path, approval_func=lambda _request: True, action_ledger=ledger)

    runner.run(PASS_CMD, tmp_path)

    diagnostics_events = [event for event in _events(ledger) if event["type"] == "diagnostics_parsed"]
    assert len(diagnostics_events) == 1
    diagnostics_path = ledger.run_dir / diagnostics_events[0]["diagnostics_path"]
    assert diagnostics_path.exists()
    packet = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    assert packet["command"] == PASS_CMD


def test_patch_engine_writes_mutation_events_to_action_ledger(tmp_path: Path):
    ledger = start_run(tmp_path, "create a file")
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True, action_ledger=ledger)

    result = engine.execute_change_request(
        {
            "change_plan": {
                "reason": "add a new module",
                "operations": [{"op": "create_file", "path": "new_module.py", "content": "value = 1\n"}],
                "verification_command": "",
                "destructive": False,
            },
            "patch": "",
        }
    )

    assert result.ok is True
    types = [event["type"] for event in _events(ledger)]
    assert "mutation_started" in types
    assert "mutation_finished" in types
    mutations = store.load_mutations(tmp_path, ledger.run_id)
    assert mutations[0]["transaction_id"] == result.transaction_id
    assert mutations[0]["status"] == "applied"
    assert "new_module.py" in mutations[0]["touched_files"]
    assert mutations[0]["operations"][0]["op"] == "create_file"
    assert mutations[0]["before_hashes"]["new_module.py"] is None
    assert mutations[0]["after_hashes"]["new_module.py"]
    assert mutations[0]["abstract_index_state"] == "stale"


def test_write_file_tool_links_hashes_diff_and_rollback_to_run(tmp_path: Path):
    ledger = start_run(tmp_path, "create note")
    tools = AgentToolRegistry(
        tmp_path,
        approval_func=lambda _request: True,
        action_ledger=ledger,
    )

    result = tools.execute("write_file", {"filepath": "note.txt", "content": "hello\n"})

    assert result.ok is True
    mutation = store.load_mutations(tmp_path, ledger.run_id)[0]
    assert mutation["transaction_id"] == result.data["transaction_id"]
    assert mutation["before_hashes"]["note.txt"] is None
    assert mutation["after_hashes"]["note.txt"]
    assert mutation["patch_path"].endswith("patch.diff")
    assert (tmp_path / mutation["patch_path"]).is_file()
    assert mutation["rollback_available"] is True


def test_edit_file_tool_records_canonical_mutation(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    ledger = start_run(tmp_path, "edit app.py")
    tools = AgentToolRegistry(
        tmp_path,
        approval_func=lambda _request: True,
        action_ledger=ledger,
    )

    result = tools.execute(
        "edit_file",
        {"filepath": "app.py", "old_string": "value = 1", "new_string": "value = 2"},
    )

    assert result.ok is True
    mutations = store.load_mutations(tmp_path, ledger.run_id)
    assert len(mutations) == 1
    assert mutations[0]["transaction_id"] == result.data["transaction_id"]
    assert mutations[0]["status"] == "applied"
    assert mutations[0]["touched_files"] == ["app.py"]
    assert mutations[0]["before_hashes"]["app.py"] != mutations[0]["after_hashes"]["app.py"]


def test_failed_run_gets_run_failed_event(tmp_path: Path):
    ledger = start_run(tmp_path, "do something that fails")

    ledger.fail("boom: something went wrong")

    manifest = store.load_manifest(tmp_path, ledger.run_id)
    assert manifest["status"] == "failed"
    types = [event["type"] for event in _events(ledger)]
    assert "run_failed" in types
    assert "run_finished" not in types


# -- fixes for previously-noted limitations ----------------------------------


@pytest.mark.asyncio
async def test_handle_request_records_answer_before_lifecycle_finalization(tmp_path: Path):
    """cli/repl.py::_handle_request's own workspace-location shortcut (reached
    for phrasings AgentOrchestrator's earlier gate doesn't catch, e.g. "where
    am i") must close the run with the actual text shown to the user, not an
    empty fallback."""
    from shamsu.cli.repl import _finish_current_run, _handle_request

    ledger = start_run(tmp_path, "where am i")
    set_current_run(ledger)
    console = Console(file=StringIO(), force_terminal=False, width=120)
    web_tool = WebTool(approval_func=lambda _request: False)
    browser_tool = BrowserTool(tmp_path, approval_func=lambda _request: False)
    try:
        await _handle_request("where am i", tmp_path, console, web_tool, browser_tool)
        _finish_current_run(tmp_path, ledger)
    finally:
        clear_current_run()

    manifest = store.load_manifest(tmp_path, ledger.run_id)
    assert manifest["status"] == "success"
    summary = store.load_summary(tmp_path, ledger.run_id)
    assert str(tmp_path) in summary["final_output_preview"]
    types = [event["type"] for event in _events(ledger)]
    assert "final_response_written" in types
    assert store.load_decisions(tmp_path, ledger.run_id)[0]["decision"] == "dispatch_request"


@pytest.mark.asyncio
async def test_router_route_logs_model_call_and_decision(tmp_path: Path):
    """LLMManager.route() (the router specialist) must log a *_model_called /
    model_response_received pair and a routing decision record, matching the
    same coverage run_specialist already has for planner/coder."""

    class FakeLLM(LLMManager):
        async def _generate(self, model, system, prompt, **kwargs):
            return '{"intent": "qa", "complexity": "single", "confidence": 0.8}'

    ledger = start_run(tmp_path, "what does this do")
    llm = FakeLLM(action_ledger=ledger)

    decision = await llm.route("what does this do", "a small project")

    assert decision.intent == "qa"
    types = [event["type"] for event in _events(ledger)]
    assert "router_model_called" in types
    assert "model_response_received" in types
    assert "decision_recorded" in types
    decisions = store.load_decisions(tmp_path, ledger.run_id)
    assert decisions[0]["decision"] == "route_prompt_as_qa"
    assert decisions[0]["outcome"] == "routed"


@pytest.mark.asyncio
async def test_run_specialist_logs_context_preview_and_model_call(tmp_path: Path):
    class FakeLLM(LLMManager):
        async def _generate(self, model, system, prompt, **kwargs):
            return "the answer"

    ledger = start_run(tmp_path, "explain this")
    llm = FakeLLM(action_ledger=ledger)
    pack = ContextPack(task_id="t1", step_id=1, specialist="qa", user_request="explain this")

    response = await llm.run_specialist("qa", pack)

    assert response.raw == "the answer"
    types = [event["type"] for event in _events(ledger)]
    assert "context_pack_built" in types
    assert "qa_model_called" in types
    assert "model_response_received" in types
    preview = store.load_context_preview(tmp_path, ledger.run_id)
    assert preview["task_id"] == "t1"


@pytest.mark.asyncio
async def test_generate_structured_logs_model_call_and_context_preview(tmp_path: Path):
    seen: dict[str, object] = {}

    class FakeLLM(LLMManager):
        async def _generate(self, model, system, prompt, **kwargs):
            seen.update(kwargs)
            return '{"ok": true}'

    ledger = start_run(tmp_path, "write a project plan")
    llm = FakeLLM(action_ledger=ledger)

    raw = await llm.generate_structured(
        "coder", "system", "prompt", {"type": "object"}, num_predict=4096
    )

    assert raw == '{"ok": true}'
    assert seen["num_predict"] == 4096
    model_calls = store.load_model_calls(tmp_path, ledger.run_id)
    assert [item["phase"] for item in model_calls] == ["started", "finished"]
    assert all(item["role"] == "coder" for item in model_calls)
    contexts = store.load_context_records(tmp_path, ledger.run_id)
    assert len(contexts) == 1
    assert contexts[0]["task_id"] == "coder-structured"
    assert contexts[0]["model_call_id"] == model_calls[0]["model_call_id"]


def test_diagnostic_digest_logs_code_memory_queried_for_exports_and_imports(tmp_path: Path):
    """DiagnosticDigest's Codebase-Memory MCP lookups (get_exports/get_imports,
    used to build root-cause facts for missing-export errors) must log
    code_memory_queried events when a run is active - not just SearchAgent's."""

    class FakeMemoryAdapter:
        def healthcheck(self, workspace_root):
            return type("Health", (), {"ok": True, "message": ""})()

        def get_exports(self, workspace_root, path):
            return {"ok": True, "results": [{"name": "doThing"}]}

        def get_imports(self, workspace_root, path):
            return {"ok": True, "results": [{"name": "helper"}]}

    (tmp_path / "app.js").write_text("import { doThing } from './lib';\n", encoding="utf-8")
    (tmp_path / "lib.js").write_text("export function doThing() {}\n", encoding="utf-8")

    ledger = start_run(tmp_path, "fix the missing export")
    set_current_run(ledger)
    try:
        digest = DiagnosticDigest(tmp_path, memory_adapter=FakeMemoryAdapter())
        record = DiagnosticRecord(
            tool="node", language="javascript", severity="error",
            category="missing_export", message="'doThing' is not exported",
            file="app.js", module="./lib", symbol="doThing",
        )
        digest._codebase_memory_facts([record])
    finally:
        clear_current_run()

    types = [event["type"] for event in _events(ledger)]
    query_events = [event for event in _events(ledger) if event["type"] == "code_memory_queried"]
    assert "code_memory_queried" in types
    assert {event["query_type"] for event in query_events} == {"get_exports", "get_imports"}


def test_raw_model_reasoning_is_not_persisted_without_debug_opt_in(tmp_path: Path):
    from shamsu.session.manager import SessionManager

    logger = SessionManager(tmp_path).create_session()
    ledger = start_run(tmp_path, "reason", session_logger=logger)
    llm = LLMManager(session_logger=logger, action_ledger=ledger)

    llm._log_thinking("fake", "private step-by-step reasoning")

    event = [item for item in logger.tail(10) if item["event_type"] == "llm.thinking"][-1]
    assert event["payload"]["reasoning_available"] is True
    assert event["payload"]["thinking_chars"] > 0
    assert "thinking" not in event["payload"]


# -- file.write route: a chat-shaped answer must not report success ----------


@pytest.mark.asyncio
async def test_file_write_route_without_a_tool_call_is_not_reported_success(
    tmp_path: Path, monkeypatch
):
    """Live repro: "add a DELETE endpoint ..." landed on the file.write route,
    the model answered with a markdown code fence instead of calling
    edit_file/write_file, and the run still finished as "success" with zero
    tool calls and zero changed files - nothing caught a mutation route that
    made no mutation."""
    from shamsu.cli import repl as repl_module

    async def fake_agent_chat(*args, **kwargs):
        return AgentLoopResult(final="```python\ndef delete_task(id):\n    ...\n```")

    monkeypatch.setattr(repl_module, "_run_agent_chat", fake_agent_chat)

    ledger = start_run(tmp_path, "create a new function in app.py")
    set_current_run(ledger)
    console = Console(file=StringIO(), force_terminal=False, width=120)
    web_tool = WebTool(approval_func=lambda _request: False)
    browser_tool = BrowserTool(tmp_path, approval_func=lambda _request: False)
    try:
        await repl_module._handle_request(
            "create a new function in app.py", tmp_path, console, web_tool, browser_tool
        )
        repl_module._finish_current_run(tmp_path, ledger)
    finally:
        clear_current_run()

    manifest = store.load_manifest(tmp_path, ledger.run_id)
    assert manifest["status"] != "success"
    types = [event["type"] for event in _events(ledger)]
    assert "mutation_required_but_missing" in types


@pytest.mark.asyncio
async def test_file_write_route_with_a_real_change_still_succeeds(tmp_path: Path, monkeypatch):
    """Regression guard for the fix above: a file.write turn that genuinely
    changed a file must still report success."""
    from shamsu.cli import repl as repl_module

    async def fake_agent_chat(*args, **kwargs):
        return AgentLoopResult(final="Added the function.", changed_files=("app.py",))

    monkeypatch.setattr(repl_module, "_run_agent_chat", fake_agent_chat)

    ledger = start_run(tmp_path, "create a new function in app.py")
    set_current_run(ledger)
    console = Console(file=StringIO(), force_terminal=False, width=120)
    web_tool = WebTool(approval_func=lambda _request: False)
    browser_tool = BrowserTool(tmp_path, approval_func=lambda _request: False)
    try:
        await repl_module._handle_request(
            "create a new function in app.py", tmp_path, console, web_tool, browser_tool
        )
        repl_module._finish_current_run(tmp_path, ledger)
    finally:
        clear_current_run()

    manifest = store.load_manifest(tmp_path, ledger.run_id)
    assert manifest["status"] == "success"
    types = [event["type"] for event in _events(ledger)]
    assert "mutation_required_but_missing" not in types


@pytest.mark.asyncio
async def test_file_write_route_asks_before_unspecified_auth_approach(
    tmp_path: Path, monkeypatch
):
    from shamsu.cli import repl as repl_module
    from shamsu.session.manager import SessionManager

    async def agent_chat_must_not_run(*args, **kwargs):
        raise AssertionError("the executor must not run before the user chooses an auth approach")

    monkeypatch.setattr(repl_module, "_run_agent_chat", agent_chat_must_not_run)

    logger = SessionManager(tmp_path).create_session("Auth choice")
    ledger = start_run(tmp_path, "Add authentication to app.py.")
    set_current_run(ledger)
    console = Console(file=StringIO(), force_terminal=False, width=120, record=True)
    web_tool = WebTool(approval_func=lambda _request: False)
    browser_tool = BrowserTool(tmp_path, approval_func=lambda _request: False)
    try:
        await repl_module._handle_request(
            "Add authentication to app.py.",
            tmp_path,
            console,
            web_tool,
            browser_tool,
            session_logger=logger,
        )
    finally:
        clear_current_run()

    pending = logger.get_pending_question()
    assert pending["question"] == "Which authentication approach should I implement?"
    assert pending["source"] == "direct_file_upfront"
    assert "Server sessions" in console.export_text()
    assert "run_needs_input" in [event["type"] for event in _events(ledger)]
    assert "Which authentication approach" in ledger.narrative_path.read_text(encoding="utf-8")


# -- a recovered patch retry must not fail the whole run ----------------------


def test_evidence_outcome_recovers_when_a_retry_fixes_the_same_file(tmp_path: Path):
    """Live repro: bug_fix's first diff was invalid (context mismatch), it
    failed to apply, and an automatic retry then rewrote the file correctly
    and it applied - confirmed independently with py_compile. The run was
    still reported "failed" solely because a patch_apply_failed event existed
    anywhere in the run's history, ignoring the later success for that same
    file."""
    ledger = start_run(tmp_path, "fix the syntax error")
    ledger.log_event("patch_apply_failed", files=["webapp/broken.py"], error="context mismatch")
    ledger.log_event("patch_apply_succeeded", files=["webapp/broken.py"])
    ledger.log_verification_result(True, "compiles", command="py_compile", required=True)

    assert ledger.evidence_outcome() == "success"


def test_evidence_outcome_still_fails_when_a_patch_failure_is_never_recovered(tmp_path: Path):
    """Regression guard: a genuinely unrecovered patch failure - no later
    success for that file - must still fail the run."""
    ledger = start_run(tmp_path, "fix the syntax error")
    ledger.log_event("patch_apply_failed", files=["webapp/broken.py"], error="still broken")

    assert ledger.evidence_outcome() == "failed"


def test_evidence_outcome_fails_when_a_different_files_patch_never_recovers(tmp_path: Path):
    """A later success for file A must not paper over an unrecovered failure
    on unrelated file B in the same run."""
    ledger = start_run(tmp_path, "fix two files")
    ledger.log_event("patch_apply_failed", files=["b.py"], error="still broken")
    ledger.log_event("patch_apply_succeeded", files=["a.py"])

    assert ledger.evidence_outcome() == "failed"


# -- a denial the agent worked around must not fail the whole run -------------


def test_evidence_outcome_recovers_when_the_agent_works_around_a_denial(tmp_path: Path):
    """Live repro 2026-08-19: a headless run repaired a truncated js/main.js in
    one turn - one patch, braces 8/3 to 26/26, `node --check` clean - and
    reported `denied`, exit 1. The model had earlier tried
    `cat js/main.js | wc -l`, been refused, and simply used read_file instead.

    Every other failure kind here already asks whether the agent recovered.
    Approval denial never did."""
    ledger = start_run(tmp_path, "fix the syntax error")
    ledger.log_event("approval_denied", command="cat js/main.js | wc -l")
    ledger.log_event("patch_apply_succeeded", files=["js/main.js"])
    ledger.log_verification_result(True, "parses", command="node --check", required=True)

    assert ledger.evidence_outcome() == "success"


def test_a_denial_that_ends_the_run_is_still_denied(tmp_path: Path):
    """Regression guard: `denied` must still mean the run could not proceed."""
    ledger = start_run(tmp_path, "delete the database")
    ledger.log_event("approval_denied", command="rm -rf data")

    assert ledger.evidence_outcome() == "denied"


def test_work_before_a_denial_does_not_excuse_it(tmp_path: Path):
    """Positional. A denial that stops the run has nothing after it, whatever
    happened earlier."""
    ledger = start_run(tmp_path, "two steps")
    ledger.log_event("patch_apply_succeeded", files=["a.py"])
    ledger.log_event("approval_denied", command="rm -rf data")

    assert ledger.evidence_outcome() == "denied"


def test_a_successful_command_after_a_denial_also_counts_as_recovery(tmp_path: Path):
    ledger = start_run(tmp_path, "run the tests")
    ledger.log_event("approval_denied", command="curl example.com")
    ledger.log_event("command_finished", command="pytest -q", exit_code=0)

    assert ledger.evidence_outcome() != "denied"


def test_a_failing_command_after_a_denial_is_not_recovery(tmp_path: Path):
    ledger = start_run(tmp_path, "run the tests")
    ledger.log_event("approval_denied", command="curl example.com")
    ledger.log_event("command_finished", command="pytest -q", exit_code=1)

    assert ledger.evidence_outcome() == "denied"
