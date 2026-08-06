"""Gap A2 (corrected): continuity across routes.

The agent loop DOES remember its own turns - `ChatState` hydrates from
`messages.jsonl` on construction, so a fresh loop per prompt still sees the
previous ones. (The original gap doc claimed otherwise; it was wrong.)

The real hole: only the loop ever WROTE that transcript, via
`chat_state._append`. Every route that answers without the loop - QA, direct
code, PRD summary - logged an `assistant.message` event but never appended to
the transcript. So "what does game.js do?" (QA) followed by "add a pause
button" (agent loop) left the agent blind to the exchange that just happened.

`_audit_simple_turn` now records both sides of a non-loop turn.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.cli.repl import _audit_simple_turn
from shamsu.session.manager import SessionManager
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import LLMResponse


class _ScriptedClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.messages_seen: list[list[dict]] = []

    async def chat(self, model, messages, tools, stream, options):  # noqa: ANN001
        self.messages_seen.append([dict(message) for message in messages])
        return self._responses.pop(0)


class _NoPlanLLM:
    async def run_specialist(self, specialist, pack):  # noqa: ANN001
        return LLMResponse(raw="", model_used="fake")


def _loop(workspace: Path, client, session_logger) -> AgentChatLoop:
    return AgentChatLoop(
        workspace,
        client=client,
        tools=AgentToolRegistry(
            workspace, approval_func=lambda _r: True, session_logger=session_logger
        ),
        llm=_NoPlanLLM(),
        session_logger=session_logger,
    )


def _answer(client) -> dict:
    return {"message": {"content": "ok", "tool_calls": []}}


def test_agent_loop_remembers_its_own_previous_turn(tmp_path: Path):
    """Characterization: this already worked via ChatState hydration. Pinned so
    a refactor that drops `session_logger` from the loop can't silently
    reintroduce amnesia."""
    logger = SessionManager(tmp_path).create_session("Own")

    first = _ScriptedClient([{"message": {"content": "I built snake.js", "tool_calls": []}}])
    asyncio.run(_loop(tmp_path, first, logger).run("build a snake game"))

    second = _ScriptedClient([{"message": {"content": "Now blue", "tool_calls": []}}])
    asyncio.run(_loop(tmp_path, second, logger).run("now make the snake blue"))

    seen = [(m["role"], m["content"]) for m in second.messages_seen[0]]
    assert ("user", "build a snake game") in seen
    assert any(role == "assistant" and "snake.js" in text for role, text in seen)


def test_non_loop_turn_is_visible_to_the_next_agent_run(tmp_path: Path):
    """The actual fix: a QA/direct-code answer must reach the agent loop."""
    logger = SessionManager(tmp_path).create_session("CrossRoute")

    _audit_simple_turn(
        tmp_path, logger, "direct_code", "what does game.js do?", "game.js runs the render loop."
    )

    client = _ScriptedClient([{"message": {"content": "Added.", "tool_calls": []}}])
    asyncio.run(_loop(tmp_path, client, logger).run("now add a pause button"))

    seen = [(m["role"], m["content"]) for m in client.messages_seen[0]]
    assert ("user", "what does game.js do?") in seen
    assert any(role == "assistant" and "render loop" in text for role, text in seen)


def test_simple_turn_records_both_sides_in_order(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Order")
    _audit_simple_turn(tmp_path, logger, "qa", "is there a test suite?", "Yes, under tests/.")

    records = logger.read_messages()
    assert [(r["role"], r["content"]) for r in records] == [
        ("user", "is there a test suite?"),
        ("assistant", "Yes, under tests/."),
    ]


@pytest.mark.parametrize(
    "prompt, final",
    [("", "an answer with no prompt"), ("a prompt with no answer", ""), ("", "")],
)
def test_simple_turn_skips_empty_sides(tmp_path: Path, prompt: str, final: str):
    """A half-turn must not create a dangling user/assistant message that would
    confuse hydration."""
    logger = SessionManager(tmp_path).create_session("Empty")
    _audit_simple_turn(tmp_path, logger, "qa", prompt, final)

    roles = [r["role"] for r in logger.read_messages()]
    assert roles == [role for role, text in (("user", prompt), ("assistant", final)) if text.strip()]


def test_simple_turn_survives_no_session_logger(tmp_path: Path):
    """Best-effort: bookkeeping must never break a response."""
    _audit_simple_turn(tmp_path, None, "qa", "q", "a")
