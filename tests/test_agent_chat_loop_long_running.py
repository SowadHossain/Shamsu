from __future__ import annotations

import pytest

from shamsu.agents.chat_loop import LONG_RUNNING_MAX_TOOL_ROUNDS, AgentChatLoop
from shamsu.tools.agent_tools import AgentToolRegistry


class FakeOllamaClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _list_files_call(call_id: str = "call-1"):
    return {
        "message": {
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {"name": "list_files", "arguments": {"path": "."}},
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_default_mode_hard_stops_at_five_rounds_on_repetition(tmp_path):
    """Confirms today's default (long_running=False) behavior is unchanged:
    no repetition guard, just the fixed round cap."""
    client = FakeOllamaClient([_list_files_call() for _ in range(5)])
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(tmp_path, client=client, tools=tools).run("list files forever")

    assert result.stopped is True
    assert "I stopped after 5 tool rounds" in result.final
    assert len(client.calls) == 5


@pytest.mark.asyncio
async def test_long_running_mode_uses_higher_ceiling_for_non_repeating_calls(tmp_path):
    """More than 5 distinct (non-repeating) tool calls succeed in
    long_running mode instead of being cut off at the old 5-round cap."""
    calls = [_list_files_call(f"call-{i}") for i in range(8)]
    # vary the path argument each time so the repetition guard never trips
    for i, call in enumerate(calls):
        call["message"]["tool_calls"][0]["function"]["arguments"] = {"path": f"dir-{i}"}
    calls.append({"message": {"content": "Done listing.", "tool_calls": []}})
    client = FakeOllamaClient(calls)
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(
        tmp_path, client=client, tools=tools, long_running=True, clarify_prompt=None
    ).run("list several directories")

    assert result.final == "Done listing."
    assert len(client.calls) == 9  # 8 tool rounds + 1 final content-only round
    assert LONG_RUNNING_MAX_TOOL_ROUNDS > 8


@pytest.mark.asyncio
async def test_long_running_mode_repetition_guard_asks_clarifying_question(tmp_path):
    client = FakeOllamaClient([_list_files_call(), _list_files_call()])
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    asked = []

    def fake_clarify(question: str) -> str:
        asked.append(question)
        return "Stop trying that, list the tests/ folder instead."

    result = await AgentChatLoop(
        tmp_path, client=client, tools=tools, long_running=True, clarify_prompt=fake_clarify,
    ).run("list files forever")

    assert len(asked) == 1
    assert "list_files" in asked[0]
    assert result.stopped is True
    assert "Stop trying that" in result.final
    # Only one round actually executed the tool before the repeat was caught.
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_long_running_mode_without_clarify_prompt_stops_plainly_on_repetition(tmp_path):
    client = FakeOllamaClient([_list_files_call(), _list_files_call()])
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(
        tmp_path, client=client, tools=tools, long_running=True, clarify_prompt=None,
    ).run("list files forever")

    assert result.stopped is True
    assert "kept repeating the same action" in result.final


@pytest.mark.asyncio
async def test_long_running_mode_logs_clarify_event(tmp_path):
    client = FakeOllamaClient([_list_files_call(), _list_files_call()])
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    class RecordingLogger:
        def __init__(self):
            self.events = []

        def log(self, event_type, payload, summary, workflow_id=None):
            self.events.append(event_type)

        def tail(self, count=80):
            return []

    logger = RecordingLogger()
    await AgentChatLoop(
        tmp_path, client=client, tools=tools, session_logger=logger,
        long_running=True, clarify_prompt=lambda _q: "try something else",
    ).run("list files forever")

    assert "agent.clarify" in logger.events
