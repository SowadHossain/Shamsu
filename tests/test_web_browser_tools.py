from __future__ import annotations

from pathlib import Path

from shamsu.session.manager import SessionManager
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import WebFetchResult, WebTool


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
