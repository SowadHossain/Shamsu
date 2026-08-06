"""The structural code retrieval seam.

Retrieval order is fixed by plan section 18 and matters more than any single
backend's quality:

    exact path -> exact text -> symbol index -> reference graph -> call graph
    -> related tests -> dependency graph -> git history -> semantic fallback

Semantic search is last and is a fallback, never a replacement. A structural
question ("who calls this?") has a correct answer; an embedding similarity
score is not it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class LineRange(BaseModel):
    """An inclusive 1-indexed line span."""

    model_config = ConfigDict(frozen=True)

    start: int = Field(ge=1)
    end: int = Field(ge=1)


class SymbolRef(BaseModel):
    """A located, named code entity."""

    model_config = ConfigDict(frozen=True)

    qualified_name: str
    path: str = Field(description="Repository-relative POSIX path.")
    lines: LineRange
    kind: str = Field(description="function | class | method | constant | module | ...")
    signature: str | None = None


class SearchHit(BaseModel):
    """One retrieval result, always traceable to a real location.

    `provenance` records which retrieval stage produced this hit, so the
    `context_retrieval_precision` metric can be attributed per stage and a
    semantic hit is never mistaken for a structural fact.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    lines: LineRange | None = None
    excerpt: str
    score: float = Field(ge=0.0)
    provenance: str = Field(
        description="exact_path | text | symbol | reference | call_graph | git | semantic"
    )


class ImpactReport(BaseModel):
    """What a change to a symbol could reach.

    Used to scope a plan step and to bound repair: the repair phase may only
    touch failure-related files, and this is how "related" is decided.
    """

    model_config = ConfigDict(frozen=True)

    symbol: SymbolRef
    direct_callers: tuple[SymbolRef, ...] = ()
    transitive_modules: tuple[str, ...] = ()
    related_tests: tuple[str, ...] = ()
    truncated: bool = Field(
        default=False,
        description="Whether traversal hit its budget. A truncated report is not proof of safety.",
    )


@runtime_checkable
class CodeIndex(Protocol):
    """Deterministic structural retrieval over the repository."""

    def find_file(self, pattern: str) -> Sequence[str]:
        """Stage 1: exact and glob path matching."""
        ...

    def search_text(self, query: str, *, limit: int = 20) -> Sequence[SearchHit]:
        """Stage 2: exact text search."""
        ...

    def lookup_symbol(self, name: str) -> Sequence[SymbolRef]:
        """Stage 3: symbol index lookup."""
        ...

    def references(self, symbol: SymbolRef) -> Sequence[SearchHit]:
        """Stage 4: every reference to a symbol."""
        ...

    def callers(self, symbol: SymbolRef) -> Sequence[SymbolRef]:
        """Stage 5a: symbols that call this one."""
        ...

    def callees(self, symbol: SymbolRef) -> Sequence[SymbolRef]:
        """Stage 5b: symbols this one calls."""
        ...

    def related_tests(self, path: str) -> Sequence[str]:
        """Stage 6: tests covering a file."""
        ...

    def impact(self, symbol: SymbolRef) -> ImpactReport:
        """Stages 4-7 combined into a change-scoping report."""
        ...

    def is_ready(self) -> bool:
        """Whether the index is built and current.

        A stale index must report False rather than serving stale structure.
        v1's abstract service gated on a marker file that could disagree with
        the actual index, which let the agent silently degrade.
        """
        ...


@runtime_checkable
class SemanticIndex(Protocol):
    """Stage 9 fallback only.

    Runs after structural retrieval returns nothing. Must degrade to "no hits"
    on any failure rather than raising -- a broken embedding backend is not a
    reason to fail a task.
    """

    def search(self, query: str, *, limit: int = 10) -> Sequence[SearchHit]:
        """Return semantically similar locations, or empty on any failure."""
        ...


__all__ = [
    "CodeIndex",
    "ImpactReport",
    "LineRange",
    "SearchHit",
    "SemanticIndex",
    "SymbolRef",
]
