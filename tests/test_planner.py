from __future__ import annotations

import asyncio

from shamsu.agents.planner import create_plan
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
