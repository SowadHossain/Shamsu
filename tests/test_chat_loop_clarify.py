from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.session.manager import SessionManager
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import LLMResponse


class ScriptedClient:
    """Returns a queued list of model responses, recording the messages it was
    given each round so tests can assert on injected corrections."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = responses
        self.messages_seen: list[list[dict]] = []

    async def chat(self, model, messages, tools, stream, options):
        self.messages_seen.append([dict(message) for message in messages])
        return self._responses.pop(0)


class NoPlanLLM:
    async def run_specialist(self, specialist, pack):
        return LLMResponse(raw="", model_used="fake")


def _tool_call(name: str, arguments: dict) -> dict:
    return {"id": f"call_{name}", "function": {"name": name, "arguments": arguments}}


def _message(content: str = "", tool_calls: list[dict] | None = None) -> dict:
    return {"message": {"content": content, "tool_calls": tool_calls or []}}


def _loop(tmp_path: Path, client, session_logger=None) -> AgentChatLoop:
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True, session_logger=session_logger)
    return AgentChatLoop(
        tmp_path,
        client=client,
        tools=tools,
        llm=NoPlanLLM(),
        session_logger=session_logger,
    )


@pytest.mark.asyncio
async def test_ask_user_ends_turn_and_persists_pending_question(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Ask")
    client = ScriptedClient([
        _message(tool_calls=[_tool_call("ask_user", {
            "question": "Which file should I read?",
            "options": [
                {"label": "client/src/App.tsx", "description": "frontend"},
                {"label": "admin/src/App.tsx", "description": "admin"},
            ],
            "allow_free_text": True,
        })]),
    ])
    loop = _loop(tmp_path, client, session_logger=logger)

    result = await loop.run("read the file src/App.tsx")

    assert result.awaiting_user is True
    assert result.stopped is True
    assert "Which file should I read?" in result.final
    assert "1. client/src/App.tsx" in result.final

    pending = logger.get_pending_question()
    assert pending["question"] == "Which file should I read?"
    assert [option["label"] for option in pending["options"]] == [
        "client/src/App.tsx",
        "admin/src/App.tsx",
    ]
    assert pending["created_from_prompt"] == "read the file src/App.tsx"


@pytest.mark.asyncio
async def test_failed_read_with_multiple_candidates_surfaces_choice(tmp_path: Path):
    (tmp_path / "client" / "src").mkdir(parents=True)
    (tmp_path / "admin" / "src").mkdir(parents=True)
    (tmp_path / "client" / "src" / "App.tsx").write_text("1\n", encoding="utf-8")
    (tmp_path / "admin" / "src" / "App.tsx").write_text("2\n", encoding="utf-8")

    client = ScriptedClient([
        _message(tool_calls=[_tool_call("read_file", {"filepath": "src/App.tsx"})]),
        _message(content="Both files could match.", tool_calls=[_tool_call("ask_user", {
            "question": "Which App.tsx?",
            "options": [{"label": "client/src/App.tsx"}, {"label": "admin/src/App.tsx"}],
        })]),
    ])
    loop = _loop(tmp_path, client)

    result = await loop.run("read the file src/App.tsx")

    assert result.awaiting_user is True
    # The correction injected after the failed read named both candidates and
    # told the model to ask the user rather than guess.
    second_round_messages = "\n".join(str(message.get("content", "")) for message in client.messages_seen[1])
    assert "client/src/App.tsx" in second_round_messages
    assert "admin/src/App.tsx" in second_round_messages
    assert "ask_user" in second_round_messages


@pytest.mark.asyncio
async def test_failed_read_with_single_candidate_suggests_exact_path(tmp_path: Path):
    (tmp_path / "client" / "src").mkdir(parents=True)
    (tmp_path / "client" / "src" / "App.tsx").write_text("export default 1\n", encoding="utf-8")

    client = ScriptedClient([
        _message(tool_calls=[_tool_call("read_file", {"filepath": "src/App.tsx"})]),
        _message(tool_calls=[_tool_call("read_file", {"filepath": "client/src/App.tsx"})]),
        _message(content="Read the file.", tool_calls=[]),
    ])
    loop = _loop(tmp_path, client)

    result = await loop.run("read the file src/App.tsx")

    assert result.final == "Read the file."
    correction = "\n".join(str(message.get("content", "")) for message in client.messages_seen[1])
    assert "client/src/App.tsx" in correction
    assert "read_file" in correction


@pytest.mark.asyncio
async def test_prose_only_promise_does_not_end_turn(tmp_path: Path):
    client = ScriptedClient([
        _message(content="I will read app.py next."),
        _message(content="Done."),
    ])
    loop = _loop(tmp_path, client)

    result = await loop.run("look at app.py")

    # The empty promise must not be the final answer; the loop injects a
    # correction and runs one more round.
    assert result.final == "Done."
    assert len(client.messages_seen) == 2
    correction = "\n".join(str(message.get("content", "")) for message in client.messages_seen[1])
    assert "did not call a tool" in correction


@pytest.mark.asyncio
async def test_trace_callback_receives_clarification_event(tmp_path: Path):
    events: list[tuple[str, str]] = []

    def on_trace(event_type, message, payload=None, level="normal"):
        events.append((event_type, message))

    client = ScriptedClient([
        _message(tool_calls=[_tool_call("ask_user", {"question": "Which one?"})]),
    ])
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    loop = AgentChatLoop(tmp_path, client=client, tools=tools, llm=NoPlanLLM(), on_trace=on_trace)

    await loop.run("do the thing")

    assert any(event_type == "clarification.needed" for event_type, _ in events)
