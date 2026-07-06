from __future__ import annotations

import pytest

from shamsu.agents.chat_loop import (
    LONG_RUNNING_MAX_TOOL_ROUNDS,
    _MAX_REPEATED_CALLS,
    AgentChatLoop,
)
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


def _write_file_call(filepath: str, content: str, call_id: str = "call-1"):
    return {
        "message": {
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "function": {
                        "name": "write_file",
                        "arguments": {"filepath": filepath, "content": content},
                    },
                }
            ],
        }
    }


@pytest.mark.asyncio
async def test_default_mode_stops_on_repetition_guard(tmp_path):
    client = FakeOllamaClient([_list_files_call() for _ in range(_MAX_REPEATED_CALLS)])
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(tmp_path, client=client, tools=tools).run("list files forever")

    assert result.stopped is True
    assert "kept repeating" in result.final
    assert len(client.calls) == _MAX_REPEATED_CALLS


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
async def test_repetition_guard_allows_multiple_writes_to_different_files(tmp_path):
    client = FakeOllamaClient(
        [
            _write_file_call("a.txt", "a\n", "write-a"),
            _write_file_call("b.txt", "b\n", "write-b"),
            {"message": {"content": "Done writing.", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(tmp_path, client=client, tools=tools, long_running=True).run("write two files")

    assert result.stopped is False
    assert result.final == "Done writing."
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "a\n"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "b\n"


@pytest.mark.asyncio
async def test_long_running_mode_allows_one_repeat_and_continues(tmp_path):
    client = FakeOllamaClient(
        [
            _list_files_call(),
            _list_files_call(),  # the repeat
            {"message": {"content": "Done listing.", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(
        tmp_path, client=client, tools=tools, long_running=True,
    ).run("list files")

    assert result.stopped is False
    assert result.final == "Done listing."
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_long_running_mode_stops_after_repeats_exceed_limit(tmp_path):
    client = FakeOllamaClient([_list_files_call() for _ in range(_MAX_REPEATED_CALLS)])
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(
        tmp_path, client=client, tools=tools, long_running=True,
    ).run("list files forever")

    assert result.stopped is True
    assert "kept repeating" in result.final
    assert len(client.calls) == _MAX_REPEATED_CALLS


@pytest.mark.asyncio
async def test_long_running_mode_logs_stuck_event(tmp_path):
    client = FakeOllamaClient([_list_files_call() for _ in range(_MAX_REPEATED_CALLS)])
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
        tmp_path, client=client, tools=tools, session_logger=logger, long_running=True,
    ).run("list files forever")

    assert "agent.stuck" in logger.events


@pytest.mark.asyncio
async def test_failed_write_injects_correction_and_reprompts(tmp_path):
    # A denied/failed write must be made loud so the model re-writes the full
    # file instead of assuming success and moving on.
    (tmp_path / "x.py").write_text("old\n", encoding="utf-8")
    client = FakeOllamaClient(
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "function": {
                                "name": "write_file",
                                "arguments": {"filepath": "x.py", "content": "new\n"},
                            },
                        }
                    ],
                }
            },
            {"message": {"content": "ok", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: False)

    await AgentChatLoop(
        tmp_path, client=client, tools=tools, long_running=True,
    ).run("edit x.py")

    assert len(client.calls) == 2
    second_turn_messages = client.calls[1]["messages"]
    assert any("did NOT succeed" in str(msg.get("content", "")) for msg in second_turn_messages)
    assert (tmp_path / "x.py").read_text(encoding="utf-8") == "old\n"
