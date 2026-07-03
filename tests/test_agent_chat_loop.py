from __future__ import annotations

import json

import pytest

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.agents.chat_state import ChatState
from shamsu.cli.command_router import CommandRouter
from shamsu.tools.agent_tools import AgentToolRegistry


class FakeOllamaClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_chat_state_appends_messages_in_order(tmp_path):
    state = ChatState("system", hydrate=False)

    state.append_user("make a file")
    state.append_assistant("", tool_calls=[{"function": {"name": "write_file", "arguments": {}}}])
    state.append_tool("write_file", "write_file", '{"ok": true}')

    assert [message["role"] for message in state.messages()] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]


@pytest.mark.asyncio
async def test_agent_chat_loop_executes_native_write_file_tool(tmp_path):
    client = FakeOllamaClient(
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {
                                "name": "write_file",
                                "arguments": {
                                    "filepath": "hello.py",
                                    "content": "print('hi')\n",
                                },
                            },
                        }
                    ],
                }
            },
            {"message": {"content": "Created hello.py.", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(tmp_path, client=client, tools=tools).run("create hello.py")

    assert result.final == "Created hello.py."
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert len(client.calls) == 2
    second_messages = client.calls[1]["messages"]
    assert second_messages[-1]["role"] == "tool"
    assert json.loads(second_messages[-1]["content"])["ok"] is True


@pytest.mark.asyncio
async def test_agent_chat_loop_markdown_fallback_writes_file(tmp_path):
    client = FakeOllamaClient(
        [
            {"message": {"content": "```python\nprint('fallback')\n```", "tool_calls": []}},
            {"message": {"content": "Created fallback.py.", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(tmp_path, client=client, tools=tools).run("create fallback.py")

    assert result.final == "Created fallback.py."
    assert (tmp_path / "fallback.py").read_text(encoding="utf-8") == "print('fallback')\n"
    assert len(client.calls) == 2


def test_agent_tool_registry_blocks_dangerous_command(tmp_path):
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = tools.run_command("rm -rf /")

    assert not result.ok
    assert result.data["exit_code"] != 0
    assert "Blocked command" in result.data["stderr"]


def test_slash_command_router_rejects_unknown_with_suggestion():
    router = CommandRouter(("/help", "/index", "/search "))

    route = router.route("/inde")

    assert not route.valid
    assert "/index" in route.suggestions


def test_slash_command_router_accepts_known_command():
    router = CommandRouter(("/help", "/index", "/search "))

    route = router.route("/search auth")

    assert route.valid
    assert route.normalized == "search auth"
