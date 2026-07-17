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


# ---------------------------------------------------------------------------
# Gap J6: ask upfront, before any work starts.
#
# The prompt-only nudge toward ask_user (J3) measurably did NOT make a 7B model
# ask about a design decision - mid-loop, a model that can always do *something*
# just does it. The planner call already runs on every request, so it decides
# "is this the user's call?" before work starts, at no extra model call.
# ---------------------------------------------------------------------------


import json  # noqa: E402

from shamsu.session.manager import SessionManager  # noqa: E402


class StructuredPlannerLLM:
    """A planner that supports schema-constrained output (the real LLMManager
    seam), unlike FakePlannerLLM which only has run_specialist."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.structured_calls = 0
        self.specialist_calls = 0

    async def generate_structured(self, role, system, prompt, schema, **kwargs):
        self.structured_calls += 1
        return json.dumps(self.payload)

    async def run_specialist(self, specialist, pack):
        self.specialist_calls += 1
        return LLMResponse(raw="fallback plan", model_used="fake")


class NeverCalledClient:
    async def chat(self, model, messages, tools, stream, options):
        raise AssertionError("the tool loop must not run when the planner asks first")


@pytest.mark.asyncio
async def test_planner_decision_asks_before_any_work_happens(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Upfront")
    llm = StructuredPlannerLLM(
        {
            "plan": "Add auth to app.py.",
            "needs_input": True,
            "question": "Should authentication use server sessions or JWT?",
            "options": [
                {"label": "Sessions", "description": "server-side, simpler"},
                {"label": "JWT", "description": "stateless"},
            ],
        }
    )
    loop = AgentChatLoop(
        tmp_path,
        client=NeverCalledClient(),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=llm,
        session_logger=logger,
    )

    result = await loop.run("Add authentication to app.py.")

    assert result.awaiting_user is True
    assert "sessions or JWT" in result.final
    assert "1. Sessions" in result.final
    # The question survives the turn so the next reply resumes the work.
    pending = logger.get_pending_question()
    assert pending["source"] == "planner_upfront"
    assert pending["created_from_prompt"] == "Add authentication to app.py."


@pytest.mark.asyncio
async def test_a_clear_task_is_not_interrupted(tmp_path: Path):
    """The other half of the threshold: asking about everything is its own
    failure. needs_input=false must go straight to work."""
    llm = StructuredPlannerLLM({"needs_input": False})
    client = FakeOllamaClient()
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=llm,
    )

    result = await loop.run("Create greet.py with a greet function.")

    assert result.awaiting_user is False
    assert client.messages_seen, "the tool loop should have run"
    # The plan text still comes from the proven free-text planner call, NOT the
    # decision call - folding the plan into the decision's JSON measurably
    # degraded it (create_file 3/3 -> 0/3).
    assert llm.specialist_calls == 1
    assert "fallback plan" in client.messages_seen[0][-1]["content"]


@pytest.mark.asyncio
async def test_needs_input_without_a_question_is_ignored(tmp_path: Path):
    """A bare needs_input flag with nothing to ask would stall the turn on an
    empty prompt - worse than just doing the work."""
    llm = StructuredPlannerLLM({"plan": "Do it.", "needs_input": True, "question": "  "})
    client = FakeOllamaClient()
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=llm,
    )

    result = await loop.run("do the thing")

    assert result.awaiting_user is False
    assert client.messages_seen


@pytest.mark.asyncio
async def test_ask_upfront_can_be_disabled(tmp_path: Path, monkeypatch):
    import shamsu.agents.chat_loop as chat_loop_module

    monkeypatch.setattr(chat_loop_module, "_ASK_UPFRONT_ENABLED", False)
    llm = StructuredPlannerLLM(
        {"plan": "p", "needs_input": True, "question": "Sessions or JWT?"}
    )
    client = FakeOllamaClient()
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=llm,
    )

    result = await loop.run("Add authentication.")

    assert result.awaiting_user is False
    assert client.messages_seen


@pytest.mark.asyncio
async def test_planner_falls_back_to_free_text_without_schema_support(tmp_path: Path):
    """An LLM with no generate_structured (test doubles, narrower interfaces)
    must keep working on the original path, not crash."""
    llm = FakePlannerLLM("Plain prose plan.")
    client = FakeOllamaClient()
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=llm,
    )

    result = await loop.run("list the files")

    assert result.awaiting_user is False
    assert llm.calls, "the free-text planner path should have been used"
    assert "Plain prose plan." in client.messages_seen[0][-1]["content"]


@pytest.mark.asyncio
async def test_a_broken_structured_planner_does_not_break_the_run(tmp_path: Path):
    class BrokenStructuredLLM(FakePlannerLLM):
        async def generate_structured(self, *a, **k):
            raise RuntimeError("schema call exploded")

    llm = BrokenStructuredLLM("Fallback prose plan.")
    client = FakeOllamaClient()
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=llm,
    )

    result = await loop.run("list the files")

    assert result.awaiting_user is False
    assert "Fallback prose plan." in client.messages_seen[0][-1]["content"]
