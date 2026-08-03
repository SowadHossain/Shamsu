"""Tests for G9 observability: the working trace now surfaces search_index
queries + hits and a dim reasoning glimpse at normal verbosity, and `/context
show` reports the observability snapshot."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from rich.console import Console

import shamsu.cli.repl as repl
import shamsu.llm.manager as manager_module
from shamsu.agents.chat_loop import AgentChatLoop, _search_summary, _thinking_preview
from shamsu.llm.manager import LLMManager
from shamsu.tools.agent_tools import AgentToolRegistry, ToolResult
from shamsu.ui.trace import format_trace_line


class _NoPlanLLM:
    async def run_specialist(self, specialist, pack):  # noqa: ANN001
        raise RuntimeError("no planner in tests")


class _ScriptedClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)

    async def chat(self, model, messages, tools, stream, options):  # noqa: ANN001
        return self._responses.pop(0)


def _tool_response(name: str, arguments: dict) -> dict:
    return {"message": {"content": "", "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": arguments}}]}}


# ---------------------------------------------------------------------------
# helpers + labels
# ---------------------------------------------------------------------------


def test_thinking_preview_is_one_line_and_bounded():
    preview = _thinking_preview("Line one.\n\nLine two is much longer " + "x" * 500)
    assert "\n" not in preview
    assert len(preview) <= 203  # limit + ellipsis
    assert preview.endswith("...")


def test_search_summary_lists_paths_and_scores():
    summary = _search_summary("game loop", [{"file_path": "a.py", "score": 0.83}, {"file_path": "b.py", "score": 0.7}])
    assert summary == '"game loop" -> a.py (0.83), b.py (0.70)'


def test_search_summary_handles_no_hits():
    assert _search_summary("nothing", []) == '"nothing" -> no hits'


def test_trace_labels_for_reasoning_and_search():
    assert format_trace_line("assistant.thinking", "planning", None, "normal") == "Reasoning: planning"
    assert format_trace_line("context.search", '"q" -> a.py (0.8)', None, "normal").startswith("Search:")


# ---------------------------------------------------------------------------
# chat-loop emits at normal verbosity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_index_emits_visible_context_trace(tmp_path: Path):
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    # Force a successful search result without needing the real index/backend.
    registry.execute = lambda name, args: ToolResult(
        True, "Found 2.", {"query": args.get("query"), "results": [{"file_path": "game.js", "score": 0.83}]}
    )
    client = _ScriptedClient(
        [_tool_response("search_index", {"query": "game loop"}), {"message": {"content": "Found it.", "tool_calls": []}}]
    )
    events: list[tuple[str, str, str]] = []
    loop = AgentChatLoop(
        tmp_path, client=client, tools=registry, llm=_NoPlanLLM(),
        on_trace=lambda et, msg, payload, level: events.append((et, msg, level)),
    )
    await loop.run("find the game loop")

    search_events = [(msg, level) for et, msg, level in events if et == "context.search"]
    assert search_events
    msg, level = search_events[0]
    assert "game loop" in msg and "game.js" in msg
    assert level == "normal"  # visible without a debug flag


@pytest.mark.asyncio
async def test_reasoning_is_surfaced_at_normal(tmp_path: Path):
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    client = _ScriptedClient(
        [{"message": {"content": "The answer is 42.", "tool_calls": [], "thinking": "Let me reason step by step here."}}]
    )
    events: list[tuple[str, str, str]] = []
    loop = AgentChatLoop(
        tmp_path, client=client, tools=registry, llm=_NoPlanLLM(),
        on_trace=lambda et, msg, payload, level: events.append((et, msg, level)),
    )
    result = await loop.run("what is 6 times 7")

    thinking_events = [(msg, level) for et, msg, level in events if et == "assistant.thinking"]
    assert thinking_events
    assert thinking_events[0][1] == "normal"
    assert "reason step by step" in thinking_events[0][0]
    # Reasoning is kept OUT of the visible answer.
    assert "reason step by step" not in result.final
    assert result.final == "The answer is 42."


# ---------------------------------------------------------------------------
# /context show
# ---------------------------------------------------------------------------


def test_context_show_reports_observability(tmp_path: Path):
    console = Console(record=True, width=100)
    repl._handle_context("/context show", tmp_path, console)
    out = console.export_text()
    assert "Observability" in out
    assert "Search" in out and "Reasoning" in out
    assert "Trace mode" in out


# ---------------------------------------------------------------------------
# Reasoning on the SPECIALIST path (QA / PRD summary / planner / direct-code).
# A reasoning model only fills the separate `thinking` field when asked; without
# `think: true` the field stays empty forever and SHAMSU shows no CoT at all.
# ---------------------------------------------------------------------------


def _fake_ollama(monkeypatch, captured: list[dict], *, status: int = 200):
    """Patch httpx.AsyncClient inside the manager with a scripted /api/generate."""

    class FakeStream:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        async def __aenter__(self):
            captured.append(self._payload)
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            # Only the think-bearing attempt is rejected, mimicking an Ollama
            # build/model that doesn't accept the flag.
            if status != 200 and self._payload.get("think"):
                request = httpx.Request("POST", "http://localhost:11434/api/generate")
                response = httpx.Response(status, request=request)
                raise httpx.HTTPStatusError("rejected", request=request, response=response)

        async def aiter_lines(self):
            yield json.dumps({"thinking": "Step one. "})
            yield json.dumps({"thinking": "Step two."})
            yield json.dumps({"response": "The answer is 42."})
            yield json.dumps({"done": True, "prompt_eval_count": 7})

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def stream(self, method, url, json=None):  # noqa: A002
            return FakeStream(json)

    monkeypatch.setattr(manager_module.httpx, "AsyncClient", FakeClient)


@pytest.mark.asyncio
async def test_reasoning_model_is_asked_to_think(monkeypatch):
    captured: list[dict] = []
    _fake_ollama(monkeypatch, captured)
    seen: list[tuple[str, str]] = []
    manager = LLMManager(on_thinking=lambda model, thinking: seen.append((model, thinking)))

    text = await manager._generate("deepseek-r1:7b", "sys", "prompt")

    assert captured[0].get("think") is True
    # The chain-of-thought is surfaced whole...
    assert seen == [("deepseek-r1:7b", "Step one. Step two.")]
    # ...and kept out of the answer.
    assert text == "The answer is 42."


@pytest.mark.asyncio
async def test_structured_generation_reports_reasoning_and_first_response(monkeypatch):
    captured: list[dict] = []
    _fake_ollama(monkeypatch, captured)
    activities: list[str] = []
    manager = LLMManager(on_activity=activities.append)

    raw = await manager.generate_structured(
        "planner",
        "system",
        "prompt",
        {"type": "object"},
    )

    assert raw == "The answer is 42."
    assert any("model is reasoning" in item for item in activities)
    assert any("model started responding" in item for item in activities)


@pytest.mark.asyncio
async def test_non_reasoning_model_is_not_asked_to_think(monkeypatch):
    captured: list[dict] = []
    _fake_ollama(monkeypatch, captured)
    manager = LLMManager()

    await manager._generate("qwen2.5-coder:7b-instruct", "sys", "prompt")

    assert "think" not in captured[0]


@pytest.mark.asyncio
async def test_think_rejection_falls_back_once_and_is_remembered(monkeypatch):
    """An Ollama build that rejects `think` must degrade to a normal call, not
    break every specialist request."""
    manager_module._THINK_UNSUPPORTED.discard("deepseek-r1:7b")
    captured: list[dict] = []
    _fake_ollama(monkeypatch, captured, status=400)
    manager = LLMManager()

    text = await manager._generate("deepseek-r1:7b", "sys", "prompt")

    assert text == "The answer is 42."           # answer still delivered
    assert captured[0].get("think") is True      # tried with think
    assert "think" not in captured[1]            # retried without it
    assert "deepseek-r1:7b" in manager_module._THINK_UNSUPPORTED

    # Remembered: the next call skips the doomed think attempt entirely.
    captured.clear()
    await manager._generate("deepseek-r1:7b", "sys", "prompt")
    assert len(captured) == 1 and "think" not in captured[0]
    manager_module._THINK_UNSUPPORTED.discard("deepseek-r1:7b")


@pytest.mark.asyncio
async def test_streamed_answer_never_leaks_reasoning_tokens(monkeypatch):
    """`on_token` drives the visible stream - it must see answer tokens only."""
    captured: list[dict] = []
    _fake_ollama(monkeypatch, captured)
    tokens: list[str] = []
    manager = LLMManager()

    text = await manager._generate_stream("deepseek-r1:7b", "sys", "prompt", tokens.append)

    assert "".join(tokens) == "The answer is 42."
    assert "Step one" not in "".join(tokens)
    assert text == "The answer is 42."


def test_repl_wires_reasoning_to_the_console(tmp_path: Path):
    """The manager the REPL builds must report reasoning, or the specialist
    path reasons invisibly (which is exactly what it used to do)."""
    console = Console(record=True, width=100)
    repl.write_trace_mode(tmp_path, "normal")
    manager = repl._make_llm_manager(None, console, tmp_path)

    assert manager.on_thinking is not None
    assert manager.on_activity is not None
    manager.on_thinking("deepseek-r1:7b", "I should check the index first.")
    manager.on_activity("still waiting for planner model qwen3:8b... 15s (reasoning)")
    out = console.export_text()
    assert "Reasoning" in out
    assert "check the index first" in out
    assert "still waiting for planner model" in out


# ---------------------------------------------------------------------------
# Gap G3: swallowed bookkeeping errors are counted and surfaced, not silent.
# ---------------------------------------------------------------------------


def test_swallowed_ledger_counts_and_snapshots():
    from shamsu.diagnostics import swallowed

    swallowed.reset()
    assert swallowed.total() == 0
    swallowed.record("repl.audit_prompt_route", OSError("disk full"))
    swallowed.record("repl.audit_prompt_route", OSError("disk full"))
    swallowed.record("repl.set_last_route", ValueError("bad state"))

    assert swallowed.total() == 3
    rows = swallowed.snapshot()
    assert rows[0] == ("repl.audit_prompt_route", 2, "OSError: disk full")
    swallowed.reset()


def test_swallowed_record_never_raises():
    from shamsu.diagnostics import swallowed

    class Unprintable(Exception):
        def __str__(self):
            raise RuntimeError("cannot stringify")

    swallowed.record("anywhere", Unprintable())   # must not raise
    swallowed.reset()


def test_context_show_reports_swallowed_errors(tmp_path: Path):
    from shamsu.diagnostics import swallowed

    swallowed.reset()
    console = Console(record=True, width=110)
    repl._handle_context("/context show", tmp_path, console)
    assert "all side channels healthy" in console.export_text()

    swallowed.record("repl.audit_prompt_route", OSError("read-only fs"))
    console = Console(record=True, width=110)
    repl._handle_context("/context show", tmp_path, console)
    out = console.export_text()
    assert "repl.audit_prompt_route" in out
    assert "read-only fs" in out
    swallowed.reset()
