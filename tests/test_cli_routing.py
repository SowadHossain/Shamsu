from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

from prompt_toolkit.document import Document
from rich.console import Console

from shamsu.cli import repl
from shamsu.tools.browser import BrowserActionResult
from shamsu.tools.django import DjangoCommandResult, DjangoSetupResult
from shamsu.types import ContextPack, LLMResponse, SearchResult, TestRunResult as ShamsuTestRunResult


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


class FakeGitTool:
    warning: str | None = None

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    def warn_if_dirty(self) -> str | None:
        return self.warning


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


def test_keyword_decision_does_not_misroute_prd_chat_as_code_edit():
    assert repl._keyword_decision(
        "i have add a prd to my working folder can you check that out?"
    ).intent == "qa"


def test_route_prompt_falls_back_to_keyword_router_when_llm_is_down():
    decision = asyncio.run(repl._route_prompt("write tests for parser", FakeLLM()))

    assert decision.intent == "test_gen"
    assert decision.confidence == 0.35


def test_normalize_command_input_strips_leading_slash():
    assert repl._normalize_command_input("/models repair") == "models repair"
    assert repl._normalize_command_input("/help") == "help"
    assert repl._normalize_command_input("hello there") == "hello there"


def test_slash_command_completer_suggests_system_commands():
    completer = repl.SlashCommandCompleter()

    completions = list(completer.get_completions(Document("/mod"), None))

    texts = [item.text for item in completions]
    assert "/models status" in texts
    assert "/models pull" in texts
    assert "/models repair" in texts


def test_slash_command_completer_suggests_web_and_browser_commands():
    completer = repl.SlashCommandCompleter()

    web_texts = [item.text for item in completer.get_completions(Document("/web"), None)]
    browse_texts = [item.text for item in completer.get_completions(Document("/browse"), None)]

    assert "/web search " in web_texts
    assert "/browse open " in browse_texts
    assert "/browse screenshot" in browse_texts


def test_web_needed_prompt_detects_external_docs_requests():
    assert repl._looks_like_web_needed_prompt("look up the latest Django auth docs")
    assert not repl._looks_like_web_needed_prompt("how does auth work in this repo?")


def test_browser_needed_prompt_detects_local_preview_requests():
    assert repl._looks_like_browser_needed_prompt("check the app and verify the dashboard")
    assert repl._looks_like_browser_needed_prompt("open http://127.0.0.1:8000 and inspect the rendered ui")
    assert not repl._looks_like_browser_needed_prompt("summarize https://docs.djangoproject.com/en/5.1/")


def test_code_edit_handler_prints_applied_result(monkeypatch, tmp_path):
    console, output = _console_output()
    monkeypatch.setattr(repl, "CodeEditWorkflow", FakeCodeEditWorkflow)
    monkeypatch.setattr(repl, "GitTool", FakeGitTool)
    FakeGitTool.warning = None

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
    assert "uncommitted changes" not in rendered


def test_code_edit_handler_warns_before_editing_dirty_worktree(monkeypatch, tmp_path):
    console, output = _console_output()
    monkeypatch.setattr(repl, "CodeEditWorkflow", FakeCodeEditWorkflow)
    monkeypatch.setattr(repl, "GitTool", FakeGitTool)
    FakeGitTool.warning = "Workspace has uncommitted changes: app.py"

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
    assert "Workspace has uncommitted changes: app.py" in rendered
    assert rendered.index("Workspace has uncommitted changes") < rendered.index("Code Edit Applied")


def test_code_edit_handler_skips_non_git_warning(monkeypatch, tmp_path):
    console, output = _console_output()
    monkeypatch.setattr(repl, "CodeEditWorkflow", FakeCodeEditWorkflow)
    monkeypatch.setattr(repl, "GitTool", FakeGitTool)
    FakeGitTool.warning = "Workspace is not a git repository."

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
    assert "Workspace is not a git repository." not in rendered
    assert "Code Edit Applied" in rendered


def test_django_setup_command_prints_runner_result(monkeypatch, tmp_path):
    console, output = _console_output()
    project = tmp_path / "generated"
    project.mkdir()

    class FakeDjangoSetupRunner:
        def __init__(self, workspace_root: Path, session_logger=None) -> None:
            assert workspace_root == tmp_path
            assert session_logger is None

        def run(self, project_dir: str) -> DjangoSetupResult:
            assert project_dir == "generated"
            return DjangoSetupResult(
                project_cwd=project,
                commands=[
                    DjangoCommandResult(
                        step="install_requirements",
                        command="pip install -r requirements.txt",
                        cwd=project,
                        exit_code=0,
                        stdout="installed",
                    )
                ],
            )

    monkeypatch.setattr(repl, "DjangoSetupRunner", FakeDjangoSetupRunner)

    repl._handle_django("django setup generated", tmp_path, console)

    rendered = output.getvalue()
    assert "Django Setup" in rendered
    assert "install_requirements" in rendered
    assert "dependencies installed" in rendered


def test_django_test_command_prints_runner_result(monkeypatch, tmp_path):
    console, output = _console_output()

    class FakeDjangoTestRunner:
        def __init__(self, workspace_root: Path, session_logger=None) -> None:
            assert workspace_root == tmp_path
            assert session_logger is None

        def run(self, project_dir: str) -> ShamsuTestRunResult:
            assert project_dir == "generated"
            return ShamsuTestRunResult(passed=3, failed=0, raw_output="OK")

    monkeypatch.setattr(repl, "DjangoTestRunner", FakeDjangoTestRunner)

    repl._handle_django("django test generated", tmp_path, console)

    rendered = output.getvalue()
    assert "Django Tests" in rendered
    assert "3" in rendered


def test_browse_handler_prints_opened_page(monkeypatch, tmp_path):
    console, output = _console_output()

    class FakeBrowserTool:
        def open(self, url: str, reason: str = "", require_approval: bool = True):
            assert url == "http://127.0.0.1:8000"
            return BrowserActionResult(
                ok=True,
                url=url,
                title="Demo App",
                visible_text="Welcome to SHAMSU",
            )

        def read(self):  # pragma: no cover - not used here
            return BrowserActionResult(ok=False)

        def click(self, selector: str):  # pragma: no cover - not used here
            return BrowserActionResult(ok=False)

        def type_text(self, selector: str, text: str):  # pragma: no cover - not used here
            return BrowserActionResult(ok=False)

        def screenshot(self):  # pragma: no cover - not used here
            return BrowserActionResult(ok=False)

    repl._handle_browse("browse open http://127.0.0.1:8000", console, FakeBrowserTool())

    rendered = output.getvalue()
    assert "Browser" in rendered
    assert "Demo App" in rendered
    assert "Welcome to SHAMSU" in rendered
