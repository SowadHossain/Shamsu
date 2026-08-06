"""Compact Codebase-Memory MCP fact briefs for pre-edit/bugfix context.

Token-saving rule: prefer these compact structural facts over sending whole
files. Best-effort - if Codebase-Memory MCP is unavailable, returns "" so
callers fall back to their existing (search-based) context unchanged.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.abstract.service import AbstractService

MAX_NAMES_PER_SECTION = 12


def build_codebase_memory_brief(
    workspace_root: Path,
    target_paths: list[str],
    service: AbstractService | None = None,
) -> str:
    if not target_paths:
        return ""
    service = service or AbstractService(workspace_root)
    health = service.adapter.healthcheck(service.workspace)
    if not health.ok:
        return ""

    lines: list[str] = ["Codebase-Memory MCP facts (prefer these over guessing):"]
    for path in target_paths[:3]:
        exports = _names(service.adapter.get_exports(service.workspace, path))
        imports = _names(service.adapter.get_imports(service.workspace, path))
        if exports:
            lines.append(f"- {path} exports: {', '.join(exports[:MAX_NAMES_PER_SECTION])}")
        if imports:
            lines.append(f"- {path} imports: {', '.join(imports[:MAX_NAMES_PER_SECTION])}")
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _names(result: dict) -> list[str]:
    """Extract a "name" column from either query_graph's columnar
    {"columns": [...], "rows": [[...], ...]} shape or search_graph's
    {"results": [{"name": ...}, ...]} shape - verified against the real tool."""
    if not result.get("ok"):
        return []
    names: list[str] = []
    columns = result.get("columns")
    rows = result.get("rows")
    if columns and rows is not None:
        name_index = columns.index("name") if "name" in columns else 0
        for row in rows:
            value = row[name_index] if isinstance(row, list) else row
            if value:
                names.append(str(value))
        return names
    for row in result.get("results") or []:
        name = row.get("name") if isinstance(row, dict) else row
        if name:
            names.append(str(name))
    return names
