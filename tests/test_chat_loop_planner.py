from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import ContextPack, LLMResponse


class FakeOllamaClient:
    """One tool call, then a plain-text final answer - the minimal shape
    AgentChatLoop's tool-calling loop expects."""

    def __init__(self) -> None:
        self._responses = [
            {"message": {"content": "", "tool_calls": [
                {"id": "call_1", "function": {"name": "list_files", "arguments": {"path": "."}}}
            ]}},
            {"message": {"content": "Done listing files.", "tool_calls": []}},
        ]
        self.messages_seen: list[list[dict]] = []

    async def chat(self, model, messages, tools, stream, options):
        self.messages_seen.append(messages)
        return self._responses.pop(0)


class FakePlannerLLM:
    def __init__(self, plan_text: str) -> None:
        self.plan_text = plan_text
        self.calls: list[tuple[str, ContextPack]] = []

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        self.calls.append((specialist, pack))
        return LLMResponse(raw=self.plan_text, model_used="fake")


@pytest.mark.asyncio
async def test_planner_runs_once_per_run_call_before_the_tool_loop(tmp_path: Path):
    client = FakeOllamaClient()
    llm = FakePlannerLLM("List the current directory, nothing else.")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    loop = AgentChatLoop(tmp_path, client=client, tools=tools, llm=llm)

    result = await loop.run("list the files here")

    assert result.final == "Done listing files."
    assert [specialist for specialist, _ in llm.calls] == ["planner"]
    # The plan text reaches the tool-calling model's first request.
    first_call_messages = client.messages_seen[0]
    user_messages = [m["content"] for m in first_call_messages if m.get("role") == "user"]
    assert any("List the current directory, nothing else." in content for content in user_messages)


@pytest.mark.asyncio
async def test_planner_failure_does_not_block_the_chat_loop(tmp_path: Path):
    class BrokenLLM:
        async def run_specialist(self, specialist, pack):
            raise RuntimeError("planner backend unavailable")

    client = FakeOllamaClient()
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    loop = AgentChatLoop(tmp_path, client=client, tools=tools, llm=BrokenLLM())

    result = await loop.run("list the files here")

    assert result.final == "Done listing files."


@pytest.mark.asyncio
async def test_two_separate_run_calls_each_get_their_own_planner_call(tmp_path: Path):
    """One planner call per top-level request, not per tool-call round -
    calling .run() twice (as _run_agent_chat does per user prompt) should
    invoke the planner twice, not accumulate across calls."""
    llm = FakePlannerLLM("Plan.")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    first_client = FakeOllamaClient()
    await AgentChatLoop(tmp_path, client=first_client, tools=tools, llm=llm).run("first request")
    second_client = FakeOllamaClient()
    await AgentChatLoop(tmp_path, client=second_client, tools=tools, llm=llm).run("second request")

    assert len(llm.calls) == 2
