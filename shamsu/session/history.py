"""The whole conversation, across forks, and how to get back into it.

Two problems this solves, and they are the same problem seen from two ends.

**The window is not the conversation.** `messages.jsonl` is append-only and
lossless - every byte a session ever exchanged is on disk. But the model only
ever sees what fits in the window, and everything older survives as a rolling
summary: a few lines standing in for a hundred turns. Ask about a decision from
turn three and it is gone, not because it was lost but because nothing could
reach it. The fix is not a bigger window. It is a way to *search the archive*,
so the whole history is retrievable on demand at the cost of the few lines that
answer the question.

**Forking usually loses the thread.** OpenCode compacts by starting a NEW
session seeded with a summary; the old conversation still exists but nothing
links the two, so "what did we decide last week" has to be answered by the user
remembering which session it was in. SHAMSU's own plan rejected forking for
exactly that reason and chose to compact in place - which trades the
fragmentation for lossy overwriting instead.

A fork with a **parent pointer** avoids both. History becomes a tree rather
than a list: the child starts clean with a summary, the parent keeps every byte,
and the link between them is walkable. Nothing is overwritten and nothing is
orphaned, so `history_search` can search a conversation that spans a dozen
forks and months of work as though it were one thread - which, to the user, it
is.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# How many transcript hits one search may return, and how much of each. Enough
# to answer "what did we decide", not enough to refill the window with the
# history we just went to the trouble of evicting.
MAX_HISTORY_HITS = 8
MAX_HIT_CHARS = 400


@dataclass
class HistoryHit:
    session_id: str
    session_title: str
    index: int
    role: str
    text: str
    score: float

    def render(self) -> str:
        where = f"{self.session_title} #{self.index}"
        body = self.text if len(self.text) <= MAX_HIT_CHARS else self.text[:MAX_HIT_CHARS] + " ..."
        return f"[{where}] {self.role}: {body}"


def ancestry(manager: Any, session_id: str) -> list[Any]:
    """This session and every session it was forked from, oldest last.

    Guarded against a cycle. A corrupted or hand-edited `parent_session_id`
    that points back into its own chain would otherwise hang the search, and a
    hang while looking something up is indistinguishable from a crash.
    """
    chain: list[Any] = []
    seen: set[str] = set()
    current = str(session_id or "").strip()
    while current and current not in seen:
        seen.add(current)
        metadata = _metadata(manager, current)
        if metadata is None:
            break
        chain.append(metadata)
        current = str(getattr(metadata, "parent_session_id", "") or "")
    return chain


def _metadata(manager: Any, session_id: str) -> Any:
    for item in manager.list_sessions():
        if getattr(item, "session_id", "") == session_id:
            return item
    return None


def _read_transcript(manager: Any, session_id: str) -> list[dict[str, Any]]:
    path = Path(manager.workspace) / ".shamsu" / "sessions" / session_id / "messages.jsonl"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            records.append(entry)
    return records


def search_history(
    manager: Any,
    session_id: str,
    query: str,
    limit: int = MAX_HISTORY_HITS,
) -> list[HistoryHit]:
    """Find turns matching *query* anywhere in this conversation's ancestry.

    Ranked with the same hashed BM25 used for code search, so a question in
    plain English reaches a turn that never used those words. Tool payloads are
    skipped: they are the bulk of any transcript and the least likely thing
    anyone means by "what did we say about X".
    """
    from shamsu.tools.hybrid_search import bm25_score, cosine, embed, tokenize
    from shamsu.tools.hybrid_search import Chunk

    chain = ancestry(manager, session_id)
    if not chain:
        return []

    candidates: list[tuple[Any, int, dict[str, Any], Chunk]] = []
    for metadata in chain:
        sid = getattr(metadata, "session_id", "")
        for index, record in enumerate(_read_transcript(manager, sid), start=1):
            role = str(record.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            content = str(record.get("content") or "").strip()
            if not content:
                continue
            tokens = tokenize(content)
            if not tokens:
                continue
            freq: dict[str, int] = {}
            for token in tokens:
                freq[token] = freq.get(token, 0) + 1
            chunk = Chunk(
                path=sid,
                start_line=index,
                end_line=index,
                symbol=role,
                code=content,
                term_freq=freq,
                doc_length=len(tokens),
                embedding=embed(content),
            )
            candidates.append((metadata, index, record, chunk))

    if not candidates:
        return []

    query_terms = list(dict.fromkeys(tokenize(query)))
    query_vector = embed(query)
    document_frequency = {term: 0 for term in query_terms}
    total_length = 0
    for _m, _i, _r, chunk in candidates:
        total_length += chunk.doc_length
        for term in query_terms:
            if chunk.term_freq.get(term):
                document_frequency[term] += 1
    stats = {
        "df": document_frequency,
        "total_docs": len(candidates),
        "avg_doc_length": (total_length / len(candidates)) or 1,
    }

    hits: list[HistoryHit] = []
    for metadata, index, record, chunk in candidates:
        score = bm25_score(query_terms, chunk, stats) + 0.6 * cosine(query_vector, chunk.embedding)
        if score <= 0:
            continue
        hits.append(
            HistoryHit(
                session_id=getattr(metadata, "session_id", ""),
                session_title=str(getattr(metadata, "title", "") or "session"),
                index=index,
                role=str(record.get("role") or ""),
                text=str(record.get("content") or ""),
                score=round(score, 4),
            )
        )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:limit]


def fork_session(manager: Any, session_id: str, title: str | None = None) -> Any:
    """Start a fresh session that remembers where it came from.

    The child begins with a clean window and the parent's summary; the parent
    keeps every byte it ever held. Because the link is recorded rather than
    implied, `search_history` can still reach across it - so forking costs
    nothing in recall, which is the only reason it is safe to offer.
    """
    parent = _metadata(manager, session_id)
    if parent is None:
        raise ValueError(f"No session {session_id!r} to fork.")
    parent_title = str(getattr(parent, "title", "") or "session")
    child = manager.create_session(
        title or f"{parent_title} (continued)",
        parent_session_id=getattr(parent, "session_id", ""),
    )
    summary = str(getattr(parent, "summary", "") or "").strip()
    carried = summary or "(the earlier conversation had produced no summary yet)"
    try:
        child.save_summary(
            "Continued from an earlier session. What it had established:\n" + carried,
            1,
        )
    except Exception:  # noqa: BLE001 - a fork that cannot carry the summary is
        pass          # still a valid fork; the parent transcript is searchable.
    child.log(
        "session.forked",
        {"parent_session_id": getattr(parent, "session_id", ""), "parent_title": parent_title},
        f"Forked from {parent_title}",
    )
    return child


def render_hits(hits: list[HistoryHit], query: str) -> str:
    if not hits:
        return (
            f'Nothing in this conversation history matches "{query}". '
            "It may have been in a different session, or phrased very differently."
        )
    lines = [f'From earlier in this conversation ({len(hits)} match(es) for "{query}"):', ""]
    lines.extend(hit.render() for hit in hits)
    return "\n".join(lines)
