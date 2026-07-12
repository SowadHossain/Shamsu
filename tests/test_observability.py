"""Tests for G9 observability: the working trace now surfaces search_index
queries + hits and a dim reasoning glimpse at normal verbosity, and `/context
show` reports the observability snapshot."""
from __future__ import annotations

from pathlib import Path

import pytest
from rich.console import Console

import shamsu.cli.repl as repl
from shamsu.agents.chat_loop import AgentChatLoop, _search_summary, _thinking_preview
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
