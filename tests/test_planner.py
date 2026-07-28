from __future__ import annotations

import asyncio
import json

from shamsu.agents.planner import _decide_needs_input, _is_degenerate_question, create_plan
from shamsu.context.builder import ContextBuilder
from shamsu.types import ContextPack, LLMResponse, SearchResult


class _FakeLLM:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls: list[tuple[str, ContextPack]] = []

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        self.calls.append((specialist, pack))
        return LLMResponse(raw=self.raw, model_used="fake")


def _result(request: str) -> list[SearchResult]:
    return [
        SearchResult(file_path="app.py", language="python", line_start=1, line_end=3, content=request, score=1.0)
    ]


def test_create_plan_calls_the_planner_specialist():
    llm = _FakeLLM("  Touch app.py; verify with pytest.  ")

    asyncio.run(
        create_plan(llm, ContextBuilder(), _result("add a function"), goal="add a function", task_id="plan-1")
    )

    assert len(llm.calls) == 1
    specialist, pack = llm.calls[0]
    assert specialist == "planner"
    assert pack.specialist == "planner"
    assert pack.step_id == 1
    assert pack.task_id == "plan-1"


def test_create_plan_strips_whitespace_from_the_raw_response():
    llm = _FakeLLM("  Touch app.py; verify with pytest.  ")

    result = asyncio.run(
        create_plan(llm, ContextBuilder(), _result("add a function"), goal="add a function", task_id="plan-1")
    )

    assert result.text == "Touch app.py; verify with pytest."


def test_create_plan_includes_the_goal_in_the_request():
    llm = _FakeLLM("plan text")

    result = asyncio.run(
        create_plan(llm, ContextBuilder(), _result("fix the bug"), goal="fix the bug in app.py", task_id="plan-2")
    )

    assert "fix the bug in app.py" in result.pack.user_request


# -- degenerate clarification question guard ----------------------------------


def test_is_degenerate_question_rejects_the_live_repro_phrasing():
    """Live repro (2026-07-23): a small model asked to decide whether to ask a
    clarifying question echoed the meta-instruction back verbatim as the
    question itself, on two unrelated prompts (auth scheme, delete-backup)."""
    assert _is_degenerate_question("Do I need to ask a question before proceeding?")
    assert _is_degenerate_question("Should I ask a question here?")


def test_is_degenerate_question_accepts_a_real_concrete_question():
    assert not _is_degenerate_question(
        "Which authentication method should this app use: sessions, JWT, or OAuth?"
    )
    assert not _is_degenerate_question("Which file did you mean: users.db or users.db.bak?")


class _StructuredFakeLLM(_FakeLLM):
    def __init__(self, raw: str, structured_raw: str) -> None:
        super().__init__(raw)
        self.structured_raw = structured_raw
        self.structured_calls: list[tuple[str, str, str]] = []

    async def generate_structured(self, role, system, prompt, schema, **kwargs):
        self.structured_calls.append((role, system, prompt))
        return self.structured_raw


def _pack(goal: str) -> ContextPack:
    return ContextBuilder().pack(
        results=_result(goal), request=goal, task_id="plan-decision", step_id=1, specialist="planner"
    )


def test_decide_needs_input_discards_a_degenerate_question():
    llm = _StructuredFakeLLM(
        "plan text",
        json.dumps({"needs_input": True, "question": "Do I need to ask a question before proceeding?"}),
    )

    needs_input, question, options = asyncio.run(
        _decide_needs_input(llm, _pack("add authentication"), "add authentication")
    )

    assert needs_input is False
    assert question == ""
    assert options == []


def test_decide_needs_input_keeps_a_real_question():
    llm = _StructuredFakeLLM(
        "plan text",
        json.dumps(
            {
                "needs_input": True,
                "question": "Which authentication method should this use?",
                "options": [{"label": "Sessions"}, {"label": "JWT"}],
            }
        ),
    )

    needs_input, question, options = asyncio.run(
        _decide_needs_input(llm, _pack("add authentication"), "add authentication")
    )

    assert needs_input is True
    assert question == "Which authentication method should this use?"
    assert [option["label"] for option in options] == ["Sessions", "JWT"]
