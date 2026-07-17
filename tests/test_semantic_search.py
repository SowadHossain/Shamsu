"""Gap H1 (semantic half): last-resort retrieval over local embeddings.

Keyword passes cannot map "authentication" onto code that only says
`login`/`jwt`. The semantic index can - and it only ever runs after FTS and
the per-word rescue BOTH found nothing, degrading to no-hits on any failure.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.retriever.search import SearchAgent
from shamsu.retriever.semantic import SemanticIndex, _cosine


def _fake_embedder(vocabulary: dict[str, list[float]]):
    """Deterministic 'embeddings': a text's vector is the sum of the vectors of
    the vocabulary words it contains."""

    def embed(texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            lowered = text.lower()
            vector = [0.0, 0.0, 0.0]
            for word, word_vector in vocabulary.items():
                if word in lowered:
                    vector = [a + b for a, b in zip(vector, word_vector)]
            out.append(vector)
        return out

    return embed


# "authentication" and "login/jwt" share a direction; "render" is orthogonal.
_VOCAB = {
    "authentication": [1.0, 0.2, 0.0],
    "login": [0.9, 0.1, 0.0],
    "jwt": [0.8, 0.0, 0.1],
    "render": [0.0, 0.0, 1.0],
    "canvas": [0.0, 0.1, 0.9],
}


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "auth.py").write_text("def login(user):\n    return jwt_token(user)\n", encoding="utf-8")
    (tmp_path / "draw.py").write_text("def render(canvas):\n    canvas.paint()\n", encoding="utf-8")
    return tmp_path


def test_semantic_index_maps_meaning_not_keywords(tmp_path: Path):
    index = SemanticIndex(_workspace(tmp_path), embed=_fake_embedder(_VOCAB))

    hits = index.search("authentication", top_k=2)

    assert hits, "the whole point: 'authentication' appears in NO file"
    assert hits[0].file_path == "auth.py"
    assert all(hit.file_path != "draw.py" or hit.score < hits[0].score for hit in hits)


def test_index_is_cached_and_only_reembeds_changed_files(tmp_path: Path):
    calls: list[int] = []
    base = _fake_embedder(_VOCAB)

    def counting(texts):
        calls.append(len(texts))
        return base(texts)

    workspace = _workspace(tmp_path)
    index = SemanticIndex(workspace, embed=counting)
    index.search("authentication")
    first_round = sum(calls)

    index.search("authentication")          # nothing changed: only the query embeds
    assert sum(calls) == first_round + 1

    (workspace / "auth.py").write_text("def login(u):\n    return jwt(u)  # changed\n", encoding="utf-8")
    import os
    os.utime(workspace / "auth.py", (0, 9_999_999_999))
    index.search("authentication")          # one stale file + the query
    assert sum(calls) == first_round + 3


def test_a_broken_embedder_degrades_to_no_hits(tmp_path: Path):
    def broken(texts):
        raise ConnectionError("ollama down")

    index = SemanticIndex(_workspace(tmp_path), embed=broken)
    assert index.search("authentication") == []


def test_search_agent_uses_semantic_only_as_last_resort(tmp_path: Path):
    class _Adapter:
        def __init__(self):
            self.queries = []

        def search_code(self, workspace, query, limit=5):
            self.queries.append(query)
            return {"ok": True, "results": []}

    class _FakeSemantic:
        def __init__(self):
            self.queries = []

        def search(self, query, top_k=5):
            self.queries.append(query)
            return []

    fake_semantic = _FakeSemantic()
    agent = SearchAgent(_workspace(tmp_path), adapter=_Adapter(), semantic_index=fake_semantic)

    agent.search("where is authentication handled", top_k=3)

    assert fake_semantic.queries == ["where is authentication handled"]


def test_search_agent_skips_semantic_when_keywords_hit(tmp_path: Path):
    class _Adapter:
        def search_code(self, workspace, query, limit=5):
            return {"ok": True, "results": [
                {"node": "login", "file": "auth.py", "start_line": 1, "end_line": 2}
            ]}

    class _MustNotRun:
        def search(self, query, top_k=5):
            raise AssertionError("semantic must not run when FTS hits")

    agent = SearchAgent(_workspace(tmp_path), adapter=_Adapter(), semantic_index=_MustNotRun())
    hits = agent.search("login", top_k=3)
    assert hits


def test_cosine_basics():
    assert _cosine([1, 0], [1, 0]) == 1.0
    assert _cosine([1, 0], [0, 1]) == 0.0
    assert _cosine([], [1.0]) == 0.0
    assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_grounding_sets_stay_in_sync_with_plan_mode():
    """semantic.py duplicates plan_mode's suffix/dir sets rather than importing
    the agents layer into the retriever layer. This is the sync guard."""
    from shamsu.agents import plan_mode
    from shamsu.retriever import semantic

    assert semantic._SOURCE_SUFFIXES == plan_mode._SOURCE_SUFFIXES
    assert semantic._IGNORED_DIRS == plan_mode._IGNORED_DIRS
