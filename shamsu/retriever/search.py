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

import sqlite3
from pathlib import Path

from shamsu.interfaces import ISearchAgent
from shamsu.types import SearchResult


class SearchAgent(ISearchAgent):
    """Real implementation. Dev A builds this out Day 2-3."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self._bm25_index = None          # lazy — built on first .search() call
        self._bm25_corpus_ids: list[int] = []

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
        return " OR ".join(terms)

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

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """
        Default entry point. FTS5 first (cheap, always available).
        BM25 in-memory layer is built lazily and only used as a
        supplement once the corpus exists — see harness §3.
        """
        return self.fts_search(query, top_k=top_k)


class SearchAgentStub(ISearchAgent):
    """
    Stub for Dev B (and anyone else) to build against before Day 3.
    Returns deterministic fake data so downstream code (ContextBuilder,
    workflows) can be written and unit-tested immediately.

    Swap `SearchAgentStub()` for `SearchAgent(db_path)` once Dev A's
    PR for feature/dev-a/search-engine merges. No other code changes
    needed if you only ever called methods on ISearchAgent.
    """

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
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
