"""Semantic last-resort retrieval over local embeddings (gap H1, second half).

FTS + the per-word rescue still cannot map "authentication" onto code that
only says `login`/`jwt` - that needs meaning, not matching. This adds it with
the smallest possible footprint:

* embeddings come from a local Ollama model (default `nomic-embed-text`,
  ~274MB) - no cloud, consistent with everything else in SHAMSU;
* the index is a JSON file under `.shamsu/` mapping path -> (mtime, vector),
  refreshed lazily per query, one embed call per changed file;
* it only ever runs as the LAST resort, after FTS and the per-word rescue both
  came up empty, so the common path pays nothing;
* any failure - Ollama down, model missing, bad response - degrades to "no
  hits" and is remembered per process, so a machine without the model pays one
  failed request total, not one per search. `SHAMSU_SEMANTIC_SEARCH=0`
  disables it outright.

File-level granularity on purpose: the question this answers is "WHICH file
handles auth", and the agent reads the file itself next. Chunk-level ranking
can come later if evals ever demand it.
"""
from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from pathlib import Path

import httpx

from shamsu.indexer.policy import DEFAULT_EXCLUDED_DIRS, SOURCE_SUFFIXES, walk_workspace_files
from shamsu.types import SearchResult

EMBED_MODEL = os.environ.get("SHAMSU_EMBED_MODEL", "nomic-embed-text").strip()
_ENABLED = os.environ.get("SHAMSU_SEMANTIC_SEARCH", "1").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
}

_INDEX_RELATIVE_PATH = Path(".shamsu") / "semantic_index.json"
_MAX_INDEXED_FILES = 400
_MAX_EMBED_CHARS = 6000          # nomic handles ~8k tokens; stay safely under
_SNIPPET_LINES = 30
_MIN_SCORE = 0.35                # cosine floor: below this it's noise, not a hit

# Kept in sync with plan_mode's grounding sets by test (importing them here
# would pull the agents layer into the retriever layer).
_SOURCE_SUFFIXES = SOURCE_SUFFIXES
_IGNORED_DIRS = DEFAULT_EXCLUDED_DIRS

# Embedder signature: texts -> one vector per text.
Embedder = Callable[[list[str]], list[list[float]]]

# Machines without the embed model should pay ONE failed request per process,
# not one per search.
_EMBEDDER_BROKEN = False


def _ollama_embed(texts: list[str]) -> list[list[float]]:
    from shamsu.llm.manager import OLLAMA_BASE_URL

    response = httpx.post(
        f"{OLLAMA_BASE_URL}/api/embed",
        json={"model": EMBED_MODEL, "input": texts},
        timeout=httpx.Timeout(connect=5, read=60, write=10, pool=5),
    )
    response.raise_for_status()
    payload = response.json()
    vectors = payload.get("embeddings") or []
    if len(vectors) != len(texts):
        raise ValueError(f"embed returned {len(vectors)} vectors for {len(texts)} inputs")
    return vectors


class SemanticIndex:
    def __init__(self, workspace_root: Path, embed: Embedder | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self._embed = embed or _ollama_embed
        self._injected = embed is not None

    # -- public -----------------------------------------------------------------

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        """Best-effort semantic hits; [] on any failure or when disabled."""
        global _EMBEDDER_BROKEN
        if not _ENABLED or not query.strip():
            return []
        if _EMBEDDER_BROKEN and not self._injected:
            return []
        try:
            index = self._refresh_index()
            if not index:
                return []
            query_vector = self._embed([query])[0]
        except Exception:
            if not self._injected:
                _EMBEDDER_BROKEN = True
            return []
        scored: list[tuple[float, str]] = []
        for path, entry in index.items():
            score = _cosine(query_vector, entry["vector"])
            if score >= _MIN_SCORE:
                scored.append((score, path))
        scored.sort(reverse=True)
        return [self._to_result(path, score) for score, path in scored[:top_k]]

    # -- index maintenance --------------------------------------------------------

    def _refresh_index(self) -> dict[str, dict]:
        """Load the stored index and re-embed only files whose mtime moved."""
        stored = self._load()
        current = self._source_files()
        fresh: dict[str, dict] = {}
        stale: list[str] = []
        for path, mtime in current.items():
            entry = stored.get(path)
            if entry and entry.get("mtime") == mtime and entry.get("vector"):
                fresh[path] = entry
            else:
                stale.append(path)
        if stale:
            texts = []
            for path in stale:
                try:
                    texts.append((self.workspace_root / path).read_text(encoding="utf-8", errors="replace")[:_MAX_EMBED_CHARS])
                except OSError:
                    texts.append("")
            vectors = self._embed(texts)
            for path, vector in zip(stale, vectors):
                fresh[path] = {"mtime": current[path], "vector": vector}
        if stale or len(fresh) != len(stored):
            self._save(fresh)
        return fresh

    def _source_files(self) -> dict[str, float]:
        found: dict[str, float] = {}
        try:
            files = walk_workspace_files(
                self.workspace_root,
                limit=_MAX_INDEXED_FILES,
                suffixes=_SOURCE_SUFFIXES,
                indexable_only=True,
            )
            for path in files:
                relative = path.relative_to(self.workspace_root)
                try:
                    found[relative.as_posix()] = path.stat().st_mtime
                except OSError:
                    continue
        except OSError:
            return {}
        return found

    def _to_result(self, path: str, score: float) -> SearchResult:
        content = ""
        line_end = 1
        try:
            lines = (self.workspace_root / path).read_text(encoding="utf-8", errors="replace").splitlines()
            line_end = max(1, min(len(lines), _SNIPPET_LINES))
            content = "\n".join(lines[:_SNIPPET_LINES])
        except OSError:
            pass
        return SearchResult(
            file_path=path,
            language=Path(path).suffix.lstrip("."),
            line_start=1,
            line_end=line_end,
            content=content,
            score=round(float(score), 4),
            symbol_name=None,
            chunk_type="semantic",
        )

    # -- persistence ----------------------------------------------------------------

    def _index_path(self) -> Path:
        return self.workspace_root / _INDEX_RELATIVE_PATH

    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self._index_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, index: dict[str, dict]) -> None:
        try:
            path = self._index_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(index), encoding="utf-8")
        except OSError:
            pass


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
