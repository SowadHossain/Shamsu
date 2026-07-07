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
from shamsu.agents.chat_loop import AgentChatLoop
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
async def test_agent_chat_loop_logs_tool_calls_into_action_ledger(tmp_path: Path):
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
async def test_handle_request_workspace_location_branch_finishes_run_with_real_answer(tmp_path: Path):
    """cli/repl.py::_handle_request's own workspace-location shortcut (reached
    for phrasings AgentOrchestrator's earlier gate doesn't catch, e.g. "where
    am i") must close the run with the actual text shown to the user, not an
    empty fallback."""
    from shamsu.cli.repl import _handle_request

    ledger = start_run(tmp_path, "where am i")
    set_current_run(ledger)
    console = Console(file=StringIO(), force_terminal=False, width=120)
    web_tool = WebTool(approval_func=lambda _request: False)
    browser_tool = BrowserTool(tmp_path, approval_func=lambda _request: False)
    try:
        await _handle_request("where am i", tmp_path, console, web_tool, browser_tool)
    finally:
        clear_current_run()

    manifest = store.load_manifest(tmp_path, ledger.run_id)
    assert manifest["status"] == "success"
    summary = store.load_summary(tmp_path, ledger.run_id)
    assert str(tmp_path) in summary["final_output_preview"]
    types = [event["type"] for event in _events(ledger)]
    assert "final_response_written" in types


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
