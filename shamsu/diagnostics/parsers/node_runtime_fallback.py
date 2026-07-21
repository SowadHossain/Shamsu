"""Small fallback parser for noisy Node/Vite/browser dev-server runtime logs.

This is a signal source for Drain3-style compaction (`adapters/drain3_compactor`),
not for exact compiler diagnostics - line numbers/symbols here are best-effort.
"""
from __future__ import annotations

import re

from shamsu.diagnostics.types import DiagnosticRecord

MODULE_NOT_FOUND_RE = re.compile(
    r"(?:Cannot find module|Module not found: Error: Can't resolve)\s+['\"](?P<module>[^'\"]+)['\"]"
)
UNCAUGHT_RE = re.compile(r"^Uncaught\s+(?P<etype>[A-Za-z]+):\s*(?P<message>.+)$")


def parse_node_runtime_errors(text: str) -> list[DiagnosticRecord]:
    records: list[DiagnosticRecord] = []
    for line in text.splitlines():
        stripped = line.strip()
        module_match = MODULE_NOT_FOUND_RE.search(stripped)
        if module_match:
            records.append(
                DiagnosticRecord(
                    tool="node",
                    language="javascript",
                    severity="error",
                    category="module_not_found",
                    message=stripped,
                    module=module_match.group("module"),
                    raw_excerpt=stripped,
                    parser_name="node_runtime_fallback",
                    confidence=0.6,
                )
            )
            continue
        uncaught_match = UNCAUGHT_RE.match(stripped)
        if uncaught_match:
            records.append(
                DiagnosticRecord(
                    tool="node",
                    language="javascript",
                    severity="error",
                    category="runtime_exception",
                    code=uncaught_match.group("etype"),
                    message=uncaught_match.group("message"),
                    raw_excerpt=stripped,
                    parser_name="node_runtime_fallback",
                    confidence=0.5,
                )
            )
    return records
