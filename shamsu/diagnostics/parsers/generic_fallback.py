"""Last-resort generic diagnostic parser.

Used only when no native structured output, SARIF, or tool-specific fallback
parser recognized the output. Extracts `path:line[:column]: message` and
strips common npm lifecycle boilerplate so it doesn't get treated as a
diagnostic or pollute the compact log.
"""
from __future__ import annotations

import re

from shamsu.diagnostics.types import DiagnosticRecord

_GENERIC_LOCATION_RE = re.compile(
    r"^(?P<file>[\w./\\-]+):(?P<line>\d+)(?::(?P<column>\d+))?:\s*(?P<message>.+)$"
)

NPM_BOILERPLATE_PATTERNS = [
    re.compile(r"^npm (notice|warn|WARN)\b"),
    re.compile(r"^npm ERR! (code|errno|A complete log|For a full report)"),
    re.compile(r"^> [\w@./-]+@[\w.\-]+ \w+$"),
    re.compile(r"^>\s*(tsc|vite|eslint|next|webpack|jest)\b"),
    re.compile(r"^added \d+ packages? in"),
    re.compile(r"^\s*$"),
]


def is_npm_boilerplate(line: str) -> bool:
    return any(pattern.search(line) for pattern in NPM_BOILERPLATE_PATTERNS)


def strip_npm_boilerplate(lines: list[str]) -> list[str]:
    return [line for line in lines if not is_npm_boilerplate(line)]


def parse_generic_locations(text: str) -> list[DiagnosticRecord]:
    records: list[DiagnosticRecord] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or is_npm_boilerplate(stripped):
            continue
        match = _GENERIC_LOCATION_RE.match(stripped)
        if not match:
            continue
        records.append(
            DiagnosticRecord(
                severity="error",
                category="generic",
                message=match.group("message").strip(),
                file=match.group("file"),
                line=int(match.group("line")),
                column=int(match.group("column")) if match.group("column") else None,
                raw_excerpt=stripped,
                parser_name="generic_fallback",
                confidence=0.5,
            )
        )
    return records
