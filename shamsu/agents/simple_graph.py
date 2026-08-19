"""Code-graph tools for simple mode.

Tool shapes taken from smallcode `bin/tools.js` - `graph_search` and
`explain_symbol` (MIT, (c) 2026 Doorman11991 - see reference/smallcode/LICENSE).
The backend is SHAMSU's own: the codebase-memory graph it already builds and
maintains.

The gap this closes is embarrassing rather than subtle. SHAMSU indexes a
workspace into a code graph - 161k nodes on this repo - and simple mode never
showed the model a single way to query it. The graph was reachable from
`/abstract`, from the REPL, and from a brief injected once per turn about files
the request happened to name by hand. The model itself could not ask
"who calls this" or "what does this function do", so it read files and guessed,
which is what the graph exists to prevent.

Both tools degrade to a plain, honest message when the graph is missing or
unindexed, because most workspaces never run `index_repository` and a tool that
raises is worse than one that says "not available here, use search_files".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# What one graph answer may put in the conversation. The graph can return a lot
# and it all lands in the window at full price.
MAX_GRAPH_TOKENS = 1500
MAX_LISTED = 15


def _adapter() -> Any:
    from shamsu.tools.codebase_memory import CodebaseMemoryAdapter

    return CodebaseMemoryAdapter()


def _unavailable(what: str) -> str:
    return (
        f"The code graph is not available for this workspace, so {what} could not "
        "be answered from it. Use search_files instead - it works without an "
        "index. (`/abstract build` indexes this workspace if you want the graph.)"
    )


def _staleness_warning(workspace: Path) -> str:
    """A line saying the graph may be out of date, when it may be.

    Not a nicety. Asked for `message_tokens` on this very repo, the graph
    confidently returned a function of that name from a VENDORED copy of
    another project and missed the one in `context/budget.py` written an hour
    earlier; `explain_symbol` reported `select_for_budget` had no callers while
    two sat in `simple_chat.py`. Both answers were wrong, both looked
    authoritative, and nothing in them hinted the index predated the code.

    SHAMSU already tracks this - `AbstractService.index_status()` knows the
    manifest hash has moved. The graph tools simply never asked.
    """
    try:
        from shamsu.abstract.service import AbstractService

        status = AbstractService(workspace).index_status()
    except Exception:  # noqa: BLE001
        return ""
    if not getattr(status, "stale", False):
        return ""
    return (
        chr(10)
        + "NOTE: the code graph is out of date with the files on disk, so this "
        + "may name things that have moved and may miss anything added since. "
        + "`/abstract refresh` rebuilds it; read_file and search_files are current."
    )


def _rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The result rows out of an adapter reply, whatever shape it used."""
    if not isinstance(payload, dict):
        return []
    if not payload.get("ok", True):
        return []
    data = payload.get("data")
    for candidate in (data, payload):
        if isinstance(candidate, dict):
            for key in ("results", "nodes", "rows", "matches", "symbols", "paths"):
                value = candidate.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _describe(row: dict[str, Any]) -> str:
    name = str(row.get("name") or row.get("qualified_name") or row.get("symbol") or "?")
    kind = row.get("labels") or row.get("kind") or row.get("type") or ""
    if isinstance(kind, list):
        kind = "/".join(str(k) for k in kind if k)
    where = str(row.get("file_path") or row.get("path") or row.get("file") or "")
    line = row.get("start_line") or row.get("line")
    location = f"{where}:{line}" if where and line else where
    parts = [f"- {name}"]
    if kind:
        parts.append(f"({kind})")
    if location:
        parts.append(f"- {location}")
    return " ".join(parts)


def _capped(lines: list[str], header: str) -> str:
    from shamsu.context.budget import count_tokens

    kept: list[str] = []
    used = count_tokens(header)
    for line in lines:
        cost = count_tokens(line)
        if used + cost > MAX_GRAPH_TOKENS:
            kept.append(f"... [{len(lines) - len(kept)} more, ask something narrower]")
            break
        kept.append(line)
        used += cost
    return header + "\n" + "\n".join(kept)


def graph_search(workspace: Path, query: str, limit: int = MAX_LISTED) -> tuple[bool, str]:
    """Find a symbol, function or class in the code graph.

    Answers "where is the auth logic" without reading a file, which on a large
    project is the difference between one call and six.
    """
    text = (query or "").strip()
    if not text:
        return False, "Pass a symbol name or a concept to look for."
    adapter = _adapter()
    try:
        if not adapter.is_available(workspace):
            return True, _unavailable(f"{text!r}")
        payload = adapter.query(workspace, text, limit=limit)
    except Exception as exc:  # noqa: BLE001 - never take a turn down
        return True, _unavailable(f"{text!r}") + f" ({type(exc).__name__})"
    rows = _rows(payload)
    if not rows:
        return True, (
            f"The code graph has nothing matching {text!r}. It may not be indexed, "
            "or the name may differ - search_files searches the text directly."
        )
    return True, _capped(
        [_describe(row) for row in rows[:limit]],
        f"Code graph: {len(rows)} match(es) for {text!r}",
    ) + _staleness_warning(workspace)


def explain_symbol(workspace: Path, symbol: str) -> tuple[bool, str]:
    """Where a symbol is defined and who calls it.

    The callers half is the part that cannot be got any other way: a text
    search finds the string, the graph finds the call.
    """
    name = (symbol or "").strip()
    if not name:
        return False, "Pass the symbol name to explain."
    adapter = _adapter()
    try:
        if not adapter.is_available(workspace):
            return True, _unavailable(f"the symbol {name!r}")
        defined = _rows(adapter.get_symbols(workspace, name))
        callers = _rows(adapter.get_references(workspace, name))
    except Exception as exc:  # noqa: BLE001
        return True, _unavailable(f"the symbol {name!r}") + f" ({type(exc).__name__})"
    if not defined and not callers:
        return True, (
            f"The code graph knows nothing about {name!r}. Try search_files, or "
            "`/abstract build` if this workspace has never been indexed."
        )
    lines: list[str] = []
    if defined:
        lines.append("Defined:")
        lines.extend(f"  {_describe(row)}" for row in defined[:5])
    if callers:
        lines.append(f"Called from ({len(callers)}):")
        lines.extend(f"  {_describe(row)}" for row in callers[:MAX_LISTED])
    else:
        lines.append(
            "No callers found in the graph - it may be an entry point, or reached "
            "dynamically."
        )
    return True, _capped(lines, f"Symbol: {name}") + _staleness_warning(workspace)


def format_notes(notes: list[Any]) -> str:
    """Memory notes as the model should see them listed."""
    if not notes:
        return "Nothing remembered for this project yet."
    by_type: dict[str, list[Any]] = {}
    for note in notes:
        by_type.setdefault(note.type, []).append(note)
    lines: list[str] = []
    for kind in sorted(by_type):
        lines.append(f"{kind} ({len(by_type[kind])}):")
        for note in by_type[kind]:
            marker = "" if note.tier == "hot" else " [archived]"
            lines.append(f"  [{note.id}] {note.title}{marker}: {note.content}")
    return "\n".join(lines)


def json_safe(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        return "{}"
