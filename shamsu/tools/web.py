"""Permission-gated web search and page fetch helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import quote_plus, urlparse

import httpx

try:
    import trafilatura
except ModuleNotFoundError:  # pragma: no cover - exercised when old/global launchers miss deps
    trafilatura = None

from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.session.manager import SessionLogger
from shamsu.types import ApprovalRequest

DEFAULT_USER_AGENT = "SHAMSU/0.3.0 (+local coding agent)"


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class WebSearchResult:
    approved: bool
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True)
class WebFetchResult:
    approved: bool
    url: str
    title: str = ""
    text: str = ""
    error: str = ""


class WebTool:
    def __init__(
        self,
        approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
        session_logger: SessionLogger | None = None,
        timeout_seconds: int = 15,
    ) -> None:
        self.approval_func = approval_func
        self.approval_manager = ApprovalManager(approval_func, session_logger)
        self.session_logger = session_logger
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, reason: str = "", top_k: int = 5) -> WebSearchResult:
        request = ApprovalRequest(
            action_type="web_search",
            description="Search the web for current or external information.",
            risk_level="medium",
            preview=query,
            reason=reason or "This request appears to need external knowledge.",
        )
        self._log("web.search.requested", {"query": query, "reason": reason}, f"Requested web search: {query}")
        self.approval_manager.session_logger = self.session_logger
        if not self.approval_manager.ask(request):
            self._log("web.search.denied", {"query": query}, f"Denied web search: {query}")
            return WebSearchResult(approved=False, query=query, error="Web search denied by user.")

        self._log("web.search.started", {"query": query}, f"Started web search: {query}")
        try:
            response = self._client().get(
                f"https://html.duckduckgo.com/html/?q={quote_plus(query)}",
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
            response.raise_for_status()
            hits = _DuckDuckGoParser().parse(response.text)[:top_k]
            self._log(
                "web.search.finished",
                {"query": query, "hit_count": len(hits), "urls": [item.url for item in hits]},
                f"Finished web search: {query}",
            )
            return WebSearchResult(approved=True, query=query, hits=hits)
        except Exception as exc:
            message = str(exc)
            self._log("web.search.failed", {"query": query, "error": message}, f"Failed web search: {query}")
            return WebSearchResult(approved=True, query=query, error=message)

    def fetch(self, url: str, reason: str = "") -> WebFetchResult:
        request = ApprovalRequest(
            action_type="web_search",
            description="Fetch and read a web page.",
            risk_level="medium",
            preview=url,
            reason=reason or "SHAMSU wants to inspect an external page for this request.",
        )
        self._log("web.fetch.requested", {"url": url, "reason": reason}, f"Requested fetch: {url}")
        self.approval_manager.session_logger = self.session_logger
        if not self.approval_manager.ask(request):
            self._log("web.fetch.denied", {"url": url}, f"Denied fetch: {url}")
            return WebFetchResult(approved=False, url=url, error="Web fetch denied by user.")

        self._log("web.fetch.started", {"url": url}, f"Started fetch: {url}")
        try:
            response = self._client().get(url, headers={"User-Agent": DEFAULT_USER_AGENT}, follow_redirects=True)
            response.raise_for_status()
            extracted = _extract_readable_text(response.text, str(response.url))
            title_parser = _VisibleTextParser()
            title_parser.feed(response.text)
            if extracted:
                text = _normalize_space(extracted)[:8000]
            else:
                text = title_parser.text()[:8000]
            title = title_parser.title.strip() or _hostname(url)
            self._log("web.fetch.finished", {"url": url, "title": title}, f"Finished fetch: {url}")
            return WebFetchResult(approved=True, url=str(response.url), title=title, text=text)
        except Exception as exc:
            message = str(exc)
            self._log("web.fetch.failed", {"url": url, "error": message}, f"Failed fetch: {url}")
            return WebFetchResult(approved=True, url=url, error=message)

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout_seconds)

    def _log(self, event_type: str, payload: dict, summary: str) -> None:
        if self.session_logger:
            self.session_logger.log(event_type, payload, summary, workflow_id="web")


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[SearchHit] = []
        self._in_link = False
        self._capture_snippet = False
        self._current_href = ""
        self._current_title: list[str] = []
        self._current_snippet: list[str] = []

    def parse(self, html: str) -> list[SearchHit]:
        self.feed(html)
        return self.results

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        classes = attributes.get("class", "")
        if tag == "a" and "result__a" in classes:
            self._in_link = True
            self._current_href = attributes.get("href", "")
            self._current_title = []
            self._current_snippet = []
        elif tag in {"a", "div"} and "result__snippet" in classes:
            self._capture_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_link:
            title = _normalize_space("".join(self._current_title))
            if title and self._current_href:
                self.results.append(SearchHit(title=title, url=self._current_href, snippet=""))
            self._in_link = False
        elif self._capture_snippet and tag in {"a", "div"}:
            if self.results:
                last = self.results[-1]
                self.results[-1] = SearchHit(
                    last.title,
                    last.url,
                    _normalize_space("".join(self._current_snippet)),
                )
            self._current_snippet = []
            self._capture_snippet = False

    def handle_data(self, data: str) -> None:
        if self._in_link:
            self._current_title.append(data)
        elif self._capture_snippet:
            self._current_snippet.append(data)


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False
        self._title_parts: list[str] = []

    @property
    def title(self) -> str:
        return _normalize_space("".join(self._title_parts))

    def text(self) -> str:
        return _normalize_space(" ".join(self._parts))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
            return
        if self._skip_depth:
            return
        cleaned = _normalize_space(data)
        if cleaned:
            self._parts.append(cleaned)


def _normalize_space(text: str) -> str:
    return " ".join(unescape(text).split())


def _extract_readable_text(html: str, url: str) -> str | None:
    if trafilatura is None:
        return None
    return trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
    )


def _hostname(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or url
