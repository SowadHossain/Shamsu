"""
shamsu/retriever/search.py

ISearchAgent implementation backed by the real, locally-installed
Codebase-Memory MCP tool via CodebaseMemoryAdapter. This is SHAMSU's only
search/symbol-lookup backend - there is no SHAMSU-owned index, parser, or
code graph here; every result traces back to a real tool call
(`search_code`, `search_graph`, `get_code_snippet`).
"""
from __future__ import annotations

import re
from pathlib import Path

from shamsu.action_ledger.context import get_current_run
from shamsu.interfaces import ISearchAgent
from shamsu.tools.codebase_memory import CodebaseMemoryAdapter
from shamsu.types import SearchResult

_LANGUAGE_BY_EXTENSION = {
    ".c": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".go": "go",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".kt": "kotlin",
    ".md": "markdown",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "shell",
    ".sql": "sql",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".txt": "text",
    ".yaml": "yaml",
    ".yml": "yaml",
}

# Bounds how many symbol_lookup() rows get a get_code_snippet() follow-up
# call - each is a fresh subprocess invocation of the (stateless, per-call)
# CLI, so this keeps interactive latency bounded.
MAX_SYMBOL_SNIPPETS = 5

# Words that carry no search signal in a zero-hit per-word retry. English
# question scaffolding plus generic code nouns that match everything.
_RESCUE_STOPWORDS = frozenset(
    {
        "where", "what", "when", "which", "does", "how", "why", "who",
        "the", "this", "that", "with", "from", "into", "have", "has",
        "handled", "handle", "should", "would", "could", "about",
        "code", "file", "files", "function", "class", "method", "project",
    }
)


def _detect_language(file_path: str) -> str:
    return _LANGUAGE_BY_EXTENSION.get(Path(file_path).suffix.lower(), "text")


class SearchAgent(ISearchAgent):
    """Real implementation, backed by Codebase-Memory MCP."""

    def __init__(self, workspace_root: Path, adapter: CodebaseMemoryAdapter | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.adapter = adapter or CodebaseMemoryAdapter()

    def search(
        self,
        query: str,
        top_k: int = 5,
        boost_paths: list[str] | None = None,
    ) -> list[SearchResult]:
        """search_code first (broad recall over indexed file text), with a
        small additive boost for results whose file matches boost_paths
        (e.g. an exact traceback location from BugFixWorkflow) - a boost
        never replaces the tool's own ranking, only reorders it."""
        results = self.fts_search(query, top_k=max(top_k * 2, top_k))
        if not results:
            # Zero-hit rescue (gap H1, cheap half): FTS treats a multi-word
            # query as one unit, so "where is authentication handled" finds
            # nothing unless a file contains that phrase - the code says
            # `login`/`session`. Retry the meaningful words individually and
            # union the hits. Costs nothing on the hit path, no model call;
            # true semantic retrieval (embeddings) remains open.
            results = self._per_word_rescue(query, top_k)
        if not results:
            return results
        boost_terms = [p.lower().replace("\\", "/") for p in (boost_paths or []) if p]
        # Boost must exceed the highest possible unboosted score (len(results),
        # from the position-based ranking in fts_search) so an exact
        # traceback/error-location match always outranks the tool's own order.
        boost_amount = float(len(results)) + 1.0
        boosted: list[SearchResult] = []
        for result in results:
            score = result.score
            path_lower = result.file_path.lower().replace("\\", "/")
            if any(path_lower.endswith(term) or term.endswith(path_lower) for term in boost_terms):
                score += boost_amount
            boosted.append(
                SearchResult(
                    file_path=result.file_path,
                    language=result.language,
                    line_start=result.line_start,
                    line_end=result.line_end,
                    content=result.content,
                    score=score,
                    symbol_name=result.symbol_name,
                    chunk_type=result.chunk_type,
                )
            )
        boosted.sort(key=lambda r: r.score, reverse=True)
        return boosted[:top_k]

    def _per_word_rescue(self, query: str, top_k: int) -> list[SearchResult]:
        """Union of per-word FTS results for a query that matched nothing whole.

        Only the meaningful words retry (4+ chars, not stopwords), capped at 4
        lookups; duplicate files keep their best-scoring hit. The only pointless
        retry is an IDENTICAL one - "where is authentication handled" boiling
        down to just "authentication" is still a brand-new query.
        """
        words = [
            word
            for word in re.findall(r"[a-z0-9_]+", query.lower())
            if len(word) >= 4 and word not in _RESCUE_STOPWORDS
        ]
        if not words:
            return []
        if len(words) == 1 and words[0] == query.strip().lower():
            return []
        merged: dict[tuple[str, int], SearchResult] = {}
        for word in words[:4]:
            for hit in self.fts_search(word, top_k=top_k):
                key = (hit.file_path, hit.line_start)
                if key not in merged or hit.score > merged[key].score:
                    merged[key] = hit
        return sorted(merged.values(), key=lambda r: r.score, reverse=True)[: top_k * 2]

    def fts_search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        result = self.adapter.search_code(self.workspace_root, query, limit=top_k)
        ledger = get_current_run()
        if ledger:
            ledger.log_code_memory_queried("search_code", query, len((result or {}).get("results") or []))
        if not result.get("ok"):
            return []
        matches = result.get("results") or []
        out: list[SearchResult] = []
        total = len(matches)
        for position, match in enumerate(matches[:top_k]):
            file_path = match.get("file", "")
            line_start = match.get("start_line", 1)
            line_end = match.get("end_line", line_start)
            content = self._read_snippet(file_path, line_start, line_end)
            out.append(
                SearchResult(
                    file_path=file_path,
                    language=_detect_language(file_path),
                    line_start=line_start,
                    line_end=line_end,
                    content=content,
                    score=float(total - position),
                    symbol_name=match.get("node"),
                    chunk_type="function",
                )
            )
        return out

    def symbol_lookup(self, name: str) -> list[SearchResult]:
        result = self.adapter.get_symbols(self.workspace_root, name)
        ledger = get_current_run()
        if ledger:
            ledger.log_code_memory_queried("get_symbols", name, len((result or {}).get("results") or []))
        if not result.get("ok"):
            return []
        rows = result.get("results") or []
        out: list[SearchResult] = []
        for row in rows[:MAX_SYMBOL_SNIPPETS]:
            file_path = row.get("file_path", "")
            qualified_name = row.get("qualified_name", "")
            snippet = self.adapter.get_code_snippet(self.workspace_root, qualified_name) if qualified_name else {}
            if snippet.get("ok"):
                content = snippet.get("source", "")
                line_start = snippet.get("start_line", 1)
                line_end = snippet.get("end_line", line_start)
            else:
                content = row.get("signature", "")
                line_start = line_end = 1
            out.append(
                SearchResult(
                    file_path=file_path,
                    language=_detect_language(file_path),
                    line_start=line_start,
                    line_end=line_end,
                    content=content,
                    score=1.0,
                    symbol_name=row.get("name"),
                    chunk_type="function",
                )
            )
        return out

    def _read_snippet(self, file_path: str, line_start: int, line_end: int) -> str:
        if not file_path:
            return ""
        target = (self.workspace_root / file_path).resolve()
        try:
            target.relative_to(self.workspace_root)
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            return ""
        start = max(line_start - 1, 0)
        end = min(max(line_end, line_start), len(lines))
        return "\n".join(lines[start:end])


class NullSearchAgent(ISearchAgent):
    """No-op search agent used when Codebase-Memory MCP is unavailable -
    callers get empty results instead of a crash, not fabricated data."""

    def search(self, query: str, top_k: int = 5, boost_paths: list[str] | None = None) -> list[SearchResult]:
        return []

    def symbol_lookup(self, name: str) -> list[SearchResult]:
        return []

    def fts_search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return []
