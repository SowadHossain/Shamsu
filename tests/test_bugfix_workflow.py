from __future__ import annotations

import asyncio

from shamsu.agents.bugfix_workflow import BugFixWorkflow
from shamsu.patch.engine import PatchEngine
from shamsu.types import ContextPack, LLMResponse, SearchResult


class _FakeSearch:
    def search(self, query, top_k=5, boost_paths=None):
        return [
            SearchResult(
                file_path="app.py", language="python", line_start=1, line_end=1,
                content="bug = 1", score=1.0,
            )
        ]

    def symbol_lookup(self, name):
        return []

    def fts_search(self, query, top_k=5):
        return self.search(query, top_k=top_k)

class _EmptySearch:
    def search(self, query, top_k=5, boost_paths=None):
        return []

    def symbol_lookup(self, name):
        return []

    def fts_search(self, query, top_k=5):
        return []

class _FakeLLM:
    """Answers "planner" calls with a fixed plan and "bugfix" calls from a
    queue of canned responses, recording every (specialist, pack) call."""

    def __init__(self, plan_text: str, bugfix_responses: list[str]) -> None:
        self.plan_text = plan_text
        self.bugfix_responses = list(bugfix_responses)
        self.calls: list[tuple[str, ContextPack]] = []

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        self.calls.append((specialist, pack))
        if specialist == "planner":
            return LLMResponse(raw=self.plan_text, model_used="fake")
        return LLMResponse(raw=self.bugfix_responses.pop(0), model_used="fake")


def _workflow(tmp_path, llm, search=None) -> BugFixWorkflow:
    return BugFixWorkflow(
        tmp_path,
        search=search or _FakeSearch(),
        llm=llm,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    )


def test_bugfix_calls_planner_before_bugfix_and_folds_plan_into_request(tmp_path):
    (tmp_path / "app.py").write_text("bug = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-bug = 1\n+bug = 2\n"
    llm = _FakeLLM(plan_text="Fix app.py line 1.", bugfix_responses=[diff])

    result = asyncio.run(_workflow(tmp_path, llm).run("Traceback: bug = 1 should be 2"))

    assert result.applied is True
    assert result.plan == "Fix app.py line 1."
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "bug = 2\n"

    specialists = [specialist for specialist, _ in llm.calls]
    assert specialists == ["planner", "bugfix"]
    bugfix_pack = llm.calls[1][1]
    assert "Fix app.py line 1." in bugfix_pack.user_request


def test_bugfix_repair_loop_reuses_the_plan_without_a_second_planner_call(tmp_path):
    (tmp_path / "app.py").write_text("bug = 1\n", encoding="utf-8")
    bad_diff = "this is not a unified diff"
    good_diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-bug = 1\n+bug = 2\n"
    llm = _FakeLLM(plan_text="Fix app.py line 1.", bugfix_responses=[bad_diff, good_diff])

    result = asyncio.run(_workflow(tmp_path, llm).run("Traceback: bug = 1 should be 2"))

    assert result.applied is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "bug = 2\n"

    planner_calls = [pack for specialist, pack in llm.calls if specialist == "planner"]
    assert len(planner_calls) == 1  # the repair retry does not re-invoke the planner

    repair_pack = llm.calls[-1][1]
    assert "Fix app.py line 1." in repair_pack.user_request


def test_bugfix_falls_back_to_full_rewrite_when_light_model_diff_has_bad_hunk_counts(tmp_path):
    (tmp_path / "app.py").write_text("bug = 1\n", encoding="utf-8")
    bad_diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,3 +1,3 @@\n"
        "-bug = 1\n"
        "+bug = 2\n"
    )
    llm = _FakeLLM(
        plan_text="Fix app.py line 1.",
        bugfix_responses=[bad_diff, bad_diff, bad_diff, "bug = 2\n"],
    )

    result = asyncio.run(_workflow(tmp_path, llm, search=_EmptySearch()).run("AssertionError: bug should be 2"))

    assert result.applied is True
    assert result.used_full_rewrite is True
    assert result.changed_files == ["app.py"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "bug = 2\n"
