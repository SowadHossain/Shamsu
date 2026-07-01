from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

from rich.console import Console

from shamsu.cli import repl
from shamsu.types import ContextPack, LLMResponse, SearchResult


class FakeSearch:
    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return [
            SearchResult(
                file_path="app.py",
                language="python",
                line_start=1,
                line_end=1,
                content="value = 1",
                score=1.0,
            )
        ]

    def symbol_lookup(self, name: str) -> list[SearchResult]:
        return []

    def fts_search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return self.search(query, top_k=top_k)


class FakeLLM:
    async def route(self, prompt: str, project_summary: str):
        raise RuntimeError("router offline")

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        return LLMResponse(raw="", model_used="fake")


class FakeCodeEditWorkflow:
    def __init__(self, workspace_root: Path, search, llm=None) -> None:
        self.workspace_root = workspace_root

    async def run(self, request: str):
        return _PatchResult(applied=True, changed_files=["app.py"], error="")


class _PatchResult:
    def __init__(self, applied: bool, changed_files: list[str], error: str) -> None:
        self.applied = applied
        self.changed_files = changed_files
        self.error = error


def _console_output() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def test_forced_decision_routes_explicit_commands():
    decision = repl._forced_decision("fix Traceback here")

    assert decision is not None
    assert decision.intent == "bug_fix"
    assert decision.confidence == 1.0


def test_keyword_decision_routes_common_agent_prompts():
    assert repl._keyword_decision("write tests for parser").intent == "test_gen"
    assert repl._keyword_decision("audit this for security issues").intent == "audit"
    assert repl._keyword_decision("update the README").intent == "doc_gen"
    assert repl._keyword_decision("change the banner").intent == "code_edit"
    assert repl._keyword_decision("how does auth work?").intent == "qa"


def test_route_prompt_falls_back_to_keyword_router_when_llm_is_down():
    decision = asyncio.run(repl._route_prompt("write tests for parser", FakeLLM()))

    assert decision.intent == "test_gen"
    assert decision.confidence == 0.35


def test_code_edit_handler_prints_applied_result(monkeypatch, tmp_path):
    console, output = _console_output()
    monkeypatch.setattr(repl, "CodeEditWorkflow", FakeCodeEditWorkflow)

    asyncio.run(
        repl._run_code_edit(
            "edit change value",
            tmp_path,
            FakeSearch(),
            console,
            FakeLLM(),
        )
    )

    rendered = output.getvalue()
    assert "Code Edit Applied" in rendered
    assert "app.py" in rendered
