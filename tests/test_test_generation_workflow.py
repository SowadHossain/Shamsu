from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.test_generation_workflow import TestGenerationWorkflow
from shamsu.patch.engine import PatchEngine
from shamsu.types import ContextPack, LLMResponse, SearchResult, TestRunResult as ShamsuTestRunResult


class FakeSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        return [
            SearchResult(
                file_path="app.py",
                language="python",
                line_start=1,
                line_end=2,
                content="def add(a, b):\n    return a + b",
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
        return LLMResponse(raw=self.raw, format="diff", model_used="fake-test-gen")


class FakeCommandRunner:
    def __init__(self, result: ShamsuTestRunResult) -> None:
        self.result = result
        self.cwd: Path | None = None

    def run(self, command: str, cwd: Path) -> tuple[int, str, str]:
        raise NotImplementedError

    def run_tests(self, cwd: Path) -> ShamsuTestRunResult:
        self.cwd = cwd
        return self.result


@pytest.mark.asyncio
async def test_test_generation_workflow_applies_valid_diff_and_can_run_tests(tmp_path: Path):
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    diff = """--- /dev/null
+++ b/tests/test_app.py
@@ -0,0 +1,5 @@
+from app import add
+
+
+def test_add():
+    assert add(1, 2) == 3
"""
    runner = FakeCommandRunner(ShamsuTestRunResult(passed=1, failed=0, raw_output="1 passed"))
    search = FakeSearch()
    llm = FakeLLM(diff)

    result = await TestGenerationWorkflow(
        workspace_root=tmp_path,
        search=search,
        llm=llm,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
        command_runner=runner,
    ).run("write tests for add", run_after_apply=True)

    assert result.applied is True
    assert result.error == ""
    assert result.changed_files == ["tests/test_app.py"]
    assert result.test_result is not None
    assert result.test_result.passed == 1
    assert runner.cwd == tmp_path
    assert (tmp_path / "tests" / "test_app.py").exists()
    assert search.queries[0] == "write tests for add"
    assert "tests pytest" in search.queries[1]
    assert llm.specialist == "test_gen"
    assert llm.pack is not None
    assert "pytest-compatible tests" in llm.pack.user_request


@pytest.mark.asyncio
async def test_test_generation_workflow_rejects_malformed_output(tmp_path: Path):
    result = await TestGenerationWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=FakeLLM("Here are some tests."),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run("write tests")

    assert result.applied is False
    assert result.error.startswith("Invalid diff:")
    assert not (tmp_path / "tests").exists()


@pytest.mark.asyncio
async def test_test_generation_workflow_reports_denied_apply(tmp_path: Path):
    diff = """--- /dev/null
+++ b/tests/test_app.py
@@ -0,0 +1,2 @@
+def test_placeholder():
+    assert True
"""

    result = await TestGenerationWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=FakeLLM(diff),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: False),
    ).run("write tests")

    assert result.applied is False
    assert result.error == "Patch was not applied."
    assert result.changed_files == ["tests/test_app.py"]
    assert not (tmp_path / "tests").exists()


@pytest.mark.asyncio
async def test_test_generation_workflow_preserves_failed_test_result(tmp_path: Path):
    diff = """--- /dev/null
+++ b/tests/test_app.py
@@ -0,0 +1,2 @@
+def test_fails():
+    assert False
"""
    runner = FakeCommandRunner(ShamsuTestRunResult(passed=0, failed=1, raw_output="1 failed"))

    result = await TestGenerationWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=FakeLLM(diff),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
        command_runner=runner,
    ).run("write failing test", run_after_apply=True)

    assert result.applied is True
    assert result.test_result is not None
    assert result.test_result.failed == 1
