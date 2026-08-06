"""Structural code retrieval: symbols, references, callers, callees, impact.

Retrieval order is exact match, then text search, then symbol index, then the
reference and call graphs, then related tests -- and only then semantic search.
Semantic search is a fallback, never a replacement: "who calls this?" has a
correct answer, and an embedding similarity score is not it.

Built on stdlib `ast` rather than tree-sitter. For Python that is strictly more
accurate -- it is the language's own parser -- and it costs no dependency. The
`CodeIndex` protocol is the seam a tree-sitter backend would arrive through
when other languages need one.

Milestone 8. See plan section 18.
"""

from shamsu.code_intelligence.index import (
    EXCERPT_CHARS,
    MAX_IMPACT_DEPTH,
    MAX_IMPACT_MODULES,
    PythonCodeIndex,
)
from shamsu.code_intelligence.retrieval import (
    STAGES,
    Retrieved,
    StructuredRetriever,
    related_files_for,
)

__all__ = [
    "EXCERPT_CHARS",
    "MAX_IMPACT_DEPTH",
    "MAX_IMPACT_MODULES",
    "STAGES",
    "PythonCodeIndex",
    "Retrieved",
    "StructuredRetriever",
    "related_files_for",
]
