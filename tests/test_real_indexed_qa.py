from __future__ import annotations

import asyncio
from io import StringIO

from rich.console import Console

from shamsu.abstract.service import AbstractService
from shamsu.cli import repl
from shamsu.cli.repl import _build_workspace_qa_workflow, _handle_request
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import WebTool
from shamsu.tools.web import SearchHit, WebSearchResult
from shamsu.types import LLMResponse
from tests.test_abstract_service import FakeCodebaseMemoryAdapter
from tests.test_search_ranking import FakeCodebaseMemorySearchAdapter


def _console_output() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def _tools(root):
    return (
        WebTool(approval_func=lambda _request: False),
        BrowserTool(root, approval_func=lambda _request: False),
    )


def _use_healthy_codebase_memory(monkeypatch, workspace, code_matches):
    """Inject a healthy, fake Codebase-Memory MCP adapter so `_build_search_agent`
    (real implementation, no SHAMSU-owned index) returns real-looking results
    without needing the actual upstream binary installed in CI."""
    from shamsu.retriever.search import SearchAgent as RealSearchAgent

    search_adapter = FakeCodebaseMemorySearchAdapter(code_matches=code_matches)
    gate_adapter = FakeCodebaseMemoryAdapter(available=True)
    monkeypatch.setattr(
        repl,
        "AbstractService",
        lambda ws: AbstractService(ws, adapter=gate_adapter),
    )
    monkeypatch.setattr(repl, "SearchAgent", lambda ws: RealSearchAgent(ws, adapter=search_adapter))


def test_workspace_qa_workflow_uses_codebase_memory_when_healthy(monkeypatch, tmp_path):
    (tmp_path / "auth.py").write_text(
        "def authenticate_user(username, password):\n"
        "    return username == 'admin' and bool(password)\n",
        encoding="utf-8",
    )
    _use_healthy_codebase_memory(
        monkeypatch,
        tmp_path,
        code_matches=[
            {"node": "authenticate_user", "file": "auth.py", "start_line": 1, "end_line": 2}
        ],
    )

    workflow, uses_real_index = _build_workspace_qa_workflow(tmp_path)
    preview = workflow.build_prompt("authenticate user")

    assert uses_real_index is True
    assert "auth.py" in preview.prompt
    assert "authenticate_user" in preview.prompt
    assert "stub/example.py" not in preview.prompt


def test_workspace_qa_workflow_falls_back_to_empty_search_when_codebase_memory_unavailable(tmp_path):
    """No real Codebase-Memory MCP binary is installed in this sandbox, so the
    gate is honestly unavailable - QA falls back to an empty search agent
    rather than fabricating results."""
    workflow, uses_real_index = _build_workspace_qa_workflow(tmp_path)
    preview = workflow.build_prompt("how does auth work?")

    assert uses_real_index is False
    assert preview.pack.snippets == []
    assert "stub/example.py" not in preview.prompt


def test_repl_request_uses_codebase_memory_context_when_healthy(monkeypatch, tmp_path):
    (tmp_path / "payments.py").write_text(
        "class PaymentGateway:\n"
        "    def charge_card(self, amount):\n"
        "        return amount > 0\n",
        encoding="utf-8",
    )
    _use_healthy_codebase_memory(
        monkeypatch,
        tmp_path,
        code_matches=[
            {"node": "charge_card", "file": "payments.py", "start_line": 1, "end_line": 3}
        ],
    )
    class FakeLLM:
        def __init__(self, session_logger=None, model_pull_progress=None, action_ledger=None):
            self.session_logger = session_logger

        async def route(self, prompt: str, project_summary: str):
            from shamsu.types import RoutingDecision

            return RoutingDecision(
                intent="qa",
                complexity="single",
                steps=[{"id": 1, "specialist": "qa", "task": prompt}],
                needs_tools=["search"],
                confidence=0.9,
            )

        async def run_specialist(self, specialist, pack):
            snippet_text = "\n".join(snippet.content for snippet in pack.snippets)
            return LLMResponse(raw=f"{pack.prd_context}\n{snippet_text}", model_used="fake-qwen")

    monkeypatch.setattr(repl, "LLMManager", FakeLLM)
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)

    asyncio.run(_handle_request("charge card", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "charge_card" in rendered
    assert "Codebase-Memory MCP is not ready" not in rendered
    assert "stub/example.py" not in rendered


def test_repl_greeting_uses_agent_chat_when_codebase_memory_is_unavailable(monkeypatch, tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)

    class FakeAgentChatLoop:
        def __init__(self, workspace, session_logger=None, tools=None, long_running=False, on_activity=None, progress=None, action_ledger=None):
            assert workspace == tmp_path

        async def run(self, user_input):
            assert user_input == "hi"
            return type("Result", (), {"final": "Hey, I am here.", "stopped": False})()

    monkeypatch.setattr(repl, "AgentChatLoop", FakeAgentChatLoop)

    asyncio.run(_handle_request("hi", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "Hey, I am here." in rendered
    assert "intent=qa" not in rendered
    assert "Codebase-Memory MCP is not ready" not in rendered
    assert "Context Preview" not in rendered


def test_repl_general_chat_uses_agent_loop_when_codebase_memory_is_unavailable(monkeypatch, tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)

    class FakeAgentChatLoop:
        def __init__(self, workspace, session_logger=None, tools=None, long_running=False, on_activity=None, progress=None, action_ledger=None):
            assert workspace == tmp_path
            self.session_logger = session_logger

        async def run(self, user_input):
            assert "what is recursion?" in user_input
            return type("Result", (), {"final": "General answer", "stopped": False})()

    monkeypatch.setattr(repl, "AgentChatLoop", FakeAgentChatLoop)

    asyncio.run(_handle_request("what is recursion?", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "General answer" in rendered
    assert "Agent" in rendered
    assert "Codebase-Memory MCP is not ready" not in rendered
    assert "intent=qa" not in rendered


def test_repl_standalone_code_sample_uses_direct_code_route(monkeypatch, tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)
    calls: list[str] = []

    async def fake_direct_code(user_input, console, llm, session_logger=None, thinking_status=None):
        calls.append(user_input)
        console.print("C++ sample")

    class ForbiddenAgentChatLoop:
        def __init__(self, *args, **kwargs):
            raise AssertionError("standalone code samples should not enter the workspace agent loop")

    monkeypatch.setattr(repl, "_run_direct_code_answer", fake_direct_code)
    monkeypatch.setattr(repl, "AgentChatLoop", ForbiddenAgentChatLoop)

    asyncio.run(_handle_request("make a bouncing ball game code in c++", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "C++ sample" in rendered
    assert calls == ["make a bouncing ball game code in c++"]
    assert "Codebase-Memory MCP is not ready" not in rendered


def test_repl_workspace_prd_request_finds_single_prd_without_routing(tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)
    prd = tmp_path / "TODO_PRD.md"
    prd.write_text("# Todo App\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")

    asyncio.run(_handle_request("i have add a prd to my working folder can you check that out?", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "PRD Found" in rendered
    assert "TODO_PRD.md" in rendered
    assert "/plan-prd" in rendered
    assert "Code Edit Not Applied" not in rendered


def test_repl_workspace_file_question_lists_real_files(tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    (tmp_path / "src").mkdir()

    asyncio.run(_handle_request("hi what files do i have here?", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "Workspace Files" in rendered
    assert "README.md" in rendered
    assert "src" in rendered
    assert "I cannot see any files" not in rendered


def test_repl_workspace_location_question_reports_workspace(tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)

    asyncio.run(_handle_request("what folder are you in rn?", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "Current Workspace" in rendered
    assert str(tmp_path) in rendered
    assert "have a current working directory" not in rendered


def test_repl_weather_question_without_location_asks_location(monkeypatch, tmp_path):
    console, output = _console_output()

    class FakeWebTool:
        def search(self, query: str, reason: str = "", top_k: int = 5):
            raise AssertionError("weather without a location should ask a question first")

        def fetch(self, url: str, reason: str = ""):
            return type(
                "Fetch",
                (),
                {
                    "approved": True,
                    "url": url,
                    "title": "Weather",
                    "text": "Today will be sunny and 31C.",
                    "error": "",
                },
            )()

    class FakeLLM:
        def __init__(self, session_logger=None, model_pull_progress=None, action_ledger=None):
            self.session_logger = session_logger

        async def run_specialist(self, specialist, pack):
            assert pack.task_id == "web-qa"
            return LLMResponse(raw="It will be sunny and 31C.", model_used="fake-qwen")

    monkeypatch.setattr(repl, "LLMManager", FakeLLM)

    asyncio.run(_handle_request("whats the weather today?", tmp_path, console, FakeWebTool(), BrowserTool(tmp_path, approval_func=lambda _request: False)))

    rendered = output.getvalue()
    assert "Location Needed" in rendered
    assert "Which location" in rendered


def test_repl_weather_question_with_location_uses_web_tool(monkeypatch, tmp_path):
    console, output = _console_output()

    class FakeWebTool:
        def search(self, query: str, reason: str = "", top_k: int = 5):
            assert "weather" in query.lower()
            assert "dhaka" in query.lower()
            return WebSearchResult(
                approved=True,
                query=query,
                hits=[SearchHit(title="Weather", url="https://example.com/weather", snippet="Sunny 31C")],
            )

        def fetch(self, url: str, reason: str = ""):
            return type(
                "Fetch",
                (),
                {
                    "approved": True,
                    "url": url,
                    "title": "Weather",
                    "text": "Today will be sunny and 31C in Dhaka. " * 10,
                    "error": "",
                },
            )()

    class FakeLLM:
        def __init__(self, session_logger=None, model_pull_progress=None, action_ledger=None):
            self.session_logger = session_logger

        async def run_specialist(self, specialist, pack):
            assert pack.task_id == "web-qa"
            return LLMResponse(raw="It will be sunny and 31C in Dhaka.", model_used="fake-qwen")

    monkeypatch.setattr(repl, "LLMManager", FakeLLM)

    asyncio.run(
        _handle_request(
            "whats the weather in Dhaka today?",
            tmp_path,
            console,
            FakeWebTool(),
            BrowserTool(tmp_path, approval_func=lambda _request: False),
        )
    )

    rendered = output.getvalue()
    assert "Web Answer" in rendered
    assert "sunny and 31C" in rendered


def test_repl_followup_web_request_uses_previous_prompt(monkeypatch, tmp_path):
    console, output = _console_output()
    seen = []

    class FakeWebTool:
        def search(self, query: str, reason: str = "", top_k: int = 5):
            seen.append(query)
            return WebSearchResult(approved=False, query=query, error="Web search denied by user.")

        def fetch(self, url: str, reason: str = ""):  # pragma: no cover - not reached here
            raise AssertionError("fetch should not be called")

    class FakeLLM:
        def __init__(self, session_logger=None, model_pull_progress=None, action_ledger=None):
            self.session_logger = session_logger

        async def run_specialist(self, specialist, pack):
            return LLMResponse(raw="General fallback", model_used="fake-gemma")

    monkeypatch.setattr(repl, "LLMManager", FakeLLM)

    asyncio.run(
        _handle_request(
            "check on the web",
            tmp_path,
            console,
            FakeWebTool(),
            BrowserTool(tmp_path, approval_func=lambda _request: False),
            previous_user_prompt="whats the weather today?",
        )
    )

    assert seen == ["whats the weather today? Please check on the web for this."]
