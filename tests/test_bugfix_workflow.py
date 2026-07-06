from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.bugfix_workflow import (
    BugFixWorkflow,
    parse_import_export_error,
    parse_traceback_locations,
    scan_ts_exports,
)
from shamsu.patch.engine import PatchEngine
from shamsu.types import ContextPack, LLMResponse, SearchResult


class FakeSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, top_k: int = 5, boost_paths: list[str] | None = None) -> list[SearchResult]:
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


class FakeCouncilLLM:
    """Distinguishes bugfix (draft/reconcile) vs reviewer (critique) calls."""

    def __init__(self, bugfix_responses: list[str], reviewer_responses: list[str]) -> None:
        self.bugfix_responses = list(bugfix_responses)
        self.reviewer_responses = list(reviewer_responses)
        self.specialists_called: list[str] = []

    async def route(self, prompt: str, project_summary: str):  # pragma: no cover
        raise NotImplementedError

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        self.specialists_called.append(specialist)
        if specialist == "reviewer":
            raw = self.reviewer_responses.pop(0)
        else:
            raw = self.bugfix_responses.pop(0)
        return LLMResponse(raw=raw, format="diff", model_used=f"fake-{specialist}")


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


def test_parse_traceback_locations_accepts_tsc_style_locations():
    report = (
        "src/game/rules.ts(71,17): error TS1005: ')' expected.\n"
        "src/game/rules.ts(71,32): error TS1005: ',' expected.\n"
    )

    locations = parse_traceback_locations(report)

    assert [(location.file_path, location.line) for location in locations] == [
        ("src/game/rules.ts", 71),
    ]


def test_parse_traceback_locations_accepts_colon_style_frontend_locations():
    report = "src/App.tsx:23:10 - error TS2322: Type 'string' is not assignable."

    locations = parse_traceback_locations(report)

    assert [(location.file_path, location.line) for location in locations] == [
        ("src/App.tsx", 23),
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


def test_bugfix_workflow_includes_file_region_around_reported_line(tmp_path: Path):
    target = tmp_path / "src" / "App.tsx"
    target.parent.mkdir()
    target.write_text("\n".join(f"line {index}" for index in range(1, 31)) + "\n", encoding="utf-8")
    workflow = BugFixWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=FakeLLM(""),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    )

    locations = parse_traceback_locations("src/App.tsx:20:5 - error TS1005: ';' expected")
    pack, _paths = workflow._build_pack("src/App.tsx:20:5 - error TS1005: ';' expected", locations)
    snippet = "\n".join(item.content for item in pack.snippets)
    assert "line 20" in snippet
    assert "line 15" in snippet


class SequenceLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    async def route(self, prompt: str, project_summary: str):  # pragma: no cover
        raise NotImplementedError

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        raw = self.responses.pop(0) if self.responses else ""
        return LLMResponse(raw=raw, model_used="fake-bugfix")


@pytest.mark.asyncio
async def test_bugfix_workflow_reports_error_when_diff_repair_fails(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")

    result = await BugFixWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=SequenceLLM(["The bug is in app.py.", "", ""]),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run("app.py:1 ValueError: wrong value")

    assert result.applied is False
    assert result.used_full_rewrite is False
    assert result.error.startswith("Invalid diff:")
    assert "No file was changed" in result.error
    assert target.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.asyncio
async def test_bugfix_workflow_repairs_malformed_diff_and_applies(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    bad = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
+extra
"""
    repaired = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""

    result = await BugFixWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=SequenceLLM([bad, repaired]),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run("app.py:1 ValueError: wrong value")

    assert result.applied is True
    assert result.used_full_rewrite is False
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_bugfix_workflow_tiny_targeted_edit_fallback_requires_unique_match(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")

    result = await BugFixWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=SequenceLLM(["FILE: app.py\nSEARCH:\nvalue = 1\n\nREPLACE:\nvalue = 2\n", "", ""]),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run("The value assertion is failing, please fix it")

    assert result.applied is True
    assert result.changed_files == ["app.py"]
    assert target.read_text(encoding="utf-8") == "value = 2\n"


@pytest.mark.asyncio
async def test_bugfix_workflow_tiny_targeted_edit_fallback_refuses_ambiguous_match(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")

    result = await BugFixWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=SequenceLLM(["FILE: app.py\nSEARCH:\nvalue = 1\n\nREPLACE:\nvalue = 2\n", "", ""]),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run("The value assertion is failing, please fix it")

    assert result.applied is False
    assert "matched 2 time" in result.error
    assert target.read_text(encoding="utf-8") == "value = 1\nvalue = 1\n"


def test_import_export_error_parser_and_export_scanner():
    parsed = parse_import_export_error("src/session.ts:1:10 - error TS2305: Module './game/loop' has no exported member 'GameLoop'.")

    assert parsed is not None
    assert parsed.missing_export == "GameLoop"
    assert parsed.module_path == "./game/loop"
    assert scan_ts_exports("export const gameLoop = 1;\nexport { gameLoop as GameLoop };\nexport default class X {}\n") == {
        "gameLoop",
        "GameLoop",
        "default",
    }


@pytest.mark.asyncio
async def test_bugfix_workflow_rejects_patch_that_removes_ts_export(tmp_path: Path):
    target = tmp_path / "loop.ts"
    target.write_text("export const gameLoop = 1;\nexport const telemetry = 2;\n", encoding="utf-8")
    diff = """--- a/loop.ts
+++ b/loop.ts
@@ -1,2 +1 @@
 export const gameLoop = 1;
-export const telemetry = 2;
"""

    result = await BugFixWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=SequenceLLM([diff]),
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run("loop.ts:1 error TS2305")

    assert result.applied is False
    assert "removes existing export" in result.error
    assert "telemetry" in target.read_text(encoding="utf-8")


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


@pytest.mark.asyncio
async def test_bugfix_workflow_convenes_council_for_security_sensitive_traceback(tmp_path: Path):
    target = tmp_path / "shamsu" / "safety" / "approval.py"
    target.parent.mkdir(parents=True)
    target.write_text("def ask_approval():\n    return True\n", encoding="utf-8")
    draft_diff = """--- a/shamsu/safety/approval.py
+++ b/shamsu/safety/approval.py
@@ -1,2 +1,2 @@
 def ask_approval():
-    return True
+    return False
"""
    reconciled_diff = """--- a/shamsu/safety/approval.py
+++ b/shamsu/safety/approval.py
@@ -1,2 +1,3 @@
 def ask_approval():
+    # fixed after review
     return True
"""
    llm = FakeCouncilLLM(
        bugfix_responses=[draft_diff, reconciled_diff],
        reviewer_responses=["Security risk: this silently flips approval to always-deny."],
    )
    report = 'Traceback (most recent call last):\n  File "shamsu/safety/approval.py", line 2\nAssertionError'

    result = await BugFixWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=llm,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run(report)

    assert llm.specialists_called == ["bugfix", "reviewer", "bugfix"]
    assert result.applied is True
    assert "fixed after review" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_bugfix_workflow_skips_council_for_ordinary_traceback(tmp_path: Path):
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
    llm = FakeCouncilLLM(bugfix_responses=[diff], reviewer_responses=[])
    report = 'Traceback (most recent call last):\n  File "app.py", line 2\nZeroDivisionError'

    result = await BugFixWorkflow(
        workspace_root=tmp_path,
        search=FakeSearch(),
        llm=llm,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).run(report)

    assert llm.specialists_called == ["bugfix"]
    assert result.applied is True
