from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.bugfix_workflow import BugFixWorkflow, parse_traceback_locations
from shamsu.patch.engine import PatchEngine
from shamsu.types import ContextPack, LLMResponse, SearchResult


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
                content="def divide(a, b):\n    return a / b",
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
        return LLMResponse(raw=self.raw, format="diff", model_used="fake-bugfix")


def test_parse_traceback_locations_accepts_tracebacks_and_plain_locations():
    report = '''Traceback (most recent call last):
  File "tests/test_app.py", line 7, in test_divide
    divide(1, 0)
  File "app.py", line 2, in divide
    return a / b
ZeroDivisionError: division by zero
Also see app.py:2
'''

    locations = parse_traceback_locations(report)

    assert [(location.file_path, location.line) for location in locations] == [
        ("tests/test_app.py", 7),
        ("app.py", 2),
    ]


@pytest.mark.asyncio
async def test_bugfix_workflow_applies_valid_diff_with_real_patch_engine(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,4 @@
 def divide(a, b):
+    if b == 0:
+        return 0
     return a / b
"""
    report = '''Traceback (most recent call last):
  File "app.py", line 2, in divide
ZeroDivisionError: division by zero
'''
    search = FakeSearch()
    llm = FakeLLM(diff)

    result = await BugFixWorkflow(
        workspace_root=tmp_path,
        search=search,
        llm=llm,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run(report)

    assert result.applied is True
    assert result.error == ""
    assert result.changed_files == ["app.py"]
    assert result.locations[0].file_path == "app.py"
    assert "if b == 0" in target.read_text(encoding="utf-8")
    assert search.queries[0] == report.strip()
    assert "app.py" in search.queries
    assert "ZeroDivisionError" in search.queries[-1]
    assert llm.specialist == "bugfix"
    assert llm.pack is not None
    assert "Output ONLY a unified diff" in llm.pack.user_request


@pytest.mark.asyncio
async def test_bugfix_workflow_rejects_malformed_output_without_applying(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")

    result = await BugFixWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=FakeLLM("The bug is in app.py."),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run("app.py:1 ValueError: wrong value")

    assert result.applied is False
    assert result.error.startswith("Invalid diff:")
    assert target.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.asyncio
async def test_bugfix_workflow_reports_denied_apply(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""

    result = await BugFixWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=FakeLLM(diff),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: False),
    ).run("app.py:1 AssertionError")

    assert result.applied is False
    assert result.error == "Patch was not applied."
    assert result.changed_files == ["app.py"]
    assert target.read_text(encoding="utf-8") == "value = 1\n"
