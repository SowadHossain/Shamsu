from __future__ import annotations

import asyncio
import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

from shamsu.action_ledger import store
from shamsu.action_ledger.ledger import start_run
from shamsu.cli import repl
from shamsu.session.manager import SessionManager
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import (
    SearchHit,
    SearxngProvider,
    WebCache,
    WebConfig,
    WebFetchResult,
    WebSearchFetchResult,
    WebSearchResult,
    WebServiceManager,
    WebServiceStatus,
    WebTool,
    _extract_readable_text,
    build_evidence_answer_prompt,
)


def test_web_cache_closes_sqlite_connection_after_each_operation(tmp_path):
    path = tmp_path / "web_cache.db"
    cache = WebCache(path)
    cache.record_hits("query", "test", [SearchHit("Title", "https://example.com", "Snippet")])

    path.unlink()

    assert path.exists() is False


def test_web_cache_initializes_lazily(tmp_path):
    path = tmp_path / "web_cache.db"
    cache = WebCache(path)

    assert path.exists() is False

    cache.record_hits("query", "test", [])
    assert path.is_file()


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


def test_web_search_uses_search_shaped_query_for_provider(monkeypatch):
    queries = []

    class FakeProvider:
        name = "duckduckgo"

        def search(self, query, _top_k):
            queries.append(query)
            return [SearchHit("Python 3.13", "https://python.org/", "Release", self.name)]

    tool = WebTool(approval_func=lambda _request: True, config=WebConfig(provider="duckduckgo"))
    monkeypatch.setattr(
        "shamsu.tools.web.DuckDuckGoHtmlProvider",
        lambda client_factory=None: FakeProvider(),
    )

    result = tool.search(
        "Use web search to find the official Python 3.13 release date. "
        "Give the date and source URL. Do not modify files."
    )

    assert result.hits
    assert queries == ["the official Python 3.13 release date"]


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
    assert [item["provider"] for item in result.provider_attempts] == ["searxng", "duckduckgo"]
    assert [item["state"] for item in result.provider_attempts] == ["failed", "success"]


def test_web_status_reports_each_capability_without_probing_external_network(tmp_path):
    tool = WebTool(workspace=tmp_path, config=WebConfig(provider="auto", cache_enabled=True))
    tool.service_manager = SimpleNamespace(
        status=lambda: WebServiceStatus(
            ok=False,
            message="SearXNG is not reachable.",
            running=False,
            state="not_running",
        )
    )

    status = tool.status()

    assert status.enabled is True
    assert status.provider_mode == "auto"
    assert status.searxng.state == "not_running"
    assert status.fallback_state == "configured"
    assert status.fetch_state == "configured_not_probed"
    assert status.cache_state == "enabled"
    assert status.ok is True


def test_web_search_log_records_ranked_provider_metadata(tmp_path, monkeypatch):
    logger = SessionManager(tmp_path).create_session("Web metadata")
    tool = WebTool(
        workspace=tmp_path,
        session_logger=logger,
        approval_func=lambda _request: True,
        config=WebConfig(provider="duckduckgo"),
    )
    monkeypatch.setattr(
        tool,
        "_run_provider_search",
        lambda _query, _top_k: (
            [SearchHit("Official Docs", "https://example.com/docs", "Reference", "duckduckgo")],
            "duckduckgo",
            False,
        ),
    )

    tool.search("official docs")

    event = next(item for item in reversed(logger.tail(20)) if item["event_type"] == "web.search.finished")
    record = event["payload"]["results"][0]
    assert record["rank"] == 1
    assert record["title"] == "Official Docs"
    assert record["provider"] == "duckduckgo"
    assert record["retrieved_at"]


def test_web_events_mirror_ranked_results_to_action_ledger(tmp_path, monkeypatch):
    events = []
    ledger = SimpleNamespace(log_event=lambda event_type, **payload: events.append((event_type, payload)))
    tool = WebTool(
        workspace=tmp_path,
        approval_func=lambda _request: True,
        action_ledger=ledger,
        config=WebConfig(provider="duckduckgo"),
    )
    monkeypatch.setattr(
        tool,
        "_run_provider_search",
        lambda _query, _top_k: (
            [SearchHit("Official Docs", "https://example.com/docs", "Reference", "duckduckgo")],
            "duckduckgo",
            False,
        ),
    )

    tool.search("official docs")

    finished = next(payload for event, payload in events if event == "web_search_finished")
    assert finished["results"][0]["rank"] == 1
    assert finished["results"][0]["url"] == "https://example.com/docs"


def test_web_fetch_blocks_local_and_private_targets_before_network(tmp_path, monkeypatch):
    tool = WebTool(workspace=tmp_path, approval_func=lambda _request: True)
    called = []
    monkeypatch.setattr(tool, "_client", lambda: called.append(True))

    local = tool.fetch("http://127.0.0.1:8000/private")
    private = tool.fetch("http://192.168.1.10/secrets")

    assert not local.approved
    assert not private.approved
    assert "browser tool" in local.error.lower()
    assert "private" in private.error.lower()
    assert called == []


def test_web_fetch_blocks_redirect_to_local_target(tmp_path, monkeypatch):
    class RedirectResponse:
        is_redirect = True
        headers = {"location": "http://127.0.0.1:8019/admin/"}

    class FakeClient:
        def get(self, *_args, **_kwargs):
            return RedirectResponse()

    tool = WebTool(workspace=tmp_path, approval_func=lambda _request: True)
    monkeypatch.setattr(tool, "_client", lambda: FakeClient())

    result = tool.fetch("https://example.com/redirect")

    assert result.approved
    assert "browser tool" in result.error.lower()


def test_web_search_blocks_private_workspace_path_before_approval(tmp_path):
    approvals = []
    tool = WebTool(
        workspace=tmp_path,
        approval_func=lambda request: approvals.append(request) or True,
    )

    result = tool.search(f"upload and explain {tmp_path / 'private.py'}")

    assert not result.approved
    assert "private workspace path" in result.error.lower()
    assert approvals == []


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


def test_web_service_start_sets_up_and_reports_missing_docker(tmp_path, monkeypatch):
    monkeypatch.setattr("shamsu.tools.web.shutil.which", lambda _name: None)
    manager = WebServiceManager(tmp_path)

    status = manager.start()

    assert not status.ok
    assert status.state == "missing_docker"
    assert manager.compose_path.exists()
    assert "Docker is not installed" in status.message


def test_web_service_start_surfaces_compose_failure(tmp_path, monkeypatch):
    commands = []

    def fake_runner(command):
        commands.append(command)
        if command[:3] == ["docker", "compose", "version"] or command[:2] == ["docker", "info"]:
            return subprocess.CompletedProcess(command, 0, "ok", "")
        if command[:3] == ["docker", "compose", "-p"]:
            return subprocess.CompletedProcess(command, 1, "", "port is already allocated: 8095")
        return subprocess.CompletedProcess(command, 1, "", "not found")

    monkeypatch.setattr("shamsu.tools.web.shutil.which", lambda _name: "docker")
    monkeypatch.setattr(
        "shamsu.tools.web.socket.create_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
    )
    manager = WebServiceManager(tmp_path, runner=fake_runner)

    status = manager.start()

    assert not status.ok
    assert status.state == "failed"
    assert "port conflict" in status.message
    assert any(command[:3] == ["docker", "compose", "-p"] for command in commands)


def test_explicit_web_search_requires_local_searxng_after_approval(tmp_path, monkeypatch):
    class FakeService:
        def status(self):
            return SimpleNamespace(running=False)

        def start(self):
            return SimpleNamespace(ok=False, message="Docker is not installed")

    tool = WebTool(
        approval_func=lambda _request: True,
        workspace=tmp_path,
        config=WebConfig(provider="auto", cache_enabled=False),
    )
    tool.service_manager = FakeService()
    monkeypatch.setattr(tool, "_client", lambda: (_ for _ in ()).throw(AssertionError("search should not fall back")))

    result = tool.search_and_fetch("weather", require_local_service=True)

    assert result.approved
    assert result.error == "Docker is not installed"
    assert not result.hits


def test_web_search_command_allows_configured_provider_fallback():
    calls = []

    class FakeWebTool:
        service_manager = SimpleNamespace()

        def search_and_fetch(self, query, reason="", require_local_service=False):
            calls.append((query, require_local_service))
            return WebSearchFetchResult(approved=True, query=query, hits=[])

    console = Console(file=StringIO(), force_terminal=False)

    repl._handle_web("web search django docs", console, FakeWebTool(), SimpleNamespace())

    assert calls == [("django docs", False)]


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


def test_pre_routed_web_assist_records_canonical_tool_call(tmp_path):
    ledger = start_run(tmp_path, "use web search for official docs")

    class FakeWebTool:
        action_ledger = ledger

        def search_and_fetch(self, query: str, reason: str = ""):
            return WebSearchFetchResult(
                approved=True,
                query=query,
                hits=[SearchHit("Official", "https://example.com/docs", "Reference")],
                pages=[WebFetchResult(
                    approved=True,
                    url="https://example.com/docs",
                    final_url="https://example.com/docs",
                    title="Official",
                    text="Official documentation evidence " * 20,
                )],
                provider="fake",
            )

    class FakeLLM:
        async def run_specialist(self, specialist, pack):
            return SimpleNamespace(raw="Sourced answer.", model_used="fake")

    asyncio.run(
        repl._run_web_assist(
            "official docs", Console(record=True), FakeLLM(), FakeWebTool()
        )
    )

    records = store.load_tool_calls(tmp_path, ledger.run_id)
    assert [record["phase"] for record in records] == ["called", "finished"]
    assert records[0]["tool"] == "web_search"
    assert records[1]["ok"] is True
    assert records[1]["data"]["sources"][0]["url"] == "https://example.com/docs"


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


def test_browser_status_distinguishes_missing_dependency(tmp_path, monkeypatch):
    tool = BrowserTool(tmp_path)
    monkeypatch.setattr(
        tool,
        "_load_playwright",
        lambda: (_ for _ in ()).throw(RuntimeError("Playwright is not available.")),
    )

    status = tool.status()

    assert status.available is False
    assert status.state == "missing_dependency"
    assert "Playwright" in status.message


def test_browser_open_reports_console_errors(tmp_path, monkeypatch):
    tool = BrowserTool(tmp_path, approval_func=lambda _request: True)

    class Locator:
        def inner_text(self, timeout=3000):
            return "Page body"

    class Page:
        url = "http://127.0.0.1:8000/"

        def goto(self, *_args, **_kwargs):
            tool._console_errors.append("Uncaught TypeError")

        def title(self):
            return "Local App"

        def locator(self, _selector):
            return Locator()

    monkeypatch.setattr(tool, "_ensure_page", lambda: setattr(tool, "_page", Page()))

    result = tool.open("http://127.0.0.1:8000/")

    assert result.ok
    assert result.console_errors == ("Uncaught TypeError",)


def test_browser_events_mirror_to_action_ledger(tmp_path, monkeypatch):
    events = []
    ledger = SimpleNamespace(log_event=lambda event_type, **payload: events.append((event_type, payload)))
    tool = BrowserTool(
        tmp_path,
        approval_func=lambda _request: True,
        action_ledger=ledger,
    )

    class FakePage:
        url = "http://127.0.0.1:8000"

        def screenshot(self, path: str, full_page: bool = True):
            Path(path).write_bytes(b"png")

    monkeypatch.setattr(tool, "_page", FakePage())

    tool.screenshot()

    assert events[0][0] == "browser_screenshot"
    assert events[0][1]["path"].endswith(".png")


def test_real_browser_reads_local_fixture_captures_console_and_screenshot(tmp_path):
    tool = BrowserTool(tmp_path, approval_func=lambda _request: True)
    status = tool.status()
    if not status.available:
        pytest.skip(status.message)
    page = tmp_path / "browser-fixture.html"
    page.write_text(
        "<html><head><title>Browser Fixture</title></head>"
        "<body><main>Local browser evidence</main>"
        "<script>console.error('fixture console error')</script></body></html>",
        encoding="utf-8",
    )

    try:
        opened = tool.open(page.as_uri())
        screenshot = tool.screenshot()
    finally:
        tool.close()

    assert opened.ok
    assert opened.title == "Browser Fixture"
    assert "Local browser evidence" in opened.visible_text
    assert opened.console_errors == ("fixture console error",)
    assert screenshot.ok
    assert Path(screenshot.screenshot_path).is_file()


def test_web_fetch_result_shape_for_useful_page():
    result = WebFetchResult(
        approved=True,
        url="https://example.com",
        title="Example",
        text="Useful content " * 20,
    )

    assert result.approved
    assert len(result.text) > 120
