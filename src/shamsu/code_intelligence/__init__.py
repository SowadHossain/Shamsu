"""Structural code retrieval: symbols, references, callers, callees, impact.

Retrieval order is exact match, then text search, then symbol index, then the
reference and call graphs, then git history -- and only then semantic search.
Semantic search is a fallback, never a replacement.

Milestone 8. See plan section 18.
"""
