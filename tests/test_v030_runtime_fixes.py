"""Integration tests for the v0.3.0 agent-runtime failure fixes.

Covers the observed failures: git/untracked routing, empty/placeholder grep
queries, prose-only tool promises, greenfield PRD builds, the generic-Django
fallback, direct coding answers, and the detailed audit trail.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.audit import SessionAuditLog
from shamsu.cli import repl
from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import LLMResponse, ParsedPRD


def _quiet_console() -> Console:
    return Console(file=io.StringIO(), width=200)


# --- Chat-loop scaffolding (mirrors tests/test_chat_loop_clarify.py) --------


class ScriptedClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = responses
        self.messages_seen: list[list[dict]] = []

    async def chat(self, model, messages, tools, stream, options):
        self.messages_seen.append([dict(message) for message in messages])
        return self._responses.pop(0)


class NoPlanLLM:
    async def run_specialist(self, specialist, pack):
        return LLMResponse(raw="", model_used="fake")


class PlanLLM:
    async def run_specialist(self, specialist, pack):
        return LLMResponse(raw="Plan: create hello.py that prints hi", model_used="fake")


def _tool_call(name: str, arguments: dict) -> dict:
    return {"id": f"call_{name}", "function": {"name": name, "arguments": arguments}}


def _message(content: str = "", tool_calls: list[dict] | None = None) -> dict:
    return {"message": {"content": content, "tool_calls": tool_calls or []}}


def _loop(tmp_path: Path, client, llm=None, **kwargs) -> AgentChatLoop:
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    return AgentChatLoop(tmp_path, client=client, tools=tools, llm=llm or NoPlanLLM(), **kwargs)


# --- A. untracked files -> git_status, never find_file ----------------------


class _RecordingRegistry:
    calls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def execute(self, name, arguments):
        _RecordingRegistry.calls.append(name)
        if name == "git_status":
            return SimpleNamespace(
                ok=True,
                message="Read git status.",
                data={
                    "is_git_repo": True,
                    "is_dirty": True,
                    "changed_files": ["new.py"],
                    "raw_output": "?? new.py",
                    "error": "",
                },
            )
        return SimpleNamespace(
            ok=True,
            message="Git command completed.",
            data={"command": name, "exit_code": 0, "stdout": "", "stderr": ""},
        )


def test_untracked_files_uses_git_status_not_find_file(tmp_path, monkeypatch):
    prompt = "can you see the untracked files?"
    # Classification: an untracked-files question is a read-only git request.
    assert repl.is_git_request(prompt)
    assert repl.is_read_only_git_request(prompt)
    assert not repl.is_git_mutation_request(prompt)

    _RecordingRegistry.calls = []
    monkeypatch.setattr(repl, "AgentToolRegistry", _RecordingRegistry)
    monkeypatch.setattr(repl, "_make_approval_manager", lambda *a, **k: None)
    monkeypatch.setattr(repl, "get_current_run", lambda: None)

    asyncio.run(repl._handle_git_request(prompt, tmp_path, _quiet_console()))

    assert "git_status" in _RecordingRegistry.calls
    assert _RecordingRegistry.calls[0] == "git_status"
    assert "find_file" not in _RecordingRegistry.calls


# --- B. add + commit -> git_status, git_add_all, git_commit -----------------


def test_git_commit_flow(tmp_path, monkeypatch):
    prompt = "add files to git and commit"
    assert repl.is_git_request(prompt)
    assert repl.is_git_mutation_request(prompt)
    assert repl._looks_like_git_add_commit_request(prompt)

    _RecordingRegistry.calls = []
    monkeypatch.setattr(repl, "AgentToolRegistry", _RecordingRegistry)
    monkeypatch.setattr(repl, "get_current_run", lambda: None)
    monkeypatch.setattr(
        repl, "_make_approval_manager", lambda *a, **k: SimpleNamespace(ask=lambda _request: True)
    )

    def _no_agent_chat(*args, **kwargs):
        raise AssertionError("add+commit must be deterministic, not the LLM loop")

    monkeypatch.setattr(repl, "_run_agent_chat", _no_agent_chat)

    asyncio.run(repl._handle_git_request(prompt, tmp_path, _quiet_console()))

    assert _RecordingRegistry.calls == ["git_status", "git_add_all", "git_commit"]
    assert "find_file" not in _RecordingRegistry.calls


# --- C. empty / placeholder grep query is rejected before execution ---------


def test_no_empty_grep_query_unit(tmp_path):
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    for bad in ("?", "", "   ", "<query>"):
        result = registry.execute("grep_files", {"query": bad, "path": "."})
        assert result.ok is False
        assert "placeholder" in result.message.lower() or "query" in result.message.lower()


def test_no_empty_grep_query_corrects_model(tmp_path):
    client = ScriptedClient([
        _message(tool_calls=[_tool_call("grep_files", {"query": "?", "path": "."})]),
        _message(content="I could not search without a real query."),
    ])
    loop = _loop(tmp_path, client)

    result = asyncio.run(loop.run("search the code"))

    # The placeholder grep was rejected and the loop injected a correction that
    # tells the model to pass a real query.
    assert len(client.messages_seen) == 2
    correction = "\n".join(str(m.get("content", "")) for m in client.messages_seen[1])
    assert "query" in correction.lower()
    assert result.final == "I could not search without a real query."


# --- D. a prose-only tool promise cannot end the loop -----------------------


def test_prose_promise_forces_tool_call(tmp_path):
    client = ScriptedClient([
        _message(content="I will read index.html next."),
        _message(content="I will read index.html now."),
        _message(content="Let me read index.html."),
    ])
    loop = _loop(tmp_path, client)

    result = asyncio.run(loop.run("look at index.html"))

    # The loop retried, then returned an explicit blocked message - never the
    # bare promise.
    assert "index.html next" not in result.final
    assert result.stopped is True
    assert result.timeout_category == "tool_call_missing_after_promise"
    assert "did not actually call a tool" in result.final or "stall" in result.final.lower()
    assert len(client.messages_seen) >= 2


# --- E. greenfield PRD build creates html/css/js; never reads missing files -


def test_greenfield_prd_html_css_js(tmp_path, monkeypatch):
    (tmp_path / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    prd_file = tmp_path / "Product Requirements Document.pdf"
    prd_file.write_bytes(b"%PDF-1.4 fake")

    parsed = ParsedPRD(
        title="Quick Notes",
        sections={},
        raw_text="Build a note app with HTML, CSS, and vanilla JavaScript. Add and delete notes.",
    )
    read_paths: list[Path] = []

    def _fake_parse(path):
        read_paths.append(Path(path))
        return parsed

    monkeypatch.setattr(repl, "parse_prd_file", _fake_parse)
    monkeypatch.setattr(repl, "_ensure_git_repo", lambda *a, **k: None)

    agent_calls: list[str] = []

    async def _fake_agent_chat(user_input, workspace, console, **kwargs):
        agent_calls.append(user_input)

    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent_chat)

    asyncio.run(
        repl._handle_prd_build_request(
            "from the PRD implement with html css js", tmp_path, _quiet_console()
        )
    )

    # The PDF PRD was read/extracted.
    assert prd_file in read_paths
    # The three frontend files were created deterministically (not assumed).
    for name in ("index.html", "style.css", "script.js"):
        assert (tmp_path / name).exists(), f"{name} should have been created"
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert "style.css" in index and "script.js" in index
    # The agent was then asked to EXTEND the created files, not read missing ones.
    assert agent_calls, "the agent should be invoked to flesh out the files"
    assert "EXTEND" in agent_calls[0]


# --- F. plan-prd does not fall back to a generic Django project -------------


def test_plan_prd_no_generic_django_fallback():
    prd_text = (
        "# Habit Tracker\n"
        "A small habit tracker built with HTML, CSS, and vanilla JavaScript.\n\n"
        "## Features\n"
        "- Add a habit\n"
        "- Mark a habit done for today\n"
        "- Data persists in localStorage\n"
    )
    parsed = parse_prd_text(prd_text, fallback_title="Habit Tracker", markdown=True)
    spec = build_project_spec(parsed)

    files = [f.path for f in spec.generation_order]
    pages = [p.name.lower() for p in spec.pages]

    assert "manage.py" not in files
    assert not any("settings.py" in f for f in files)
    assert not any(word in f.lower() for f in files for word in ("login", "register", "dashboard"))
    assert not any(word in page for page in pages for word in ("dashboard", "login", "register"))
    # It should propose the static frontend files instead.
    assert "index.html" in files
    assert "script.js" in files


# --- G. a direct coding question answers immediately, no planner/tool loop --


class _DirectCodeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def run_specialist(self, specialist, pack):
        self.calls += 1
        return LLMResponse(
            raw="```python\nprimes=[]\nn=2\nwhile len(primes)<100:\n    ...\n```",
            model_used="fake",
        )


def test_direct_prime_code_no_agent_timeout(tmp_path):
    prompt = "write me a python code that prints prime numbers, first 100 of them"
    assert repl._looks_like_direct_code_request(prompt)
    assert not repl._looks_like_file_write_request(prompt)
    assert repl._classify_route_label(prompt, tmp_path) == "direct_code"

    console = _quiet_console()
    llm = _DirectCodeLLM()
    asyncio.run(repl._run_direct_code_answer(prompt, console, llm))

    # Exactly one specialist call (no planner, no tool loop) and code came back.
    assert llm.calls == 1
    output = console.file.getvalue()
    assert "primes" in output


# --- H. the audit trail records every step of a file-write task -------------


def test_audit_log_records_everything(tmp_path):
    audit = SessionAuditLog(tmp_path, session_id="sess-h")
    audit.log_route("file.write", workflow="agent-chat", model="fake", tier="light")

    client = ScriptedClient([
        _message(tool_calls=[_tool_call("write_file", {"filepath": "hello.py", "content": "print('hi')\n"})]),
        _message(content="Created hello.py that prints hi."),
    ])
    loop = _loop(tmp_path, client, llm=PlanLLM(), audit=audit)

    result = asyncio.run(loop.run("create hello.py that prints hi"))

    assert result.final == "Created hello.py that prints hi."
    assert (tmp_path / "hello.py").exists()

    events_path = tmp_path / ".shamsu" / "audit" / "events.jsonl"
    session_path = tmp_path / ".shamsu" / "audit" / "sessions" / "sess-h.jsonl"
    assert events_path.exists() and session_path.exists()

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    types = {event["event_type"] for event in events}
    for required in {
        "user.prompt",
        "route.selected",
        "planner.output",
        "tool.call",
        "tool.result",
        "file.change",
        "assistant.final",
    }:
        assert required in types, f"audit trail missing {required}: {sorted(types)}"

    prompt_event = next(e for e in events if e["event_type"] == "user.prompt")
    assert "hello.py" in prompt_event["prompt"]
    file_event = next(e for e in events if e["event_type"] == "file.change")
    assert file_event["filepath"] == "hello.py"
    assert "print('hi')" in file_event["content"]
    assert file_event["diff"].strip() != ""
    final_event = next(e for e in events if e["event_type"] == "assistant.final")
    assert "hello.py" in final_event["final"]
