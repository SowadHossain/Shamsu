"""errorformat-style parsing for compiler/linter text output.

reviewdog/errorformat (https://github.com/reviewdog/errorformat) is a real
Go tool/library, but it ships no offline-installable Python package and
its CLI expects a VCS diff context to post comments against - not a fit
for "normalize one command's stdout" without faking an integration we
haven't actually wired up. This module implements the same small set of
well-known errorformat patterns directly (a handful of regexes, not a full
errorformat DSL), which is what section 7 of the diagnostics prompt asks
SHAMSU to natively support:

- `file:line:column: message`
- `file:line: message`
- `file(line,column): error CODE: message`

If a real `reviewdog` binary is later configured (`SHAMSU_REVIEWDOG_BIN`),
`external_binary()` reports it for `/diagnostics doctor`, but this parser
never shells out to it - only doctor's status output changes.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

from shamsu.diagnostics.types import DiagnosticRecord

_PATTERNS = [
    # file(line,column): error CODE: message  (tsc-style)
    re.compile(
        r"^(?P<file>[\w./\\-]+)\((?P<line>\d+),(?P<column>\d+)\):\s*"
        r"(?P<severity>error|warning)\s+(?P<code>[A-Z]+\d+):\s*(?P<message>.+)$"
    ),
    # file:line:column: message
    re.compile(r"^(?P<file>[\w./\\-]+):(?P<line>\d+):(?P<column>\d+):\s*(?P<message>.+)$"),
    # file:line: message
    re.compile(r"^(?P<file>[\w./\\-]+):(?P<line>\d+):\s*(?P<message>.+)$"),
]


def parse(text: str) -> list[DiagnosticRecord]:
    records: list[DiagnosticRecord] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in _PATTERNS:
            match = pattern.match(stripped)
            if not match:
                continue
            groups = match.groupdict()
            records.append(
                DiagnosticRecord(
                    severity=groups.get("severity", "error"),
                    code=groups.get("code", ""),
                    category="generic",
                    message=groups["message"].strip(),
                    file=groups["file"],
                    line=int(groups["line"]),
                    column=int(groups["column"]) if groups.get("column") else None,
                    raw_excerpt=stripped,
                    parser_name="reviewdog_errorformat",
                    confidence=0.75,
                )
            )
            break
    return records


def external_binary() -> str | None:
    """Path to a real reviewdog binary, if the user has configured/installed
    one - informational only, see module docstring."""
    explicit = os.environ.get("SHAMSU_REVIEWDOG_BIN", "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else None
    found = shutil.which("reviewdog")
    return found
