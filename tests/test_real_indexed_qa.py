from __future__ import annotations

import asyncio
from io import StringIO

from rich.console import Console

from shamsu.abstract.service import AbstractService
from shamsu.cli import repl
from shamsu.cli.repl import _build_workspace_qa_workflow, _handle_request
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import SearchHit, WebSearchResult, WebTool
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


def test_repl_greeting_routes_to_general_chat(monkeypatch, tmp_path):
    """A bare greeting is pure small talk: it gets a lightweight conversational
    reply, never the agent loop / task router (which used to answer "hi" with a
    "QA task" and a fabricated plan). Also asserts no routing jargon leaks."""
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)

    async def _fake_general_chat(user_input, console, llm, **kwargs):
        assert user_input == "hi"
        console.print("Hey, I am here.")

    monkeypatch.setattr(repl, "_run_general_chat", _fake_general_chat)
    monkeypatch.setattr(repl, "_make_llm_manager", lambda *a, **k: object())

    class _BoomLoop:
        def __init__(self, *a, **k):
            pass

        async def run(self, user_input):
            raise AssertionError("a greeting must not reach the agent loop")

    monkeypatch.setattr(repl, "AgentChatLoop", _BoomLoop)

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


def test_repl_explicit_read_file_prompt_reads_before_answer(monkeypatch, tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)
    packs = []
    target = tmp_path / "shamsu" / "llm"
    target.mkdir(parents=True)
    (target / "output.py").write_text(
        "def parse_model_turn(response, registered=None):\n"
        "    \"\"\"Normalize model output and salvage tool calls.\"\"\"\n"
        "    return response\n",
        encoding="utf-8",
    )

    class FakeAgentChatLoop:
        def __init__(self, *args, **kwargs):
            raise AssertionError("explicit read_file prompts should not route to generic agent chat")

    class FakeLLM:
        def __init__(self, session_logger=None, model_pull_progress=None, action_ledger=None):
            self.session_logger = session_logger

        async def run_specialist(self, specialist, pack):
            assert specialist == "qa"
            packs.append(pack)
            return LLMResponse(raw="parse_model_turn normalizes model output.", model_used="fake")

    monkeypatch.setattr(repl, "AgentChatLoop", FakeAgentChatLoop)
    monkeypatch.setattr(repl, "LLMManager", FakeLLM)

    asyncio.run(
        _handle_request(
            "Use read_file on shamsu/llm/output.py. Then explain parse_model_turn.",
            tmp_path,
            console,
            web_tool,
            browser_tool,
        )
    )

    rendered = output.getvalue()
    assert "File Answer" in rendered
    assert "parse_model_turn normalizes model output." in rendered
    assert packs
    assert "Normalize model output and salvage tool calls" in packs[0].prd_context
    assert "The file has already been read successfully" in packs[0].user_request
    assert "Codebase-Memory MCP is not ready" not in rendered


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


# ---------------------------------------------------------------------------
# Gap H1 (cheap half): zero-hit rescue. FTS treats a multi-word query as one
# unit, so "where is authentication handled" found nothing unless a file
# contained that phrase. A miss now retries the meaningful words individually
# and unions the hits. Embeddings (true semantic search) remain open.
# ---------------------------------------------------------------------------


def test_zero_hit_multiword_query_is_rescued_per_word(tmp_path):
    from shamsu.retriever.search import SearchAgent

    class _Adapter:
        def __init__(self):
            self.queries = []

        def search_code(self, workspace, query, limit=5):
            self.queries.append(query)
            if query == "authentication":
                return {"ok": True, "results": [
                    {"node": "login", "file": "auth.py", "start_line": 1, "end_line": 2}
                ]}
            return {"ok": True, "results": []}

    adapter = _Adapter()
    agent = SearchAgent(tmp_path, adapter=adapter)
    (tmp_path / "auth.py").write_text("def login():\n    pass\n", encoding="utf-8")

    hits = agent.search("where is authentication handled", top_k=5)

    assert [h.file_path for h in hits] == ["auth.py"]
    # The full query ran first; stopwords ("where", "handled") never retried.
    assert adapter.queries[0] == "where is authentication handled"
    assert "authentication" in adapter.queries
    assert "where" not in adapter.queries[1:]
    assert "handled" not in adapter.queries[1:]


def test_single_word_miss_is_not_retried(tmp_path):
    from shamsu.retriever.search import SearchAgent

    class _Adapter:
        def __init__(self):
            self.queries = []

        def search_code(self, workspace, query, limit=5):
            self.queries.append(query)
            return {"ok": True, "results": []}

    adapter = _Adapter()
    agent = SearchAgent(tmp_path, adapter=adapter)

    assert agent.search("authentication", top_k=5) == []
    assert adapter.queries == ["authentication"], "one word already ran as-is"


def test_hit_path_pays_no_rescue_cost(tmp_path):
    from shamsu.retriever.search import SearchAgent

    class _Adapter:
        def __init__(self):
            self.queries = []

        def search_code(self, workspace, query, limit=5):
            self.queries.append(query)
            return {"ok": True, "results": [
                {"node": "x", "file": "a.py", "start_line": 1, "end_line": 1}
            ]}

    adapter = _Adapter()
    agent = SearchAgent(tmp_path, adapter=adapter)
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")

    agent.search("game loop timing", top_k=5)
    assert len(adapter.queries) == 1, "a query with hits must not fan out"
