from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.code_edit_workflow import CodeEditWorkflow
from shamsu.patch.engine import PatchEngine
from shamsu.types import ContextPack, LLMResponse, SearchResult


class FakeSearch:
    def __init__(self, file_path: str = "app.py") -> None:
        self.queries: list[str] = []
        self.file_path = file_path

    def search(self, query: str, top_k: int = 5, boost_paths: list[str] | None = None) -> list[SearchResult]:
        self.queries.append(query)
        return [
            SearchResult(
                file_path=self.file_path,
                language="python",
                line_start=1,
                line_end=2,
                content="value = 1\n",
                score=1.0,
            )
        ]

    def symbol_lookup(self, name: str) -> list[SearchResult]:
        return []

    def fts_search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return self.search(query, top_k=top_k)


class FakeLLM:
    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.specialist = ""
        self.pack: ContextPack | None = None

    async def route(self, prompt: str, project_summary: str):  # pragma: no cover
        raise NotImplementedError

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        self.specialist = specialist
        self.pack = pack
        return LLMResponse(raw=self.raw, format="diff", model_used="fake-coder")


class FakeCouncilLLM:
    def __init__(self, coder_responses: list[str], reviewer_responses: list[str]) -> None:
        self.coder_responses = list(coder_responses)
        self.reviewer_responses = list(reviewer_responses)
        self.specialists_called: list[str] = []

    async def route(self, prompt: str, project_summary: str):  # pragma: no cover
        raise NotImplementedError

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        self.specialists_called.append(specialist)
        if specialist == "reviewer":
            return LLMResponse(raw=self.reviewer_responses.pop(0), model_used="fake-reviewer")
        return LLMResponse(raw=self.coder_responses.pop(0), format="diff", model_used="fake-coder")


@pytest.mark.asyncio
async def test_code_edit_workflow_applies_valid_diff_with_real_patch_engine(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
    llm = FakeLLM(diff)

    result = await CodeEditWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=llm,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run("change value to 2")

    assert result.applied is True
    assert result.error == ""
    assert result.changed_files == ["app.py"]
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert llm.specialist == "coder"
    assert llm.pack is not None
    assert "Output ONLY a unified diff" in llm.pack.user_request


@pytest.mark.asyncio
async def test_code_edit_workflow_rejects_malformed_diff_without_applying(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")

    result = await CodeEditWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=FakeLLM("Here is what I would change."),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run("change value")

    assert result.applied is False
    assert result.error.startswith("Invalid diff:")
    assert target.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.asyncio
async def test_code_edit_workflow_reports_denied_or_failed_apply(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""

    result = await CodeEditWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=FakeLLM(diff),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: False),
    ).run("change value to 2")

    assert result.applied is False
    assert result.error == "Patch was not applied."
    assert result.changed_files == ["app.py"]
    assert target.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.asyncio
async def test_code_edit_workflow_convenes_council_for_sensitive_file(tmp_path: Path):
    target = tmp_path / "settings.py"
    target.write_text("SECRET_KEY = 'old'\n", encoding="utf-8")
    draft_diff = """--- a/settings.py
+++ b/settings.py
@@ -1 +1 @@
-SECRET_KEY = 'old'
+SECRET_KEY = 'unsafe'
"""
    reconciled_diff = """--- a/settings.py
+++ b/settings.py
@@ -1 +1 @@
-SECRET_KEY = 'old'
+SECRET_KEY = 'from-env'
"""
    llm = FakeCouncilLLM(
        coder_responses=[draft_diff, reconciled_diff],
        reviewer_responses=["Security issue: do not hardcode a secret."],
    )

    result = await CodeEditWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch("settings.py"),
        llm=llm,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run("update secret key handling")

    assert llm.specialists_called == ["coder", "reviewer", "coder"]
    assert result.applied is True
    assert "from-env" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_code_edit_workflow_skips_council_for_ordinary_file(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
    llm = FakeCouncilLLM(coder_responses=[diff], reviewer_responses=[])

    result = await CodeEditWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch("app.py"),
        llm=llm,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run("change value")

    assert llm.specialists_called == ["coder"]
    assert result.applied is True
