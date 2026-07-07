from __future__ import annotations

import asyncio

from shamsu.agents.code_edit_workflow import CodeEditWorkflow
from shamsu.agents.rewrite_fallback import (
    lenient_diff_target_paths,
    rewrite_files_fully,
    strip_code_fences,
)
from shamsu.context.builder import ContextBuilder
from shamsu.patch.engine import PatchEngine
from shamsu.types import ContextPack, LLMResponse, SearchResult


def test_lenient_diff_target_paths_survives_malformed_hunks():
    diff = (
        "--- a/src/game/rules.ts\n"
        "+++ b/src/game/rules.ts\n"
        "@@ this hunk header is broken @@\n"
        "+some line\n"
    )
    assert lenient_diff_target_paths(diff) == ["src/game/rules.ts"]


def test_strip_code_fences_removes_markdown():
    assert strip_code_fences("```ts\nconst x = 1;\n```") == "const x = 1;"
    assert strip_code_fences("no fences here") == "no fences here"


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[str] = []

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        self.calls.append(specialist)
        return LLMResponse(raw=self.content, model_used="fake")


class _AllowPatchEngine:
    def validate_diff(self, diff_text):
        self.diff_text = diff_text
        return True, None

    def apply(self, diff_text, workspace_root):
        # Minimal test double for the rewrite helper; workflow tests use the real
        # patch engine path.
        target = workspace_root / "app.py"
        target.write_text("new = 2\n", encoding="utf-8")
        return True


def test_rewrite_files_fully_overwrites_target(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old = 1\n", encoding="utf-8")
    llm = _FakeLLM("```python\nnew = 2\n```")

    changed = asyncio.run(
        rewrite_files_fully(
            llm=llm,
            context_builder=ContextBuilder(),
            patch_engine=_AllowPatchEngine(),
            workspace_root=tmp_path,
            request="make it new",
            target_paths=["app.py"],
            specialist="coder",
        )
    )

    assert changed == ["app.py"]
    assert target.read_text(encoding="utf-8") == "new = 2\n"


def test_rewrite_files_fully_rejects_paths_outside_workspace(tmp_path):
    llm = _FakeLLM("hacked")
    changed = asyncio.run(
        rewrite_files_fully(
            llm=llm,
            context_builder=ContextBuilder(),
            patch_engine=_AllowPatchEngine(),
            workspace_root=tmp_path,
            request="x",
            target_paths=["../escape.py"],
            specialist="coder",
        )
    )
    assert changed == []
    assert not (tmp_path.parent / "escape.py").exists()


class _FakeSearch:
    def search(self, query, top_k=8, boost_paths=None):
        return [
            SearchResult(
                file_path="app.py", language="python", line_start=1, line_end=1,
                content="old = 1", score=1.0,
            )
        ]

    def symbol_lookup(self, name):
        return []

    def fts_search(self, query, top_k=5):
        return self.search(query, top_k=top_k)


class _BrokenDiffLLM:
    """Emits a diff whose hunk counts are wrong (what an 8B model does) on its
    first "coder" call, then a clean full-file rewrite on the fallback call.
    Planner calls (CodeEditWorkflow now makes one before "coder") are answered
    with a harmless plan string and don't count toward the coder call index -
    this fake is about the coder's diff-repair fallback, not planning."""

    def __init__(self) -> None:
        self.coder_calls = 0

    async def run_specialist(self, specialist, pack):
        if specialist == "planner":
            return LLMResponse(raw="Edit app.py to set new = 2.", model_used="fake")
        self.coder_calls += 1
        if self.coder_calls == 1:
            broken = (
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1,5 +1,5 @@\n"  # claims 5 lines that aren't there
                "-old = 1\n"
                "+new = 2\n"
            )
            return LLMResponse(raw=broken, model_used="fake")
        return LLMResponse(raw="new = 2\n", model_used="fake")

    async def route(self, prompt, project_summary):  # pragma: no cover
        raise RuntimeError("offline")


def test_code_edit_falls_back_to_full_rewrite_on_bad_diff(tmp_path):
    (tmp_path / "app.py").write_text("old = 1\n", encoding="utf-8")
    workflow = CodeEditWorkflow(
        tmp_path,
        search=_FakeSearch(),
        llm=_BrokenDiffLLM(),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    )

    result = asyncio.run(workflow.run("rename old to new"))

    assert result.applied is True
    assert result.used_full_rewrite is True
    assert result.changed_files == ["app.py"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "new = 2\n"
    assert result.plan == "Edit app.py to set new = 2."


class _RecordingLLM:
    """Records the pack.user_request seen for each specialist name, so tests
    can confirm the planner's output actually reaches later prompts instead
    of just not-crashing."""

    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.requests: dict[str, list[str]] = {}

    async def run_specialist(self, specialist, pack):
        self.requests.setdefault(specialist, []).append(pack.user_request)
        return LLMResponse(raw=self.responses.get(specialist, ""), model_used="fake")


def test_rewrite_files_fully_folds_plan_text_into_its_prompt(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old = 1\n", encoding="utf-8")
    llm = _RecordingLLM({"coder": "new = 2\n"})

    changed = asyncio.run(
        rewrite_files_fully(
            llm=llm,
            context_builder=ContextBuilder(),
            patch_engine=_AllowPatchEngine(),
            workspace_root=tmp_path,
            request="make it new",
            target_paths=["app.py"],
            specialist="coder",
            plan_text="Change the assignment on line 1 to new = 2.",
        )
    )

    assert changed == ["app.py"]
    assert "Change the assignment on line 1 to new = 2." in llm.requests["coder"][0]


def test_code_edit_plan_reaches_the_coder_pack(tmp_path):
    (tmp_path / "app.py").write_text("old = 1\n", encoding="utf-8")
    llm = _RecordingLLM({
        "planner": "Update app.py: set new = 2.",
        "coder": "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old = 1\n+new = 2\n",
    })
    workflow = CodeEditWorkflow(
        tmp_path,
        search=_FakeSearch(),
        llm=llm,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    )

    result = asyncio.run(workflow.run("rename old to new"))

    assert result.applied is True
    assert result.plan == "Update app.py: set new = 2."
    assert "Update app.py: set new = 2." in llm.requests["coder"][0]
