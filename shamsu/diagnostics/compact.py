"""Build the compact log text and recommended snippet list that make up an
ErrorPacket - the things a model should actually read instead of a raw log."""
from __future__ import annotations

from pathlib import Path

from shamsu.diagnostics.adapters import drain3_compactor
from shamsu.diagnostics.parsers.generic_fallback import strip_npm_boilerplate
from shamsu.diagnostics.types import DiagnosticRecord, RecommendedSnippet

SNIPPET_WINDOW = 30
MAX_COMPACT_LINES = 60


def build_compact_log(raw_output: str, tool: str, structured_diagnostic_count: int) -> tuple[str, int]:
    """Returns (compact_log_text, repeated_or_noise_lines_removed)."""
    lines = raw_output.splitlines()
    cleaned = strip_npm_boilerplate(lines)
    removed = len(lines) - len(cleaned)

    if drain3_compactor.is_noisy_runtime_log(tool, structured_diagnostic_count, len(cleaned)):
        templates, template_removed = drain3_compactor.compact(cleaned)
        removed += template_removed
        compact_lines = [f"{line} (x{count})" if count > 1 else line for line, count in templates]
    else:
        compact_lines, dedupe_removed = _dedupe_consecutive(cleaned)
        removed += dedupe_removed

    if len(compact_lines) > MAX_COMPACT_LINES:
        removed += len(compact_lines) - MAX_COMPACT_LINES
        compact_lines = compact_lines[:MAX_COMPACT_LINES]

    return "\n".join(compact_lines), removed


def _dedupe_consecutive(lines: list[str]) -> tuple[list[str], int]:
    result: list[str] = []
    removed = 0
    previous: str | None = None
    for line in lines:
        if line == previous:
            removed += 1
            continue
        result.append(line)
        previous = line
    return result, removed


def recommend_snippets(
    workspace_root: Path,
    root_diagnostics: list[DiagnosticRecord],
) -> list[RecommendedSnippet]:
    snippets: list[RecommendedSnippet] = []
    seen: set[tuple[str, int, int]] = set()

    for record in root_diagnostics:
        if record.file and record.line:
            _add_snippet(workspace_root, snippets, seen, record.file, record.line, record.category)
        if record.module:
            resolved = resolve_module_path(workspace_root, record.file, record.module)
            if resolved:
                _add_snippet(workspace_root, snippets, seen, resolved, 1, "exporter for " + record.category)

    return snippets


def _add_snippet(
    workspace_root: Path,
    snippets: list[RecommendedSnippet],
    seen: set[tuple[str, int, int]],
    file_path: str,
    line: int,
    reason: str,
) -> None:
    target = (workspace_root / file_path).resolve()
    try:
        target.relative_to(workspace_root)
        line_count = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
    except (OSError, ValueError):
        line_count = line + SNIPPET_WINDOW

    start = max(line - SNIPPET_WINDOW, 1)
    end = min(line + SNIPPET_WINDOW, max(line_count, line))
    key = (file_path, start, end)
    if key in seen:
        return
    seen.add(key)
    snippets.append(RecommendedSnippet(file=file_path, line_start=start, line_end=end, reason=reason))


def resolve_module_path(workspace_root: Path, importer: str, module_path: str) -> str:
    """Best-effort resolution of a relative/bare import specifier to a
    workspace-relative file path, shared by snippet recommendation and
    DiagnosticDigest's Codebase-Memory MCP lookups."""
    module_path = module_path.replace("\\", "/").lstrip("/")
    bases = [(workspace_root / importer).parent] if importer else []
    bases.append(workspace_root)
    candidates = [base / module_path for base in bases] if module_path.startswith(".") else [workspace_root / module_path]
    suffixes = ["", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js", "/index.jsx"]
    for candidate in candidates:
        for suffix in suffixes:
            path = Path(str(candidate) + suffix).resolve()
            if path.is_file():
                try:
                    return path.relative_to(workspace_root).as_posix()
                except ValueError:
                    return ""
    return ""
