from __future__ import annotations

import asyncio
from pathlib import Path
from io import StringIO

from rich.console import Console

from shamsu.cli import repl
from shamsu.session.manager import SessionManager
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import SearchHit, WebFetchResult, WebSearchResult, WebTool


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

    tool = WebTool(approval_func=lambda _request: True)
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

    tool = WebTool(approval_func=lambda _request: True)
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

        def search(self, query: str, reason: str = "", top_k: int = 5):
            request = repl.ApprovalRequest(
                action_type="web_search",
                description="Search the web for current or external information.",
                risk_level="medium",
                preview=query,
            )
            assert self.approval_manager.ask(request)
            return WebSearchResult(
                approved=True,
                query=query,
                hits=[
                    SearchHit("One", "https://example.com/1", "Snippet one"),
                    SearchHit("Two", "https://example.com/2", "Snippet two"),
                    SearchHit("Three", "https://example.com/3", "Snippet three"),
                ],
            )

        def fetch(self, url: str, reason: str = "", require_approval: bool = True):
            assert require_approval is False
            return WebFetchResult(
                approved=True,
                url=url,
                title=url,
                text="Useful web result content " * 20,
            )

    class FakeLLM:
        async def run_specialist(self, specialist, pack):
            return type("Response", (), {"raw": "Synthesized answer."})()

    console_file = StringIO()
    console = Console(file=console_file, force_terminal=False, width=100)

    asyncio.run(repl._run_web_assist("query", console, FakeLLM(), FakeWebTool()))

    assert approvals == [
        "Search the web for current or external information.",
        "Fetch and read the top web search results.",
    ]
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
