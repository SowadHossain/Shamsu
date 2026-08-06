"""The ordered retrieval pipeline (plan §18).

The order is the product. Any one backend can be swapped; running them in the
wrong order cannot be fixed by improving any of them.

    1. exact file/path match
    2. exact text search
    3. symbol index
    4. reference graph
    5. call graph
    6. related tests
    7. dependency graph
    8. git history
    9. semantic search — fallback only

Stages run in order and the first non-empty stage wins. That is not an
optimisation: a query answerable by an exact path match has a *correct*
answer, and letting a later, fuzzier stage contribute to it can only dilute
it. Semantic search runs last, when everything with a defensible answer has
declined to answer.

Every hit records which stage produced it, so retrieval precision can be
attributed per stage — and so a semantic guess is never mistaken later for a
structural fact.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from shamsu.code_intelligence.index import EXCERPT_CHARS, PythonCodeIndex
from shamsu.interfaces.code_intelligence import (
    LineRange,
    SearchHit,
    SemanticIndex,
    SymbolRef,
)

#: Stage names in order, for reporting and for tests that assert the order has
#: not quietly changed.
STAGES: tuple[str, ...] = (
    "exact_path",
    "text",
    "symbol",
    "reference",
    "call_graph",
    "related_tests",
    "semantic",
)


@dataclass(frozen=True)
class Retrieved:
    """What a retrieval produced, and how.

    `stage` names the stage that answered. `attempted` lists every stage that
    ran and declined, which is what makes "retrieval found nothing" a
    diagnosable outcome rather than a shrug.
    """

    hits: tuple[SearchHit, ...] = ()
    stage: str = ""
    attempted: tuple[str, ...] = ()
    degraded: str = ""

    @property
    def found(self) -> bool:
        return bool(self.hits)

    def render(self, limit: int = 10) -> str:
        """The retrieval as it enters a frame."""
        if not self.hits:
            attempted = ", ".join(self.attempted) or "none"
            return f"No code found. Stages tried: {attempted}."

        lines = [f"Found via {self.stage}:"]
        for hit in self.hits[:limit]:
            where = f"{hit.path}:{hit.lines.start}" if hit.lines else hit.path
            lines.append(f"  {where}  {hit.excerpt}")
        if len(self.hits) > limit:
            lines.append(f"  … and {len(self.hits) - limit} more")
        if self.degraded:
            lines.append(f"[{self.degraded}]")
        return "\n".join(lines)


@dataclass
class StructuredRetriever:
    """Runs the stages in order and returns the first that answers."""

    index: PythonCodeIndex
    semantic: SemanticIndex | None = None
    limit: int = 20
    _attempted: list[str] = field(default_factory=list, repr=False)

    def retrieve(self, query: str) -> Retrieved:
        """Answer a retrieval query with the narrowest stage that can."""
        attempted: list[str] = []
        degraded = ""

        if not self.index.is_ready():
            # Answering from a stale index is the failure v1 shipped: the
            # marker said ready, the index disagreed, and nobody could tell.
            degraded = "index is stale; rebuild before trusting structural answers"

        for stage in self._order(query):
            if stage == "semantic":
                continue
            attempted.append(stage)
            hits = self._run(stage, query)
            if hits:
                return Retrieved(
                    hits=tuple(hits[: self.limit]),
                    stage=stage,
                    attempted=tuple(attempted),
                    degraded=degraded,
                )

        attempted.append("semantic")
        hits = self._semantic(query)
        if hits:
            return Retrieved(
                hits=tuple(hits[: self.limit]),
                stage="semantic",
                attempted=tuple(attempted),
                degraded=degraded,
            )

        return Retrieved(attempted=tuple(attempted), degraded=degraded)

    # -- stages ------------------------------------------------------------

    def _order(self, query: str) -> tuple[str, ...]:
        """Stage order for this query.

        Plan §18 puts exact text search (stage 2) ahead of the symbol index
        (stage 3), and for a literal — an error message, a config key, a
        magic string — that is right: the text is the answer.

        For a bare identifier it is not. `check_completion` as a text search
        matches the definition *and* every import, call site, and mention in
        the tests, and first-non-empty-wins then hands back the noisy set. The
        definition is the correct answer to "where is `check_completion`", and
        the symbol index has it exactly.

        So an identifier the symbol index knows is routed to `symbol` first.
        The stage order is otherwise untouched; what changes is which queries
        each stage claims. Both behaviours are asserted in
        `tests/unit/test_code_index.py`.
        """
        if _is_identifier(query) and self.index.lookup_symbol(query):
            return ("exact_path", "symbol", "text", "reference", "call_graph", "related_tests")
        return STAGES

    def _run(self, stage: str, query: str) -> list[SearchHit]:
        if stage == "exact_path":
            return [
                SearchHit(path=path, excerpt=path, score=1.0, provenance="exact_path")
                for path in self.index.find_file(query)
            ]

        if stage == "text":
            return list(self.index.search_text(query, limit=self.limit))

        if stage == "symbol":
            return [_symbol_hit(reference) for reference in self.index.lookup_symbol(query)]

        if stage in ("reference", "call_graph"):
            symbols = self.index.lookup_symbol(query)
            if not symbols:
                return []
            if stage == "reference":
                return list(self.index.references(symbols[0]))
            return [
                _symbol_hit(reference, provenance="call_graph")
                for reference in self.index.callers(symbols[0])
            ]

        if stage == "related_tests":
            paths = self.index.find_file(query)
            return [
                SearchHit(path=test, excerpt=test, score=0.8, provenance="related_tests")
                for path in paths
                for test in self.index.related_tests(path)
            ]

        return []

    def _semantic(self, query: str) -> list[SearchHit]:
        """Stage 9, and only ever stage 9.

        Any failure degrades to "no hits". A broken embedding backend is not a
        reason to fail a task, and it is certainly not a reason to raise out of
        a retrieval call three layers below the runtime.
        """
        if self.semantic is None:
            return []
        try:
            return list(self.semantic.search(query, limit=self.limit))
        except Exception:  # noqa: BLE001 - a fallback that can fail is not a fallback
            return []


def _is_identifier(query: str) -> bool:
    """Whether the query looks like a name rather than a literal."""
    return bool(query) and all(part.isidentifier() for part in query.split("."))


def _symbol_hit(reference: SymbolRef, provenance: str = "symbol") -> SearchHit:
    return SearchHit(
        path=reference.path,
        lines=LineRange(start=reference.lines.start, end=reference.lines.end),
        excerpt=(reference.signature or reference.qualified_name)[:EXCERPT_CHARS],
        score=1.0,
        provenance=provenance,
    )


def related_files_for(index: PythonCodeIndex, paths: Sequence[str]) -> tuple[str, ...]:
    """Files structurally related to a change, for scoping a repair.

    This is what plan §20.5's "failure-related files" should eventually mean.
    `RepairScope` currently derives its allowlist from traceback frames and the
    step's declared files, because those need no index; this widens it to what
    the call graph actually says once an index is available.

    Deliberately *not* wired into `RepairController` yet: widening a write
    scope is a safety change, and it should land with retrieval evaluations
    behind it rather than on the strength of it being available.
    """
    related: list[str] = []
    for path in paths:
        for candidate in (path, *index.related_tests(path)):
            if candidate not in related:
                related.append(candidate)

    for path in tuple(related):
        for defined in index.symbols_in(path):
            for caller in index.callers(defined):
                if caller.path not in related:
                    related.append(caller.path)

    return tuple(related)


__all__ = [
    "STAGES",
    "Retrieved",
    "StructuredRetriever",
    "related_files_for",
]
