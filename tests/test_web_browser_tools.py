from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from shamsu.cli import repl
from shamsu.session.manager import SessionManager
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import (
    SearchHit,
    SearxngProvider,
    WebConfig,
    WebFetchResult,
    WebSearchFetchResult,
    WebSearchResult,
    WebServiceManager,
    WebTool,
    build_evidence_answer_prompt,
    _extract_readable_text,
)


def test_web_tool_denied_search_skips_network(tmp_path, monkeypatch):
    called = []

    def fake_client():
        called.append(True)
        raise AssertionError("network should not be called")

    tool = WebTool(approval_func=lambda _request: False)
    monkeypatch.setattr(tool, "_client", fake_client)

    result = tool.search("latest django docs")

    assert not result.approved
    assert not called
    assert "denied" in result.error.lower()


def test_web_tool_logs_central_approval_result(tmp_path, monkeypatch):
    logger = SessionManager(tmp_path).create_session("Web")

    def fake_client():
        raise AssertionError("network should not be called")

    tool = WebTool(approval_func=lambda _request: False, session_logger=logger)
    monkeypatch.setattr(tool, "_client", fake_client)

    tool.search("latest django docs")

    event_types = [event["event_type"] for event in logger.tail(10)]
    assert "approval.request" in event_types
    assert "approval.result" in event_types


def test_web_tool_search_parses_results(monkeypatch):
    html = """
    <html><body>
    <a class="result__a" href="https://example.com/docs">Django docs</a>
    <a class="result__snippet">Authentication guide</a>
    </body></html>
    """

    class FakeResponse:
        text = html

        def raise_for_status(self):
            return None

    class FakeClient:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    tool = WebTool(approval_func=lambda _request: True, config=WebConfig(provider="duckduckgo"))
    monkeypatch.setattr(tool, "_client", lambda: FakeClient())

    result = tool.search("django auth docs")

    assert result.approved
    assert result.hits[0].title == "Django docs"
    assert result.hits[0].url == "https://example.com/docs"


def test_web_tool_decodes_duckduckgo_redirect_href(monkeypatch):
    html = """
    <html><body>
    <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fweather%3Fq%3DDhaka&amp;rut=abc">Weather</a>
    <a class="result__snippet">Dhaka weather forecast</a>
    </body></html>
    """

    class FakeResponse:
        text = html

        def raise_for_status(self):
            return None

    class FakeClient:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    tool = WebTool(approval_func=lambda _request: True, config=WebConfig(provider="duckduckgo"))
    monkeypatch.setattr(tool, "_client", lambda: FakeClient())

    result = tool.search("dhaka weather")

    assert result.hits[0].url == "https://example.com/weather?q=Dhaka"


def test_web_tool_fetch_can_skip_per_url_approval(monkeypatch):
    approvals = []

    class FakeResponse:
        text = "<html><title>Example</title><body>Useful content " * 40 + "</body></html>"
        url = "https://example.com"

        def raise_for_status(self):
            return None

    class FakeClient:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    tool = WebTool(approval_func=lambda request: approvals.append(request.preview) or True)
    monkeypatch.setattr(tool, "_client", lambda: FakeClient())

    result = tool.fetch("https://example.com", require_approval=False)

    assert result.approved
    assert approvals == []


def test_searxng_provider_parses_json_results():
    class FakeResponse:
        def json(self):
            return {
                "results": [
                    {
                        "title": "Python Docs",
                        "url": "https://docs.python.org/3/",
                        "content": "Official language docs",
                    }
                ]
            }

        def raise_for_status(self):
            return None

    class FakeClient:
        def get(self, url, params=None, headers=None):
            assert url == "http://localhost:8095/search"
            assert params["format"] == "json"
            return FakeResponse()

    hits = SearxngProvider(client_factory=lambda: FakeClient()).search("python docs")

    assert hits == [
        SearchHit(
            title="Python Docs",
            url="https://docs.python.org/3/",
            snippet="Official language docs",
            source_provider="searxng",
        )
    ]


def test_auto_provider_falls_back_to_duckduckgo(monkeypatch, tmp_path):
    html = """
    <html><body>
    <a class="result__a" href="https://example.com/docs">Fallback docs</a>
    <a class="result__snippet">Fallback snippet</a>
    </body></html>
    """

    class FakeResponse:
        text = html

        def raise_for_status(self):
            return None

    class FakeService:
        def status(self):
            return SimpleNamespace(running=False)

        def start(self):
            return SimpleNamespace(ok=False, message="docker missing")

    class FakeClient:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    tool = WebTool(
        approval_func=lambda _request: True,
        workspace=tmp_path,
        config=WebConfig(provider="auto", auto_start=True),
    )
    tool.service_manager = FakeService()
    monkeypatch.setattr(tool, "_client", lambda: FakeClient())

    result = tool.search("docs")

    assert result.provider == "duckduckgo"
    assert result.fallback_used is True
    assert result.hits[0].title == "Fallback docs"


def test_search_and_fetch_fetches_top_results_once(monkeypatch, tmp_path):
    approvals = []
    fetched = []
    tool = WebTool(
        approval_func=lambda request: approvals.append(request.description) or True,
        workspace=tmp_path,
        config=WebConfig(provider="duckduckgo", cache_enabled=False),
    )
    monkeypatch.setattr(
        tool,
        "_run_provider_search",
        lambda query, top_k: (
            [
                SearchHit("One", "https://example.com/1", "first"),
                SearchHit("Two", "https://example.com/2", "second"),
            ],
            "duckduckgo",
            False,
        ),
    )

    def fake_fetch(url, reason="", require_approval=True):
        fetched.append((url, require_approval))
        return WebFetchResult(
            approved=True,
            url=url,
            final_url=url,
            title=url,
            text="useful evidence about python docs " * 20,
            extraction_method="visible_text",
        )

    monkeypatch.setattr(tool, "fetch", fake_fetch)

    result = tool.search_and_fetch("python docs", search_top_k=2, fetch_top_k=2)

    assert result.approved
    assert approvals == ["Search the web and fetch the top results."]
    assert fetched == [("https://example.com/1", False), ("https://example.com/2", False)]
    assert len(result.pages) == 2
    assert result.evidence


def test_extraction_returns_method_and_fallback(monkeypatch):
    monkeypatch.setattr("shamsu.tools.web.trafilatura", None)

    extracted, method = _extract_readable_text("<html><body>Hello</body></html>", "https://example.com")

    assert extracted is None
    assert method == "none"


def test_evidence_answer_prompt_is_evidence_only():
    result = WebSearchFetchResult(
        approved=True,
        query="next fifa game in utc time",
        hits=[SearchHit("Fixtures", "https://example.com/fixtures", "snippet says maybe")],
        pages=[
            WebFetchResult(
                approved=True,
                url="https://example.com/fixtures",
                final_url="https://example.com/fixtures",
                title="Fixtures",
                text="FIFA fixture table kickoff 20:00 UTC",
                extraction_method="trafilatura_markdown",
            )
        ],
        provider="duckduckgo",
        query_type="schedule_time",
    )

    prompt = build_evidence_answer_prompt("next fifa game in utc time", result)

    assert "Answer only from fetched evidence" in prompt
    assert "Do not guess" in prompt
    assert "FIFA fixture table kickoff 20:00 UTC" in prompt
    assert "Sources fetched" in prompt


def test_web_service_manager_refuses_to_stop_unmanaged_container(tmp_path):
    commands = []

    def fake_runner(command):
        commands.append(command)
        if command[:2] == ["docker", "inspect"]:
            return SimpleNamespace(stdout="false\n")
        raise AssertionError("stop should not be called for unmanaged containers")

    manager = WebServiceManager(tmp_path, runner=fake_runner)

    status = manager.stop()

    assert not status.ok
    assert "Refusing" in status.message
    assert len(commands) == 1


def test_web_service_setup_writes_json_enabled_settings(tmp_path):
    manager = WebServiceManager(tmp_path)

    status = manager.setup()

    assert status.ok
    assert "json" in manager.settings_path.read_text(encoding="utf-8")
    compose = manager.compose_path.read_text(encoding="utf-8")
    assert "shamsu.managed=true" in compose
    assert "8095:8080" in compose


def test_web_answer_uses_snippet_fallback_when_fetches_empty():
    class FakeLLM:
        async def run_specialist(self, specialist, pack):
            assert specialist == "qa"
            assert "Dhaka weather forecast" in pack.prd_context
            return type("Response", (), {"raw": "Dhaka looks warm today based on the available snippets."})()

    console_file = StringIO()
    console = Console(file=console_file, force_terminal=False, width=100)
    result = WebSearchResult(
        approved=True,
        query="weather in Dhaka",
        hits=[
            SearchHit(
                title="Dhaka Weather",
                url="https://example.com/weather",
                snippet="Dhaka weather forecast is warm and humid.",
            )
        ],
    )

    asyncio.run(repl._print_web_answer("weather in Dhaka", result, [], console, FakeLLM()))

    rendered = console_file.getvalue()
    assert "Dhaka looks warm today" in rendered
    assert "Sources:" in rendered
    assert "- Dhaka Weather" not in rendered.split("Sources:", 1)[0]


def test_web_assist_uses_single_followup_fetch_approval(monkeypatch, tmp_path):
    approvals = []

    class FakeWebTool(WebTool):
        def __init__(self):
            super().__init__(approval_func=lambda request: approvals.append(request.description) or True)

        def search_and_fetch(self, query: str, reason: str = "", search_top_k: int = 8, fetch_top_k: int = 4):
            request = repl.ApprovalRequest(
                action_type="web_search",
                description="Search the web and fetch the top results.",
                risk_level="medium",
                preview=query,
            )
            assert self.approval_manager.ask(request)
            return WebSearchFetchResult(
                approved=True,
                query=query,
                hits=[
                    SearchHit("One", "https://example.com/1", "Snippet one"),
                    SearchHit("Two", "https://example.com/2", "Snippet two"),
                    SearchHit("Three", "https://example.com/3", "Snippet three"),
                ],
                pages=[
                    WebFetchResult(
                        approved=True,
                        url="https://example.com/1",
                        final_url="https://example.com/1",
                        title="One",
                        text="Useful web result content " * 20,
                        extraction_method="visible_text",
                    )
                ],
                provider="duckduckgo",
            )

    class FakeLLM:
        async def run_specialist(self, specialist, pack):
            assert "Answer only from fetched evidence" in pack.prd_context
            return type("Response", (), {"raw": "Synthesized answer."})()

    console_file = StringIO()
    console = Console(file=console_file, force_terminal=False, width=100)

    asyncio.run(repl._run_web_assist("query", console, FakeLLM(), FakeWebTool()))

    assert approvals == ["Search the web and fetch the top results."]
    assert "Synthesized answer." in console_file.getvalue()


def test_browser_tool_requires_open_before_read(tmp_path):
    result = BrowserTool(tmp_path, approval_func=lambda _request: True).read()

    assert not result.ok
    assert "No browser page is open yet" in result.message


def test_browser_tool_logs_screenshot_event(tmp_path, monkeypatch):
    logger = SessionManager(tmp_path).create_session("Browser")
    tool = BrowserTool(tmp_path, approval_func=lambda _request: True, session_logger=logger)

    class FakePage:
        url = "http://127.0.0.1:8000"

        def screenshot(self, path: str, full_page: bool = True):
            Path(path).write_bytes(b"png")

    monkeypatch.setattr(tool, "_page", FakePage())

    result = tool.screenshot()
    events = [event["event_type"] for event in logger.tail(5)]

    assert result.ok
    assert "browser.screenshot" in events
    assert result.screenshot_path.endswith(".png")


def test_web_fetch_result_shape_for_useful_page():
    result = WebFetchResult(
        approved=True,
        url="https://example.com",
        title="Example",
        text="Useful content " * 20,
    )

    assert result.approved
    assert len(result.text) > 120
