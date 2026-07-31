from __future__ import annotations

import sys
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
async def test_ambiguous_edit_recovers_with_context_and_verifies(tmp_path: Path):
    source = (
        "def add(a, b):\n"
        "    return a + b\n\n"
        "def subtract(a, b):\n"
        "    return a + b\n\n"
        "print(subtract(5, 2))\n"
    )
    (tmp_path / "calc.py").write_text(source, encoding="utf-8")
    command = f'"{sys.executable}" calc.py'

    class RecoveryClient:
        def __init__(self) -> None:
            self.messages_seen: list[list[dict]] = []
            self.responses = [
                {"message": {"content": "", "tool_calls": [{
                    "id": "read", "function": {
                        "name": "read_file", "arguments": {"filepath": "calc.py"}
                    }
                }]}},
                {"message": {"content": "", "tool_calls": [{
                    "id": "ambiguous", "function": {
                        "name": "edit_file", "arguments": {
                            "filepath": "calc.py",
                            "old_string": "return a + b",
                            "new_string": "return a - b",
                        }
                    }
                }]}},
                {"message": {"content": "", "tool_calls": [{
                    "id": "contextual", "function": {
                        "name": "edit_file", "arguments": {
                            "filepath": "calc.py",
                            "old_string": "def subtract(a, b):\n    return a + b",
                            "new_string": "def subtract(a, b):\n    return a - b",
                        }
                    }
                }]}},
                {"message": {"content": "", "tool_calls": [{
                    "id": "verify", "function": {
                        "name": "run_command", "arguments": {"command": command}
                    }
                }]}},
                {"message": {"content": "Fixed and verified: subtract(5, 2) prints 3.", "tool_calls": []}},
            ]

        async def chat(self, **_kwargs):
            self.messages_seen.append(list(_kwargs["messages"]))
            return self.responses.pop(0)

    client = RecoveryClient()
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        llm=FakePlannerLLM("Fix only subtract, then run calc.py."),
    )

    result = await loop.run("Fix subtract in calc.py and run it.")

    assert "return a - b" in (tmp_path / "calc.py").read_text(encoding="utf-8")
    assert result.stopped is False
    assert "prints 3" in result.final
    correction_messages = [
        message["content"]
        for message in client.messages_seen[2]
        if message.get("role") == "user"
    ]
    assert any("Exact candidate blocks" in message for message in correction_messages)


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
async def test_unspecified_auth_approach_is_deterministic_even_when_planner_misses(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Deterministic auth")
    llm = StructuredPlannerLLM({"needs_input": False})
    loop = AgentChatLoop(
        tmp_path,
        client=NeverCalledClient(),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=llm,
        session_logger=logger,
    )

    result = await loop.run("Add authentication to app.py.")

    assert result.awaiting_user is True
    assert "Which authentication approach" in result.final
    assert "Server sessions" in result.final
    assert "JWT" in result.final
    assert logger.get_pending_question()["source"] == "planner_upfront"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "task_prompt",
    [
        "Add JWT authentication to app.py.",
        "Fix the authentication bug in app.py.",
        "Explain how authentication works in app.py.",
    ],
)
async def test_auth_tasks_with_a_decided_approach_or_no_design_choice_continue(
    tmp_path: Path, task_prompt: str
):
    llm = StructuredPlannerLLM({"needs_input": False})
    client = FakeOllamaClient()
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=llm,
    )

    result = await loop.run(task_prompt)

    assert result.awaiting_user is False
    assert client.messages_seen


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


# ---------------------------------------------------------------------------
# C1 remainder: the chat loop's planner call was context-blind (results=[]),
# the same trap that made plan_mode hallucinate. With no results and a
# workspace, create_plan now injects a real-files listing into its request.
# Measured live: 1/3 grounded -> 3/3 grounded on the chat_plan eval.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_blind_planner_gets_a_real_files_listing(tmp_path: Path):
    from shamsu.agents.planner import create_plan
    from shamsu.context.builder import ContextBuilder

    (tmp_path / "game.js").write_text("// loop", encoding="utf-8")
    (tmp_path / "index.html").write_text("<html>", encoding="utf-8")
    llm = FakePlannerLLM("plan")

    await create_plan(llm, ContextBuilder(), results=[], goal="add pause", task_id="t", workspace=tmp_path)

    request = llm.calls[0][1].user_request
    assert "game.js" in request and "index.html" in request
    assert "never invent" in request


@pytest.mark.asyncio
async def test_planner_with_real_results_gets_no_listing(tmp_path: Path):
    """Search results ARE the grounding; the listing must not crowd them."""
    from shamsu.agents.planner import create_plan
    from shamsu.context.builder import ContextBuilder
    from shamsu.types import SearchResult

    (tmp_path / "game.js").write_text("// loop", encoding="utf-8")
    llm = FakePlannerLLM("plan")
    results = [SearchResult(file_path="game.js", language="js", line_start=1, line_end=1, content="// loop", score=1.0)]

    await create_plan(llm, ContextBuilder(), results=results, goal="add pause", task_id="t", workspace=tmp_path)

    assert "Real files in the workspace" not in llm.calls[0][1].user_request


@pytest.mark.asyncio
async def test_planner_without_workspace_stays_blind(tmp_path: Path):
    """workspace=None preserves the old behavior exactly (callers that never
    pass one are unchanged)."""
    from shamsu.agents.planner import create_plan
    from shamsu.context.builder import ContextBuilder

    llm = FakePlannerLLM("plan")
    await create_plan(llm, ContextBuilder(), results=[], goal="add pause", task_id="t")

    assert "Real files in the workspace" not in llm.calls[0][1].user_request
