from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

from prompt_toolkit.document import Document
import pytest
from rich.console import Console

from shamsu.cli import repl
from shamsu.tools.browser import BrowserActionResult
from shamsu.tools.django import DjangoCommandResult, DjangoSetupResult
from shamsu.types import ContextPack, LLMResponse, SearchResult, TestRunResult as ShamsuTestRunResult


class FakeSearch:
    def search(self, query: str, top_k: int = 5, boost_paths: list[str] | None = None) -> list[SearchResult]:
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


class StreamingFakeLLM:
    """A fake manager that streams tokens, like the real LLMManager."""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens

    async def run_specialist_stream(self, specialist, pack, on_token):
        for token in self.tokens:
            on_token(token)
        return LLMResponse(raw="".join(self.tokens), model_used="fake-stream")

    async def run_specialist(self, specialist, pack):  # pragma: no cover - not reached
        return LLMResponse(raw="".join(self.tokens), model_used="fake-stream")


def test_run_qa_streams_tokens_to_console(tmp_path):
    console, output = _console_output()

    asyncio.run(
        repl._run_qa(
            "how does auth work?",
            tmp_path,
            console,
            StreamingFakeLLM(["Auth ", "works ", "like this."]),
        )
    )

    rendered = output.getvalue()
    assert "Answer" in rendered
    assert "Auth works like this." in rendered


def test_run_general_chat_streams_tokens_to_console(tmp_path):
    console, output = _console_output()

    asyncio.run(
        repl._run_general_chat(
            "hi there",
            console,
            StreamingFakeLLM(["Hey", "!"]),
        )
    )

    rendered = output.getvalue()
    assert "Chat" in rendered
    assert "Hey!" in rendered


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
    def __init__(self, applied: bool, changed_files: list[str], error: str, used_full_rewrite: bool = False) -> None:
        self.applied = applied
        self.changed_files = changed_files
        self.error = error
        self.used_full_rewrite = used_full_rewrite


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


def test_react_prompt_detects_file_and_command_requests():
    assert repl._looks_like_react_prompt("create hello.py")
    assert repl._looks_like_react_prompt("run the tests")
    assert not repl._looks_like_react_prompt("explain this repo")


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


def test_slash_command_completer_suggests_at_files(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    completer = repl.SlashCommandCompleter(tmp_path)

    texts = [item.text for item in completer.get_completions(Document("@REA"), None)]

    assert texts == ["@README.md"]


def test_web_needed_prompt_detects_external_docs_requests():
    assert repl._looks_like_web_needed_prompt("look up the latest Django auth docs")
    assert repl._looks_like_web_needed_prompt("whats the weather today?")
    assert not repl._looks_like_web_needed_prompt("how does auth work in this repo?")


def test_is_prd_filename_matches_spelled_out_and_acronym_names():
    from shamsu.prd.input import is_prd_filename

    assert is_prd_filename("Product Requirements Document.pdf")
    assert is_prd_filename("myprd.md")
    assert is_prd_filename("PRD.txt")
    assert is_prd_filename("requirements document.md")
    assert not is_prd_filename("notes.txt")
    assert not is_prd_filename("upward.md")
    assert not is_prd_filename("report.pdf")
    assert not is_prd_filename("game.py")  # unsupported extension


def test_prd_build_request_fires_on_build_phrasing_with_a_workspace_prd(tmp_path):
    (tmp_path / "Product Requirements Document.md").write_text(
        "# Cube Runner\n\n## Milestone 1: Setup\n", encoding="utf-8"
    )

    assert repl._looks_like_prd_build_request("build me the product from this prd", tmp_path)
    # Works even without the literal word "prd" because a single PRD file exists.
    assert repl._looks_like_prd_build_request("finish the product please", tmp_path)


def test_prd_build_request_tolerates_common_build_typo_with_pdf_prd(tmp_path):
    (tmp_path / "Product Requirements Document.pdf").write_bytes(b"%PDF-1.4 stub")

    assert repl._looks_like_prd_build_request("buld me the game from prd", tmp_path)


@pytest.mark.asyncio
async def test_typo_prd_build_prompt_bypasses_qa_and_enters_build_handler(tmp_path, monkeypatch):
    from rich.console import Console

    (tmp_path / "Product Requirements Document.pdf").write_bytes(b"%PDF-1.4 stub")
    calls = []

    async def fake_build_handler(user_input, workspace, console, session_logger=None):
        calls.append((user_input, workspace))

    async def forbidden_route(*args, **kwargs):
        raise AssertionError("typo PRD build prompt should not reach QA routing")

    monkeypatch.setattr(repl, "_handle_prd_build_request", fake_build_handler)
    monkeypatch.setattr(repl, "_route_prompt", forbidden_route)

    await repl._handle_request(
        "buld me the game from prd",
        tmp_path,
        Console(file=StringIO()),
        web_tool=None,
        browser_tool=None,
    )

    assert calls == [("buld me the game from prd", tmp_path)]


def test_prd_build_request_does_not_fire_on_narrow_or_casual_prompts(tmp_path):
    (tmp_path / "Product Requirements Document.md").write_text("# X\n", encoding="utf-8")

    assert not repl._looks_like_prd_build_request("build the navbar", tmp_path)
    assert not repl._looks_like_prd_build_request("hola", tmp_path)
    assert not repl._looks_like_prd_build_request("how does auth work?", tmp_path)


def test_vague_action_request_detects_imperatives_but_not_questions():
    for imperative in ("do the task", "do it", "continue", "go", "build it", "keep going", "finish it"):
        assert repl._looks_like_vague_action_request(imperative), imperative
    for question in ("how does auth work?", "what files do i have here?", "hi there",
                     "explain the payment module in detail please"):
        assert not repl._looks_like_vague_action_request(question), question


def test_trouble_reports_and_error_logs_route_to_the_fix_path():
    # "It's broken" reports and pasted errors are implicit fix requests, not
    # questions — they must reach the tool-having agent loop, not tool-less QA.
    troubles = (
        "i still cant see the game working",
        "the page is blank",
        "nothing happens when i open it",
        "App.tsx: Module '\"./game/rules\"' has no exported member 'createInputState'.",
        "Uncaught SyntaxError: Unexpected token",
        "[plugin:vite:react] Failed to compile",
        "it doesn't work",
    )
    for prompt in troubles:
        assert repl._looks_like_trouble_report(prompt), prompt

    for ordinary in ("how does the game loop work?", "what is colyseus", "hello"):
        assert not repl._looks_like_trouble_report(ordinary), ordinary


def test_action_request_catches_imperatives_beyond_the_phrase_list():
    # Real prompts that previously fell through to the tool-less QA brain and
    # got a described-but-not-applied answer.
    actions = (
        "okay you should do the thing",
        "fix the code and check the requirements and fix it",
        "fix the collision detection",
        "implement the game over screen",
        "add sound effects to the game",
        "refactor script.js",
        "can you fix the bug in movement",
        "review the code and correct the scoring",
        "do the rest",
    )
    for prompt in actions:
        assert repl._looks_like_action_request(prompt), prompt

    # Genuine questions must still go to QA, not the agent loop.
    questions = (
        "how do i fix the collision bug?",
        "what does spawnObstacles do",
        "why is the cube spinning",
        "is the scoring correct",
        "explain the movement logic",
        "does the game track high scores?",
    )
    for prompt in questions:
        assert not repl._looks_like_action_request(prompt), prompt


def test_action_request_catches_run_start_verbs():
    # "run the server" was previously misrouted to the tool-less QA brain
    # (intent=qa), which only described how to run it instead of executing it.
    actions = (
        "run the server",
        "start the dev server",
        "restart the backend",
        "execute the tests",
        "launch the game",
        "rebuild the project",
    )
    for prompt in actions:
        assert repl._looks_like_action_request(prompt), prompt


def test_file_write_request_routes_explicit_file_prompts_to_agent_loop():
    writes = (
        "create hello.py with a hello world script",
        "write file src/app.tsx",
        "save this as README.md",
        "make a .gitignore file",
        "add tests for calculator.py",
    )
    for prompt in writes:
        assert repl._looks_like_file_write_request(prompt), prompt

    questions = (
        "what files did you write?",
        "how do i create hello.py?",
        "show me the README.md",
        "list files here",
    )
    for prompt in questions:
        assert not repl._looks_like_file_write_request(prompt), prompt


def test_run_game_request_detects_preview_link_prompts():
    assert repl._looks_like_run_game_request("okay run the game now and give me the link to access it")
    assert repl._looks_like_run_game_request("start the app preview")
    assert not repl._looks_like_run_game_request("what is the game loop doing?")


def test_run_game_handler_starts_dev_servers_and_prints_link(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text('{"scripts":{"dev":"vite","dev:relay":"tsx server/relay.ts"}}', encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    started = []

    class FakePopen:
        def __init__(self, command, **kwargs):
            self.pid = 4321 + len(started)
            started.append((command, kwargs))

    monkeypatch.setattr(repl.subprocess, "Popen", FakePopen)

    async def no_verify(*_a, **_k):
        return True

    async def no_settle(*_a, **_k):
        return None

    monkeypatch.setattr(repl, "_verify_and_repair_frontend", no_verify)
    monkeypatch.setattr(repl, "_await_dev_server_settle", no_settle)

    out = StringIO()
    console = Console(file=out, force_terminal=False)
    asyncio.run(repl._handle_run_game(tmp_path, console))

    output = out.getvalue()
    assert "http://localhost:5173" in output
    # Two dev processes launched (relay + vite).
    assert len(started) == 2
    flattened = " ".join(str(item[0]) for item in started)
    assert "dev:relay" in flattened and "npm run dev" in flattened
    if hasattr(repl.subprocess, "CREATE_NEW_CONSOLE"):
        # Visible window (no piped stdout) AND teed to a log file for reading.
        assert all("stdout" not in item[1] for item in started)
        assert all(item[1]["creationflags"] == repl.subprocess.CREATE_NEW_CONSOLE for item in started)
        assert "Tee-Object" in flattened


def test_dev_log_scanner_flags_errors_and_recognizes_ready():
    ready = "VITE v6.0.3  ready in 431 ms\n  ➜  Local:   http://localhost:5173/"
    assert repl._dev_log_indicates_ready(ready)
    assert repl._scan_dev_log_for_errors(ready) is None

    broken = "[vite] Internal server error: Failed to resolve import \"./game/rules\" from \"src/App.tsx\""
    assert repl._scan_dev_log_for_errors(broken) is not None
    assert "Failed to resolve import" in repl._scan_dev_log_for_errors(broken)

    export_err = "src/App.tsx:3:30 - error TS2724: '\"./game/rules\"' has no exported member 'createInputState'."
    assert repl._scan_dev_log_for_errors(export_err) is not None


def test_read_text_safe_decodes_utf16_teed_logs(tmp_path):
    # Windows PowerShell Tee-Object writes UTF-16 with a BOM; the log reader must
    # decode it so the scanners see real text, not interleaved null bytes.
    log = tmp_path / "vite.log"
    log.write_bytes("[vite] Internal server error\n".encode("utf-16"))

    text = repl._read_text_safe(log)

    assert "Internal server error" in text
    assert repl._scan_dev_log_for_errors(text) is not None


def test_agent_display_summary_hides_code_and_lists_edited_files():
    body = (
        "Implemented the changes.\n"
        "1. Added collisions\n"
        "2. Updated HUD\n\n"
        "```ts\nexport const x = 1;\n```"
    )

    rendered = repl._agent_display_summary(
        body,
        ["Writing src/game/rules.ts", "Writing src/ui/Hud.tsx"],
    )

    assert "Edited files:" in rendered
    assert "src/game/rules.ts" in rendered
    assert "What changed:" in rendered
    assert "export const x" not in rendered


def test_context_preview_is_hidden_by_default(monkeypatch):
    monkeypatch.delenv("SHAMSU_SHOW_CONTEXT", raising=False)

    assert repl._should_show_context_preview() is False


def test_context_preview_can_be_enabled(monkeypatch):
    monkeypatch.setenv("SHAMSU_SHOW_CONTEXT", "1")

    assert repl._should_show_context_preview() is True


@pytest.mark.asyncio
async def test_file_write_prompt_bypasses_llm_router_and_uses_agent_chat(tmp_path, monkeypatch):
    from rich.console import Console

    calls = []

    async def fake_run_agent_chat(user_input, workspace, console, session_logger=None, force_long_running=False, auto_approve=False):
        calls.append((user_input, workspace, auto_approve))

    async def forbidden_route(*args, **kwargs):
        raise AssertionError("file write prompt should not reach LLM routing")

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    monkeypatch.setattr(repl, "_route_prompt", forbidden_route)

    await repl._handle_request(
        "create hello.py with a hello world script",
        tmp_path,
        Console(file=StringIO()),
        web_tool=None,
        browser_tool=None,
    )

    assert calls
    assert calls[0][1] == tmp_path


@pytest.mark.asyncio
async def test_affirmative_followup_continues_game_agent_instead_of_qa(tmp_path, monkeypatch):
    for relative in (
        "client/src/game",
        "client/src/ui",
        "server/src",
    ):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    for relative in (
        "package.json",
        "client/package.json",
        "client/src/App.tsx",
        "client/src/game/entities.ts",
        "client/src/game/rules.ts",
        "client/src/ui/Hud.tsx",
        "server/src/index.ts",
        "server/src/db.ts",
    ):
        (tmp_path / relative).write_text("// existing\n", encoding="utf-8")
    calls = []

    async def fake_run_agent_chat(user_input, workspace, console, session_logger=None, force_long_running=False, auto_approve=False):
        calls.append((user_input, workspace, force_long_running, auto_approve))

    async def forbidden_route(*args, **kwargs):
        raise AssertionError("affirmative game follow-up should not reach QA routing")

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    monkeypatch.setattr(repl, "_route_prompt", forbidden_route)

    await repl._handle_request(
        "yes please",
        tmp_path,
        Console(file=StringIO()),
        web_tool=None,
        browser_tool=None,
    )

    assert calls
    assert calls[0][1] == tmp_path
    assert calls[0][2] is True
    assert calls[0][3] is True


def test_vague_action_with_a_prd_present_routes_to_build(tmp_path):
    # "do the task" in a workspace with exactly one PRD means "build that PRD".
    (tmp_path / "Product Requirements Document.md").write_text(
        "# Cube Runner\n\n## Milestones\nMilestone 1: Setup\n", encoding="utf-8"
    )
    assert repl._looks_like_prd_build_request("do the task", tmp_path)
    assert repl._looks_like_prd_build_request("continue", tmp_path)


def test_vague_action_without_a_prd_does_not_trigger_build(tmp_path):
    # No PRD present -> not a build request (it will fall through to the agent
    # loop, which has real tools, instead of the tool-less QA path).
    assert not repl._looks_like_prd_build_request("do the task", tmp_path)


def test_web_needed_prompt_tolerates_common_weather_typos():
    """A typo like "weither" must still route to the real web-search path
    rather than silently falling through to a tool-less chat completion
    that has no way to actually check the weather."""
    assert repl._looks_like_web_needed_prompt("can you check the weither today")
    assert repl._looks_like_web_needed_prompt("whats the wheather like")
    assert not repl._looks_like_web_needed_prompt("hola")
    assert not repl._looks_like_web_needed_prompt("how you doin?")


def test_qa_workflow_prompt_includes_no_live_tools_notice():
    from shamsu.agents.qa_workflow import NO_LIVE_TOOLS_NOTICE, QAWorkflow

    preview = QAWorkflow(search=FakeSearch()).build_prompt("can you check the weither today")

    assert NO_LIVE_TOOLS_NOTICE in preview.prompt


def test_browser_needed_prompt_detects_local_preview_requests():
    assert repl._looks_like_browser_needed_prompt("check the app and verify the dashboard")
    assert repl._looks_like_browser_needed_prompt("open http://127.0.0.1:8000 and inspect the rendered ui")
    assert not repl._looks_like_browser_needed_prompt("summarize https://docs.djangoproject.com/en/5.1/")


def test_expand_followup_prompt_uses_previous_turn_for_web_followup():
    expanded = repl._expand_followup_prompt("check on the web", "whats the weather today?")

    assert expanded == "whats the weather today? Please check on the web for this."


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


def test_routed_code_edit_includes_task_harness(monkeypatch, tmp_path):
    console, output = _console_output()
    captured: list[str] = []

    class PlanLLM(FakeLLM):
        async def route(self, prompt: str, project_summary: str):
            from shamsu.types import RoutingDecision

            return RoutingDecision(
                intent="code_edit",
                complexity="multi_step",
                steps=[{"id": 1, "specialist": "coder", "task": "Update app.py"}],
                needs_tools=["search_index", "read_file", "write_file"],
                target_files=["app.py"],
                confidence=0.9,
            )

    class CapturingCodeEditWorkflow:
        def __init__(self, workspace_root: Path, search, llm=None, **kwargs) -> None:
            pass

        async def run(self, request: str):
            captured.append(request)
            return _PatchResult(applied=True, changed_files=["app.py"], error="")

    monkeypatch.setattr(repl, "CodeEditWorkflow", CapturingCodeEditWorkflow)
    monkeypatch.setattr(repl, "_build_search_agent", lambda workspace, logger=None: (FakeSearch(), True))
    monkeypatch.setattr(repl, "_make_llm_manager", lambda *args, **kwargs: PlanLLM())
    monkeypatch.setattr(repl, "ensure_index", lambda *args, **kwargs: None)
    monkeypatch.setattr(repl, "GitTool", FakeGitTool)
    FakeGitTool.warning = None

    asyncio.run(
        repl._handle_request(
            "improve validation logic",
            tmp_path,
            console,
            web_tool=None,
            browser_tool=None,
        )
    )

    assert captured
    assert "## SHAMSU Task Harness" in captured[0]
    assert "Mode: code_edit" in captured[0]
    assert "Target files:\n- app.py" in captured[0]
    assert "Code Edit Applied" in output.getvalue()


def test_run_code_in_dev_routes_to_dev_server(monkeypatch, tmp_path):
    console, output = _console_output()
    calls = []

    class FakeDevServerManager:
        def __init__(self, workspace, approval_manager=None, session_logger=None):
            pass

        def start(self, command):
            calls.append(command)
            return type(
                "Result",
                (),
                {
                    "launched": True,
                    "duplicate": False,
                    "message": "launched",
                    "command": command,
                    "url": "http://localhost:5173/",
                },
            )()

    monkeypatch.setattr(repl, "DevServerManager", FakeDevServerManager)

    asyncio.run(
        repl._handle_request(
            "run the code in dev",
            tmp_path,
            console,
            web_tool=None,
            browser_tool=None,
        )
    )

    assert calls == ["npm run dev"]
    rendered = output.getvalue()
    assert "Dev Server" in rendered
    assert "http://localhost:5173/" in rendered


def test_low_confidence_qa_for_command_like_prompt_is_corrected():
    class LowConfidenceQA:
        async def route(self, prompt: str, project_summary: str):
            from shamsu.types import RoutingDecision

            return RoutingDecision(intent="qa", complexity="single", confidence=0.2)

    decision = asyncio.run(repl._route_prompt("repair one part at a time", LowConfidenceQA()))

    assert decision.intent == "bug_fix"


def test_import_export_runtime_error_routes_to_bug_fix():
    decision = repl._keyword_decision(
        "Uncaught SyntaxError: requested module '/src/game/loop.ts' does not provide an export named 'GameLoop'"
    )

    assert decision.intent == "bug_fix"


def test_bug_followup_phrases_route_to_bug_fix():
    assert repl._keyword_decision("this is an error i got TS2305 Module './rules' has no exported member 'World'").intent == "bug_fix"
    assert repl._keyword_decision("fix it now").intent == "bug_fix"


def test_run_prompts_route_to_command_or_dev_workflow():
    assert repl._looks_like_command_like_prompt("run the code")
    assert repl._looks_like_dev_server_prompt("run dev")


def test_trace_command_persists_mode(tmp_path):
    console, output = _console_output()

    repl._handle_trace("trace verbose", tmp_path, console)

    assert repl._trace_mode(tmp_path) == "verbose"
    assert "verbose" in output.getvalue()


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
