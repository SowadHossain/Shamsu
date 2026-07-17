"""Gap D1: the agent loop had no web access.

Web was a pre-routed keyword path decided BEFORE the agent started, so a task
that needed both files and a docs lookup got only one of the two - mid-build,
the agent guessed library APIs from 7B weights while a working WebTool sat in
the same process, unregistered. These adapters expose it; WebTool keeps its own
approval gate and SHAMSU_WEB_ENABLED kill switch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shamsu.tools.agent_tools import AgentToolRegistry


@dataclass
class _Hit:
    title: str
    url: str
    snippet: str = ""


@dataclass
class _SearchResult:
    approved: bool
    query: str
    hits: list[_Hit] = field(default_factory=list)
    error: str = ""
    provider: str = "fake"
    fallback_used: bool = False


@dataclass
class _FetchResult:
    approved: bool
    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    excerpt: str = ""
    error: str = ""


class _FakeWebTool:
    def __init__(self, search_result=None, fetch_result=None) -> None:
        self._search = search_result
        self._fetch = fetch_result
        self.searches: list[str] = []
        self.fetches: list[str] = []

    def search(self, query, reason="", top_k=5):  # noqa: ANN001
        self.searches.append(query)
        return self._search

    def fetch(self, url, reason="", require_approval=True):  # noqa: ANN001
        self.fetches.append(url)
        return self._fetch


def _registry(tmp_path: Path, web_tool) -> AgentToolRegistry:
    return AgentToolRegistry(tmp_path, approval_func=lambda _r: True, web_tool=web_tool)


def test_web_search_returns_budgetable_hits(tmp_path: Path):
    fake = _FakeWebTool(
        search_result=_SearchResult(
            approved=True,
            query="flask jwt",
            hits=[_Hit("Flask-JWT docs", "https://example.com/jwt", "how to issue tokens")],
        )
    )
    result = _registry(tmp_path, fake).execute("web_search", {"query": "flask jwt"})

    assert result.ok
    assert fake.searches == ["flask jwt"]
    assert result.data["results"][0]["url"] == "https://example.com/jwt"


def test_web_search_denial_is_an_honest_failure(tmp_path: Path):
    fake = _FakeWebTool(
        search_result=_SearchResult(approved=False, query="q", error="Web search denied by user.")
    )
    result = _registry(tmp_path, fake).execute("web_search", {"query": "q"})

    assert not result.ok
    assert "denied" in result.message.lower()


def test_fetch_url_returns_capped_text(tmp_path: Path):
    fake = _FakeWebTool(
        fetch_result=_FetchResult(
            approved=True,
            url="https://example.com/docs",
            final_url="https://example.com/docs",
            title="Docs",
            text="x" * 50_000,
        )
    )
    result = _registry(tmp_path, fake).execute("fetch_url", {"url": "https://example.com/docs"})

    assert result.ok
    assert len(result.data["text"]) <= 12_000   # never floods the loop history


def test_empty_inputs_fail_cleanly(tmp_path: Path):
    registry = _registry(tmp_path, _FakeWebTool())
    assert not registry.execute("web_search", {"query": "  "}).ok
    assert not registry.execute("fetch_url", {"url": ""}).ok


def test_web_tools_are_exposed_to_the_model(tmp_path: Path):
    registry = _registry(tmp_path, _FakeWebTool())
    names = {(schema.get("function") or {}).get("name") for schema in registry.tool_schemas()}
    assert {"web_search", "fetch_url"} <= names


def test_a_broken_web_tool_never_breaks_the_loop(tmp_path: Path):
    class _Exploding:
        def search(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("provider down")

        def fetch(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("provider down")

    registry = _registry(tmp_path, _Exploding())
    assert not registry.execute("web_search", {"query": "q"}).ok
    assert not registry.execute("fetch_url", {"url": "https://x.example"}).ok
