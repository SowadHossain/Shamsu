"""
shamsu/retriever/search.py — Dev A owns this file.

Implements ISearchAgent using SQLite FTS5 (zero extra RAM, built into
stdlib sqlite3) as the primary engine, with rank_bm25 as an optional
in-memory layer for the 500 most-recently-touched snippets (see
ENGINEERING_HARNESS.md §3 — FTS5 is the recommended default; BM25 is
a lazy-built supplement, never built at startup).

Dev B: import SearchAgentStub from this file until Dev A's real
implementation lands (target: Day 3). Swap the import, nothing else
in your code should need to change — that's the point of building
against ISearchAgent.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from rank_bm25 import BM25Okapi

from shamsu.interfaces import ISearchAgent
from shamsu.types import SearchResult

# How many of the most-recently-touched snippets the lazy BM25 layer covers.
RECENT_SNIPPET_LIMIT = 500

# search() over-fetches from FTS5 before boosting/re-ranking, since a boost
# can promote a result that plain FTS ranked outside the first top_k.
OVER_FETCH_MULTIPLIER = 4
MAX_OVER_FETCH = 40

# Boosts are additive on top of FTS5's bm25 score (already flipped so higher
# is better) — small, conservative nudges, not a replacement ranker.
PATH_MATCH_BOOST = 0.3
SYMBOL_MATCH_BOOST = 0.5
RECENCY_BOOST = 0.2
ERROR_TRACE_BOOST = 1.0


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


class SearchAgent(ISearchAgent):
    """Real implementation. Dev A builds this out Day 2-3."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._bm25_index = None          # lazy — built on first .search() call
        self._bm25_built = False
        self._bm25_keys: list[tuple[str, int, int]] = []

    @staticmethod
    def _build_fts_query(query: str) -> str:
        """
        FTS5's default MATCH syntax treats bare multi-word queries as an
        implicit AND — 'login authentication' only matches snippets containing
        BOTH words. That kills recall for natural-language queries from the
        router/PRD ("user login authentication flow"). Join terms with OR
        instead, and strip characters FTS5's query syntax treats as special
        (quotes, parens, etc.) so a stray symbol doesn't throw a syntax error.
        """
        cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in query)
        terms = [t for t in cleaned.split() if t]
        if not terms:
            return '""'
        return " OR ".join(f'"{term}"' for term in terms)

    def fts_search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """
        SQLite FTS5 search. bm25() returns NEGATIVE scores in FTS5 —
        smaller (more negative) = more relevant. We flip the sign so
        callers always see "higher score = better", matching SearchResult
        convention used by ranker.py.
        """
        fts_query = self._build_fts_query(query)
        rows = self.conn.execute(
            """
            SELECT s.id, s.file_id, s.content, s.line_start, s.line_end,
                   f.path, f.language, bm25(snippets_fts) AS rank
            FROM snippets_fts
            JOIN snippets s ON s.id = snippets_fts.rowid
            JOIN files f ON f.id = s.file_id
            WHERE snippets_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, top_k),
        ).fetchall()

        return [
            SearchResult(
                file_path=row["path"],
                language=row["language"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                content=row["content"],
                score=-row["rank"],   # flip sign — higher is better downstream
            )
            for row in rows
        ]

    def symbol_lookup(self, name: str) -> list[SearchResult]:
        rows = self.conn.execute(
            """
            SELECT sym.name, sym.line_start, sym.line_end, sym.signature,
                   f.path, f.language
            FROM symbols sym
            JOIN files f ON f.id = sym.file_id
            WHERE sym.name LIKE ?
            ORDER BY length(sym.name) ASC
            LIMIT 10
            """,
            (f"%{name}%",),
        ).fetchall()

        return [
            SearchResult(
                file_path=row["path"],
                language=row["language"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                content=row["signature"] or "",
                score=1.0,
                symbol_name=row["name"],
                chunk_type="function",
            )
            for row in rows
        ]

    def search(
        self,
        query: str,
        top_k: int = 5,
        boost_paths: list[str] | None = None,
    ) -> list[SearchResult]:
        """
        Default entry point. FTS5 first (cheap, always available, gives
        broad recall across the whole corpus), then re-ranked with small
        additive boosts: symbol-name match, file-path match, recency (via
        the lazy in-memory BM25 layer over recently-touched snippets), and
        optionally exact traceback/error-location matches via boost_paths
        (e.g. from BugFixWorkflow). Boosts never replace FTS5 — they only
        reorder results FTS5 already considered relevant.
        """
        over_fetch = min(max(top_k * OVER_FETCH_MULTIPLIER, top_k), MAX_OVER_FETCH)
        results = self.fts_search(query, top_k=over_fetch)
        if not results:
            return results

        query_terms = {term for term in _tokenize(query) if term}
        boost_path_terms = [p.lower().replace("\\", "/") for p in (boost_paths or []) if p]
        symbol_paths = self._files_with_matching_symbol(query_terms)
        bm25_scores = self._recent_snippet_bm25_scores(query)

        boosted: list[SearchResult] = []
        for result in results:
            score = result.score
            path_lower = result.file_path.lower()
            if any(term in path_lower for term in query_terms):
                score += PATH_MATCH_BOOST
            if result.file_path in symbol_paths:
                score += SYMBOL_MATCH_BOOST
            if any(path_lower.endswith(term) or term.endswith(path_lower) for term in boost_path_terms):
                score += ERROR_TRACE_BOOST
            key = (result.file_path, result.line_start, result.line_end)
            if key in bm25_scores:
                score += RECENCY_BOOST
            boosted.append(
                SearchResult(
                    file_path=result.file_path, language=result.language,
                    line_start=result.line_start, line_end=result.line_end,
                    content=result.content, score=score,
                    symbol_name=result.symbol_name, chunk_type=result.chunk_type,
                )
            )
        boosted.sort(key=lambda r: r.score, reverse=True)
        return boosted[:top_k]

    def _files_with_matching_symbol(self, terms: set[str]) -> set[str]:
        if not terms:
            return set()
        placeholders = " OR ".join(["sym.name LIKE ?"] * len(terms))
        params = [f"%{term}%" for term in terms]
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT f.path
            FROM symbols sym
            JOIN files f ON f.id = sym.file_id
            WHERE {placeholders}
            """,
            params,
        ).fetchall()
        return {row["path"] for row in rows}

    def _ensure_bm25_index(self) -> None:
        if self._bm25_built:
            return
        self._bm25_built = True
        rows = self.conn.execute(
            """
            SELECT f.path, s.line_start, s.line_end, s.content
            FROM snippets s
            JOIN files f ON f.id = s.file_id
            ORDER BY f.last_modified DESC
            LIMIT ?
            """,
            (RECENT_SNIPPET_LIMIT,),
        ).fetchall()
        if not rows:
            return
        self._bm25_keys = [(row["path"], row["line_start"], row["line_end"]) for row in rows]
        self._bm25_index = BM25Okapi([_tokenize(row["content"]) for row in rows])

    def _recent_snippet_bm25_scores(self, query: str) -> dict[tuple[str, int, int], float]:
        self._ensure_bm25_index()
        if self._bm25_index is None:
            return {}
        scores = self._bm25_index.get_scores(_tokenize(query))
        return {key: score for key, score in zip(self._bm25_keys, scores) if score > 0}


class SearchAgentStub(ISearchAgent):
    """
    Stub for Dev B (and anyone else) to build against before Day 3.
    Returns deterministic fake data so downstream code (ContextBuilder,
    workflows) can be written and unit-tested immediately.

    Swap `SearchAgentStub()` for `SearchAgent(db_path)` once Dev A's
    PR for feature/dev-a/search-engine merges. No other code changes
    needed if you only ever called methods on ISearchAgent.
    """

    def search(
        self,
        query: str,
        top_k: int = 5,
        boost_paths: list[str] | None = None,
    ) -> list[SearchResult]:
        return [
            SearchResult(
                file_path="stub/example.py",
                language="python",
                line_start=1,
                line_end=10,
                content=f"# stub result for query: {query!r}\ndef example():\n    pass",
                score=0.5,
            )
        ][:top_k]

    def symbol_lookup(self, name: str) -> list[SearchResult]:
        return [
            SearchResult(
                file_path="stub/example.py",
                language="python",
                line_start=3,
                line_end=3,
                content=f"def {name}(): ...",
                score=1.0,
                symbol_name=name,
                chunk_type="function",
            )
        ]

    def fts_search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return self.search(query, top_k=top_k)
