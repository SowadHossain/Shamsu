"""Hybrid code search: exact matching fused with meaning-based ranking.

Method adapted from smallcode `src/tools/hybrid_search.js` and
`src/rag/index_store.js` (MIT, (c) 2026 Doorman11991 - see
reference/smallcode/LICENSE). The algorithm and its constants are theirs; the
Python is ours.

Why this and not SHAMSU's existing retrievers: `retriever/search.py` needs the
codebase-memory MCP server running, and `retriever/semantic.py` needs a 274MB
`nomic-embed-text` model pulled into Ollama. Both degrade to "no hits" when the
dependency is absent, which on a fresh machine is always. This has neither -
BM25 plus a hashed bag-of-words vector is pure stdlib, needs no model and no
service, and runs on CPU instantly. That is the "no embedding model, no vector
DB" constraint SHAMSU already committed to, met rather than worked around.

What it replaces: `grep_files` matched with `query in line` - not a regex, a
literal substring. So `def handle_.*login` found nothing, and "the function
that validates tokens" found nothing, and in both cases the model was told
"Found 0 match(es)" as though it had asked a fair question and got a true no.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# BM25 constants, from smallcode's index_store.js.
BM25_K1 = 1.4
BM25_B = 0.72

# Hashed-vector width. The whole "embedding" is a feature-hashing trick: tokens
# are hashed into a fixed number of buckets and the sparse result is
# normalised. It captures term co-occurrence, not meaning in the transformer
# sense - but it does put `login`, `signIn` and `authenticate_user` nearer each
# other than a substring match ever will, for zero bytes of model weights.
VECTOR_DIMS = 1024

# Chunks are centred on a definition, so a hit points at a function rather than
# at a line.
MAX_CHUNK_LINES = 80
MAX_INDEXED_FILES = 1500
MAX_FILE_BYTES = 512 * 1024

# Hard ceiling on the corpus. Files alone is not a bound - one 4,000-line
# module is 50 chunks - and the cold build is the only part a user waits on:
# measured on this repo, 1,500 files became 18,084 chunks and ~18s. Warm calls
# are ~0.9s, so this only ever caps the FIRST search in a workspace.
MAX_INDEXED_CHUNKS = 12_000

# When the cap bites, it must drop prose before it drops code. Capping in walk
# order silently truncated `shamsu/` on this repo because `agent context/`
# sorts first, and the top hit for a code question became the word "of" in a
# markdown file. A cap that changes the ANSWER is worse than a slow search.
_CODE_SUFFIXES = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".kt", ".swift", ".scala", ".sh", ".sql",
    ".vue", ".svelte",
})


def _code_first(files: list[str]) -> list[str]:
    """Source before configuration before prose, each group in walk order."""
    def rank(relative: str) -> int:
        suffix = ("." + relative.rsplit(".", 1)[-1].lower()) if "." in relative else ""
        if suffix in _CODE_SUFFIXES:
            return 0
        if suffix in {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg"}:
            return 1
        return 2

    return sorted(files, key=lambda relative: (rank(relative), relative))

# Fusion weights, from smallcode. An exact match outweighs a semantic one by a
# lot, which is what keeps this usable as a grep replacement rather than as a
# fuzzy suggestion box.
VECTOR_WEIGHT = 0.6
EXACT_BOOST = 2.0

_STOP_WORDS = frozenset({
    "the", "and", "for", "that", "this", "with", "from", "into", "your", "you",
    "are", "was", "were", "will", "would", "could", "should", "have", "has",
    "had", "not", "but", "what", "when", "where", "why", "how", "can", "need",
    "make", "create", "add", "fix", "code", "file", "class", "function",
})

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")
_CAMEL_RE = re.compile(r"([a-z0-9])([A-Z])")
_SPLIT_RE = re.compile(r"[_\-]+")

# Definition boundaries across the languages SHAMSU actually meets. Regex and
# not a parser on purpose: tree-sitter is available but only for the languages
# the indexer knows, and a search that silently skips .vue or .svelte is worse
# than one that splits them a little wrong.
_SYMBOL_PATTERNS = [
    re.compile(r"\b(?:function|func|fn|def|sub)\s+([A-Za-z_][\w]*)"),
    re.compile(r"\b(?:class|struct|interface|enum|trait|impl|type)\s+([A-Za-z_][\w]*)"),
    re.compile(r"(?:^|\s)(?:const|let|var)\s+([A-Za-z_][\w]*)\s*=\s*(?:async\s*)?(?:function|\([^)]*\)\s*=>)"),
    re.compile(r"(?:public|private|protected|static|async)\s+([A-Za-z_][\w]*)\s*\("),
]


def split_identifier(token: str) -> list[str]:
    """``handleUserLogin`` -> ``['handle', 'user', 'login']``.

    The single most valuable line in this file. Without it, a query for "login"
    cannot reach a symbol called `handleUserLogin`, and that is most of why
    plain substring search feels stupid on real code.
    """
    spaced = _CAMEL_RE.sub(r"\1 \2", str(token or ""))
    spaced = _SPLIT_RE.sub(" ", spaced).lower()
    return [part for part in spaced.split() if part]


def stem(word: str) -> str:
    """Fold the endings that separate a question from its answer.

    An addition to smallcode's tokenizer, not a copy of it - and the reason is
    a case their version misses: a user asks for "the function that validates
    tokens", the code says `validateAuthToken`, and with no stemming
    `validates` and `validate` are simply different words, so BM25 scores zero
    and the hashed vectors land in unrelated buckets. The whole point of the
    hybrid is to survive that gap.

    Deliberately crude - no Porter stemmer, no dependency. Plurals and the two
    commonest verb endings, guarded so "class" does not become "clas" and
    "index" is left alone.
    """
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("sses", "shes", "ches", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed") and not word.endswith("eed"):
        return word[:-2]
    return word


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for raw in _TOKEN_RE.findall(str(text or "")):
        parts = [stem(part) for part in split_identifier(raw)]
        out.extend(parts)
        if len(parts) > 1:
            # Keep the joined form as well, so the exact identifier still
            # outscores any single word inside it.
            out.append("_".join(parts))
    return [token for token in out if len(token) >= 2 and token not in _STOP_WORDS]


def hash_token(token: str, dims: int = VECTOR_DIMS) -> int:
    """FNV-1a masked to 32 bits - the hash smallcode uses."""
    value = 2166136261
    for char in token:
        value ^= ord(char)
        value = (value * 16777619) & 0xFFFFFFFF
    return value % dims


def embed_tokens(tokens: list[str], dims: int = VECTOR_DIMS) -> dict[int, float]:
    """A normalised sparse hashed bag of words. No model, no service."""
    vector: dict[int, float] = {}
    for token in tokens:
        key = hash_token(token, dims)
        vector[key] = vector.get(key, 0.0) + 1.0
    norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
    return {key: value / norm for key, value in vector.items()}


def embed(text: str, dims: int = VECTOR_DIMS) -> dict[int, float]:
    return embed_tokens(tokenize(text), dims)


def cosine(left: dict[int, float], right: dict[int, float]) -> float:
    if not left or not right:
        return 0.0
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(key, 0.0) for key, value in left.items())


@dataclass
class Chunk:
    path: str
    start_line: int
    end_line: int
    symbol: str
    code: str
    term_freq: dict[str, int] = field(default_factory=dict)
    doc_length: int = 0
    embedding: dict[int, float] = field(default_factory=dict)


def detect_symbol(line: str) -> str:
    for pattern in _SYMBOL_PATTERNS:
        match = pattern.search(line)
        if match and match.group(1):
            return match.group(1)
    return ""


def chunk_file(path: str, content: str) -> list[Chunk]:
    """Split a file at definition boundaries, capped so that a file with no
    detectable symbols still breaks into something rankable."""
    lines = content.splitlines()
    chunks: list[Chunk] = []
    current: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal current
        if current and current["lines"]:
            body = "\n".join(current["lines"])
            if body.strip():
                chunks.append(
                    Chunk(
                        path=path,
                        start_line=current["start"],
                        end_line=current["start"] + len(current["lines"]) - 1,
                        symbol=current["symbol"],
                        code=body,
                    )
                )
        current = None

    for index, line in enumerate(lines):
        symbol = detect_symbol(line)
        if symbol or current is None:
            if current is not None and (len(current["lines"]) > 1 if symbol else True):
                flush()
            if current is None or symbol:
                current = {"start": index + 1, "symbol": symbol, "lines": []}
        current["lines"].append(line)
        if len(current["lines"]) >= MAX_CHUNK_LINES:
            flush()
            current = {"start": index + 2, "symbol": "", "lines": []}
    flush()
    return chunks


def bm25_score(query_terms: list[str], chunk: Chunk, stats: dict[str, Any]) -> float:
    if not query_terms or not chunk.term_freq:
        return 0.0
    length = chunk.doc_length or 1
    average = stats["avg_doc_length"] or 1
    total = stats["total_docs"]
    score = 0.0
    for term in query_terms:
        frequency = chunk.term_freq.get(term, 0)
        if not frequency:
            continue
        document_frequency = stats["df"].get(term, 0)
        idf = math.log(1 + (total - document_frequency + 0.5) / (document_frequency + 0.5))
        score += idf * (
            (frequency * (BM25_K1 + 1))
            / (frequency + BM25_K1 * (1 - BM25_B + BM25_B * (length / average)))
        )
    return score


def compile_pattern(query: str, mode: str) -> re.Pattern[str] | None:
    """The query as a regex, falling back to a literal when it will not compile.

    A model writing ``def handle_(`` produces an invalid regex. Refusing it
    costs a round and teaches nothing; treating it literally answers the
    question it was almost certainly asking.
    """
    if mode == "semantic":
        return None
    if mode == "keyword":
        return re.compile(re.escape(query), re.IGNORECASE)
    try:
        return re.compile(query, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(query), re.IGNORECASE)


def _build_index_uncached(root: Path, files: list[str]) -> list[Chunk]:
    chunks: list[Chunk] = []
    for relative in _code_first(files)[:MAX_INDEXED_FILES]:
        full = Path(root) / relative
        try:
            if full.stat().st_size > MAX_FILE_BYTES:
                continue
            content = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "\x00" in content:
            continue
        for chunk in chunk_file(relative, content):
            searchable = "\n".join(
                part for part in (chunk.path, chunk.symbol, chunk.code) if part
            )
            tokens = tokenize(searchable)
            frequency: dict[str, int] = {}
            for token in tokens:
                frequency[token] = frequency.get(token, 0) + 1
            chunk.term_freq = frequency
            chunk.doc_length = len(tokens)
            # `embed(text)` would tokenise this a second time - measured at
            # 1.65s against 0.90s for the tokenise alone, i.e. most of the
            # index build was doing the same work twice.
            chunk.embedding = embed_tokens(tokens)
            chunks.append(chunk)
            if len(chunks) >= MAX_INDEXED_CHUNKS:
                # Partial index. The caller must SAY so - a cap that silently
                # changes which files are searchable turns "no results" into a
                # lie, and the model has no way to tell the difference.
                _TRUNCATED.add(str(root))
                return chunks
    return chunks


# One index per workspace, rebuilt only when a file actually changes. Without
# this every search re-read and re-tokenised the whole project: measured on
# this repo, 1,906 files and 18,084 chunks at ~8s a call. A model that searches
# three times in a turn cannot pay that.
_INDEX_CACHE: dict[str, tuple[tuple[Any, ...], list[Chunk]]] = {}

# Workspaces whose index hit the chunk cap.
_TRUNCATED: set[str] = set()


def index_was_truncated(root: Path) -> bool:
    return str(root) in _TRUNCATED


def _signature(root: Path, files: list[str]) -> tuple[Any, ...]:
    """Cheap fingerprint of the corpus: names plus modification times."""
    stamps = []
    for relative in files[:MAX_INDEXED_FILES]:
        try:
            stamps.append((relative, (root / relative).stat().st_mtime_ns))
        except OSError:
            stamps.append((relative, 0))
    return tuple(stamps)


def build_index(root: Path, files: list[str]) -> list[Chunk]:
    """The chunk index for *root*, reusing the last one when nothing changed."""
    key = str(root)
    signature = _signature(root, files)
    cached = _INDEX_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    chunks = _build_index_uncached(root, files)
    # One workspace at a time in practice; more would be a memory leak with a
    # long-lived REPL open on several projects.
    if len(_INDEX_CACHE) >= 4:
        _INDEX_CACHE.clear()
    _INDEX_CACHE[key] = (signature, chunks)
    return chunks


def _corpus_stats(chunks: list[Chunk], query_terms: list[str]) -> dict[str, Any]:
    document_frequency = {term: 0 for term in query_terms}
    total_length = 0
    for chunk in chunks:
        total_length += chunk.doc_length
        for term in query_terms:
            if chunk.term_freq.get(term):
                document_frequency[term] += 1
    count = len(chunks) or 1
    return {
        "df": document_frequency,
        "total_docs": count,
        "avg_doc_length": (total_length / count) or 1,
    }


def _snippet(chunk: Chunk, pattern: re.Pattern[str] | None) -> str:
    lines = chunk.code.split("\n")
    if pattern is not None:
        for line in lines:
            if pattern.search(line):
                return line.strip()[:160]
    return next((line.strip() for line in lines if line.strip()), "")[:160]


def hybrid_search(
    query: str,
    root: Path,
    files: list[str],
    *,
    mode: str = "hybrid",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Rank code chunks by exact match fused with meaning-based similarity.

    Modes: ``hybrid`` (default), ``regex`` and ``keyword`` (exact only - chunks
    that do not match are dropped), and ``semantic`` (ranking only, no pattern).
    """
    chunks = build_index(root, files)
    if not chunks:
        return []
    pattern = compile_pattern(query, mode)
    query_terms = list(dict.fromkeys(tokenize(query)))
    query_vector = embed(query)
    stats = _corpus_stats(chunks, query_terms)

    results: list[dict[str, Any]] = []
    for chunk in chunks:
        exact = False
        hits = 0
        if pattern is not None:
            found = pattern.findall(chunk.code)
            if found:
                exact = True
                hits = len(found)
        if mode in {"regex", "keyword"} and not exact:
            continue
        vector = cosine(query_vector, chunk.embedding)
        score = bm25_score(query_terms, chunk, stats) + VECTOR_WEIGHT * vector
        if mode == "semantic":
            score = vector
        if exact:
            score += EXACT_BOOST + min(hits, 5) * 0.2
        if score <= 0:
            continue
        results.append(
            {
                "file": chunk.path,
                "filepath": chunk.path,
                "line": chunk.start_line,
                "end_line": chunk.end_line,
                "symbol": chunk.symbol,
                "score": round(score, 4),
                "exact": exact,
                "text": _snippet(chunk, pattern),
            }
        )
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:limit]


def format_results(
    results: list[dict[str, Any]],
    query: str,
    mode: str,
    truncated: bool = False,
) -> str:
    """A compact block a small model can act on.

    The score and the exact/semantic marker are shown deliberately: an exact
    hit is a fact, a semantic-only hit is a lead, and a model that cannot tell
    them apart will treat a guess as ground truth and edit the wrong file.
    """
    if not results:
        return f'No results for "{query}" (mode: {mode}).'
    lines = [f'Hybrid search: "{query}" (mode: {mode}) - {len(results)} result(s)', ""]
    for item in results:
        marker = "*" if item["exact"] else "~"
        symbol = f" {item['symbol']}" if item["symbol"] else ""
        lines.append(
            f"{marker} {item['file']}:{item['line']}{symbol}  [score {item['score']}]"
        )
        if item["text"]:
            lines.append(f"    {item['text']}")
    lines.append("")
    lines.append("* exact + semantic match   ~ semantic only (a lead, not a fact)")
    if truncated:
        lines.append(
            "NOTE: the project was too large to index whole, so this searched "
            "the first portion of it (code before prose). Pass `path` to scope "
            "the search to a directory if what you want is missing."
        )
    return "\n".join(lines)
