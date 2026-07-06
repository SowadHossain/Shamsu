from __future__ import annotations

import json

import pytest

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.agents.chat_state import ChatState
from shamsu.cli.command_router import CommandRouter
from shamsu.tools.agent_tools import AgentToolRegistry, ToolResult


class FakeOllamaClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FailingOllamaClient:
    def __init__(self, exc: Exception):
        self.exc = exc
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        raise self.exc


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


def test_chat_state_hydrates_only_chat_messages():
    class FakeLogger:
        def tail(self, count=80):
            return [
                {
                    "event_type": "assistant.message",
                    "payload": {"message": "SHAMSU is ready."},
                },
                {
                    "event_type": "chat.message",
                    "payload": {"role": "assistant", "content": "SHAMSU is ready."},
                },
                {
                    "event_type": "chat.message",
                    "payload": {"role": "user", "content": "hello"},
                },
                {
                    "event_type": "chat.message",
                    "payload": {"role": "assistant", "content": "Hey."},
                },
            ]

    state = ChatState("system", session_logger=FakeLogger())

    contents = [message["content"] for message in state.messages()]
    assert "SHAMSU is ready." not in contents
    assert contents == ["system", "hello", "Hey."]


@pytest.mark.asyncio
async def test_agent_chat_loop_calls_read_file_then_answers(tmp_path):
    (tmp_path / "main.py").write_text("VALUE = 42\n", encoding="utf-8")
    client = FakeOllamaClient(
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "read-1",
                            "function": {
                                "name": "read_file",
                                "arguments": {"filepath": "main.py"},
                            },
                        }
                    ],
                }
            },
            {"message": {"content": "main.py defines VALUE = 42.", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(tmp_path, client=client, tools=tools).run("what is in main.py?")

    assert result.final == "main.py defines VALUE = 42."
    observation = json.loads(client.calls[1]["messages"][-1]["content"])
    assert observation["ok"] is True
    assert "VALUE = 42" in observation["data"]["content"]


@pytest.mark.asyncio
async def test_agent_chat_loop_calls_search_index_before_workspace_answer(tmp_path):
    class RecordingTools:
        def __init__(self):
            self.calls = []

        def tool_schemas(self):
            return AgentToolRegistry(tmp_path, approval_func=lambda _request: True).tool_schemas()

        def execute(self, name, arguments):
            self.calls.append((name, arguments))
            return ToolResult(
                True,
                "Found 1 result(s).",
                {"results": [{"file_path": "README.md", "content": "project notes"}]},
            )

    client = FakeOllamaClient(
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "search-1",
                            "function": {
                                "name": "search_index",
                                "arguments": {"query": "project notes"},
                            },
                        }
                    ],
                }
            },
            {"message": {"content": "README.md contains project notes.", "tool_calls": []}},
        ]
    )
    tools = RecordingTools()

    result = await AgentChatLoop(tmp_path, client=client, tools=tools).run("what says project notes?")

    assert result.final == "README.md contains project notes."
    assert tools.calls == [("search_index", {"query": "project notes"})]


@pytest.mark.asyncio
async def test_agent_chat_loop_returns_final_when_no_tool_calls(tmp_path):
    client = FakeOllamaClient([{"message": {"content": "Hello.", "tool_calls": []}}])

    result = await AgentChatLoop(tmp_path, client=client).run("hello")

    assert result.final == "Hello."
    assert result.tool_rounds == 0
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_agent_chat_loop_accepts_valid_json_action_fallback(tmp_path):
    (tmp_path / "main.py").write_text("print('json')\n", encoding="utf-8")
    client = FakeOllamaClient(
        [
            {
                "message": {
                    "content": json.dumps(
                        {"action": "read_file", "arguments": {"filepath": "main.py"}}
                    ),
                    "tool_calls": [],
                }
            },
            {"message": {"content": "Read main.py.", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(tmp_path, client=client, tools=tools).run("read main.py")

    assert result.final == "Read main.py."
    assert client.calls[1]["messages"][-1]["name"] == "read_file"


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
async def test_write_file_tool_overwrites_existing_without_a_flag(tmp_path):
    # The model must be able to UPDATE an existing file without remembering an
    # overwrite flag — otherwise small models get blocked and hallucinate success.
    (tmp_path / "hello.py").write_text("old = 1\n", encoding="utf-8")
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
                                "arguments": {"filepath": "hello.py", "content": "new = 2\n"},
                            },
                        }
                    ],
                }
            },
            {"message": {"content": "Updated hello.py.", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    await AgentChatLoop(tmp_path, client=client, tools=tools).run("update hello.py")

    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "new = 2\n"
    tool_message = client.calls[1]["messages"][-1]
    assert json.loads(tool_message["content"])["ok"] is True
    # The overwrite flag is no longer part of the model-facing schema.
    schemas = tools.tool_schemas()
    write_schema = next(s for s in schemas if s["function"]["name"] == "write_file")
    assert "overwrite" not in write_schema["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_agent_chat_loop_reports_tool_activity(tmp_path):
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
                                "arguments": {"filepath": "game.js", "content": "// game\n"},
                            },
                        }
                    ],
                }
            },
            {"message": {"content": "Done.", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    activity: list[str] = []

    await AgentChatLoop(
        tmp_path, client=client, tools=tools, on_activity=activity.append
    ).run("create game.js")

    assert "Writing game.js" in activity


@pytest.mark.asyncio
async def test_agent_chat_loop_emits_progress_for_tool_calls(tmp_path):
    class FakeProgress:
        def __init__(self):
            self.events = []

        def tool_start(self, tool_name, args_summary):
            self.events.append(("start", tool_name, args_summary))

        def tool_result(self, tool_name, result_summary, ok=True):
            self.events.append(("result", tool_name, ok, result_summary))

    client = FakeOllamaClient(
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {
                                "name": "read_file",
                                "arguments": {"filepath": "note.txt"},
                            },
                        }
                    ],
                }
            },
            {"message": {"content": "Read note.txt.", "tool_calls": []}},
        ]
    )
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    progress = FakeProgress()

    await AgentChatLoop(tmp_path, client=client, progress=progress).run("read note")

    assert progress.events[0] == ("start", "read_file", "file=note.txt")
    assert progress.events[1][0:3] == ("result", "read_file", True)


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


@pytest.mark.asyncio
async def test_agent_chat_loop_markdown_fallback_handles_generate_file_wording(tmp_path):
    client = FakeOllamaClient(
        [
            {"message": {"content": "```ts\nexport const value = 1;\n```", "tool_calls": []}},
            {"message": {"content": "Generated src/value.ts.", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(tmp_path, client=client, tools=tools).run("generate file src/value.ts")

    assert result.final == "Generated src/value.ts."
    assert (tmp_path / "src" / "value.ts").read_text(encoding="utf-8") == "export const value = 1;\n"
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_agent_chat_loop_markdown_fallback_writes_multiple_commented_files(tmp_path):
    client = FakeOllamaClient(
        [
            {
                "message": {
                    "content": (
                        "```ts\n// src/game/entities.ts\nexport type Player = { id: string };\n```\n"
                        "```tsx\n// src/ui/Hud.tsx\nexport function Hud() { return null; }\n```"
                    ),
                    "tool_calls": [],
                }
            },
            {"message": {"content": "Wrote the files.", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = await AgentChatLoop(tmp_path, client=client, tools=tools).run("fill the game requirements")

    assert result.final == "Wrote the files."
    assert (tmp_path / "src" / "game" / "entities.ts").read_text(encoding="utf-8") == (
        "export type Player = { id: string };\n"
    )
    assert (tmp_path / "src" / "ui" / "Hud.tsx").read_text(encoding="utf-8") == (
        "export function Hud() { return null; }\n"
    )
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_agent_chat_loop_handles_ollama_connection_failure(tmp_path):
    client = FailingOllamaClient(ConnectionError("Failed to connect to Ollama"))

    result = await AgentChatLoop(tmp_path, client=client).run("hhhhhhhhhhh")

    assert result.stopped
    assert "/models repair" in result.final
    assert "Ollama is not running" in result.final
    assert len(client.calls) == 1


def test_agent_tool_registry_blocks_dangerous_command(tmp_path):
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    result = tools.run_command("rm -rf /")

    assert not result.ok
    assert result.data["exit_code"] != 0
    assert "Blocked command" in result.data["stderr"]


def test_write_file_uses_existing_approval_path(tmp_path):
    approvals = []
    tools = AgentToolRegistry(tmp_path, approval_func=lambda request: approvals.append(request) or True)

    result = tools.write_file("approved.txt", "yes\n")

    assert result.ok is True
    assert approvals
    assert approvals[0].action_type == "file_write"
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "yes\n"


def test_run_command_uses_command_runner(tmp_path):
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    calls = []

    def fake_run(command, cwd):
        calls.append((command, cwd))
        return 0, "ok", ""

    tools.command_runner.run = fake_run

    result = tools.run_command("python -m pytest tests/ -q", ".")

    assert result.ok is True
    assert calls == [("python -m pytest tests/ -q", tmp_path.resolve())]


def test_large_tool_output_is_compact_truncated():
    result = ToolResult(True, "large", {"stdout": "x" * 7000})

    data = json.loads(result.to_json())

    assert len(data["data"]["stdout"]) < 6500
    assert "truncated" in data["data"]["stdout"]


@pytest.mark.asyncio
async def test_failed_write_final_does_not_claim_success(tmp_path):
    client = FakeOllamaClient(
        [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "write-1",
                            "function": {
                                "name": "write_file",
                                "arguments": {"filepath": "blocked.py", "content": "print('x')\n"},
                            },
                        }
                    ],
                }
            },
            {"message": {"content": "Updated blocked.py successfully.", "tool_calls": []}},
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: False)

    result = await AgentChatLoop(tmp_path, client=client, tools=tools).run("write blocked.py")

    assert result.stopped is True
    assert "could not confirm the file edit" in result.final
    assert not (tmp_path / "blocked.py").exists()


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
