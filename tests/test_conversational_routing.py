"""Dispatch coverage for the conversational short-circuit.

A greeting or bit of small talk ("hey how are you", "thanks") must get a
lightweight conversational reply - never the LLM task router, the tool-less QA
brain, or the agent loop. Before this guard, an indexed workspace forced every
greeting through `_route_prompt` -> qa -> the task harness, which answered "hey
how are you" with a fabricated plan citing a PRD and a "proceed with the QA
task?" prompt. These tests pin the routing so that cannot come back.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from rich.console import Console

from shamsu.cli import repl


def _quiet_console() -> Console:
    return Console(file=open("nul" if repl.sys.platform == "win32" else "/dev/null", "w"))


class _UnhandledOrchestrator:
    """Orchestrator stand-in that never handles the prompt, so dispatch falls
    through to the routing logic under test (no memory/model I/O)."""

    def __init__(self, *args, **kwargs) -> None:
        pass

    def run(self, user_input: str):
        return SimpleNamespace(
            handled=False, effective_input=user_input, context="", action=None
        )


def _wire_dispatch(monkeypatch) -> dict[str, object]:
    """Route a greeting through _handle_request with every heavy destination
    replaced: general chat records the call; the task-router paths explode."""
    recorded: dict[str, object] = {}
    monkeypatch.setattr(repl, "AgentOrchestrator", _UnhandledOrchestrator)
    monkeypatch.setattr(repl, "_make_llm_manager", lambda *a, **k: k.get("lightweight", False))

    async def _fake_general_chat(user_input, console, llm, **kwargs):
        recorded["general_chat"] = user_input
        recorded["kwargs"] = kwargs
        # llm here is the return of the patched _make_llm_manager, which echoes
        # the lightweight flag - so we can assert small talk used a lightweight
        # manager (no ctx indicator / reasoning dump).
        recorded["lightweight"] = llm

    monkeypatch.setattr(repl, "_run_general_chat", _fake_general_chat)

    async def _boom_route(*a, **k):
        raise AssertionError("small talk must not reach the LLM task router")

    async def _boom_qa(*a, **k):
        raise AssertionError("small talk must not reach the tool-less QA brain")

    async def _boom_agent(*a, **k):
        raise AssertionError("small talk must not reach the agent loop")

    monkeypatch.setattr(repl, "_route_prompt", _boom_route)
    monkeypatch.setattr(repl, "_run_qa", _boom_qa)
    monkeypatch.setattr(repl, "_run_agent_chat", _boom_agent)
    return recorded


def _dispatch(prompt: str, tmp_path) -> None:
    asyncio.run(
        repl._handle_request(
            prompt,
            tmp_path,
            _quiet_console(),
            SimpleNamespace(),  # web_tool - unused on this path
            SimpleNamespace(),  # browser_tool - unused on this path
        )
    )


def test_greeting_routes_to_general_chat(tmp_path, monkeypatch):
    recorded = _wire_dispatch(monkeypatch)
    _dispatch("hey how are you", tmp_path)
    assert recorded.get("general_chat") == "hey how are you"
    # Small talk must get a LIGHTWEIGHT manager and NO injected workspace
    # context - injecting agent_context made the model narrate about files
    # instead of just saying hi.
    assert recorded.get("lightweight") is True
    kwargs = recorded.get("kwargs") or {}
    assert not kwargs.get("extra_context")


def test_lightweight_manager_drops_indicator_and_reasoning_trace(tmp_path):
    """The lightweight manager backing small talk has no budget indicator and no
    reasoning-trace glimpse, so a greeting is one clean line. The normal manager
    keeps the reasoning glimpse for real specialist answers (QA, planner, ...)."""
    light = repl._make_llm_manager(None, _quiet_console(), tmp_path, lightweight=True)
    assert light.budget_manager is None
    assert light.on_thinking is None

    heavy = repl._make_llm_manager(None, _quiet_console(), tmp_path)
    assert heavy.on_thinking is not None


def test_bare_acknowledgement_routes_to_general_chat(tmp_path, monkeypatch):
    recorded = _wire_dispatch(monkeypatch)
    _dispatch("thanks", tmp_path)
    assert recorded.get("general_chat") == "thanks"


def test_greeting_plus_request_leaves_the_small_talk_branch(tmp_path, monkeypatch):
    """"hey, fix the login bug" is work, not small talk: it must NOT be diverted
    to general chat. _wire_dispatch makes every normal routing destination raise,
    so leaving the small-talk branch surfaces as one of those guards firing -
    proof the override did not swallow a real request. (The detector's own
    negative cases are pinned in test_repl_routing_detectors.)"""
    import pytest

    recorded = _wire_dispatch(monkeypatch)
    monkeypatch.setattr(repl, "_build_search_agent", lambda *a, **k: (SimpleNamespace(), False))
    with pytest.raises(AssertionError):
        _dispatch("hey, fix the login bug", tmp_path)
    assert "general_chat" not in recorded
