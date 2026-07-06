"""Small fallback parser for TypeScript/tsc, Vite/browser module errors.

Only used when native structured output (e.g. `tsc --pretty false` doesn't
emit JSON) and the generic errorformat-style adapter don't already cover a
line. Deliberately narrow: it hand-parses the exact shapes tsc/Vite are
known to emit, not a general compiler-output grammar.
"""
from __future__ import annotations

import re

from shamsu.diagnostics.types import DiagnosticRecord

# src/game/rules.ts(71,17): error TS1005: ')' expected.
TSC_LOCATION_RE = re.compile(
    r"^(?P<file>[\w./\\-]+\.tsx?)\((?P<line>\d+),(?P<column>\d+)\):\s*"
    r"(?P<severity>error|warning)\s+(?P<code>TS\d+):\s*(?P<message>.+)$"
)

SYNTAX_ERROR_HINTS = (
    "expected",
    "unexpected token",
    "unterminated",
    "declaration or statement expected",
)

# Module '"./rules"' has no exported member 'World'.
# Module '"./loop"' has no exported member named 'GameLoop'. Did you mean 'gameLoop'?
# The requested module '/src/game/loop.ts' does not provide an export named 'GameLoop'
MISSING_EXPORT_RE = re.compile(
    r"(?:has no exported member(?: named)?|does not provide an export named)\s+"
    r"['\"](?P<name>[A-Za-z_$][\w$]*)['\"]",
    re.IGNORECASE,
)
MODULE_PATH_RE = re.compile(
    r"(?:[Mm]odule\s+['\"](?P<module>[^'\"]+)['\"]|requested module\s+['\"](?P<requested>[^'\"]+)['\"])"
)
DID_YOU_MEAN_RE = re.compile(r"Did you mean\s+['\"](?P<suggestion>[A-Za-z_$][\w$]*)['\"]")

# Uncaught SyntaxError: The requested module '/src/game/loop.ts' does not
# provide an export named 'GameLoop'
BROWSER_RUNTIME_RE = re.compile(
    r"Uncaught SyntaxError:.*requested module\s+['\"](?P<module>[^'\"]+)['\"].*"
    r"does not provide an export named\s+['\"](?P<name>[A-Za-z_$][\w$]*)['\"]",
)


def _syntax_category(message: str) -> str:
    lowered = message.lower()
    return "syntax_error" if any(hint in lowered for hint in SYNTAX_ERROR_HINTS) else "type_error"


def parse_tsc_errors(text: str) -> list[DiagnosticRecord]:
    records: list[DiagnosticRecord] = []
    for line in text.splitlines():
        match = TSC_LOCATION_RE.match(line.strip())
        if not match:
            continue
        message = match.group("message").strip()
        records.append(
            DiagnosticRecord(
                tool="tsc",
                language="typescript",
                severity=match.group("severity"),
                code=match.group("code"),
                category=_syntax_category(message),
                message=message,
                file=match.group("file"),
                line=int(match.group("line")),
                column=int(match.group("column")),
                raw_excerpt=line.strip(),
                parser_name="typescript_fallback",
                confidence=0.9,
            )
        )
    return records


def parse_missing_export(text: str) -> list[DiagnosticRecord]:
    records: list[DiagnosticRecord] = []
    for line in text.splitlines():
        stripped = line.strip()
        missing = MISSING_EXPORT_RE.search(stripped)
        if not missing:
            continue
        module_match = MODULE_PATH_RE.search(stripped)
        module_path = ""
        if module_match:
            module_path = module_match.group("module") or module_match.group("requested") or ""
        suggestion = DID_YOU_MEAN_RE.search(stripped)
        message = stripped
        records.append(
            DiagnosticRecord(
                tool="tsc",
                language="typescript",
                severity="error",
                category="missing_export",
                message=message,
                module=module_path,
                symbol=missing.group("name"),
                related_locations=[suggestion.group("suggestion")] if suggestion else [],
                raw_excerpt=stripped,
                parser_name="typescript_fallback",
                confidence=0.85,
            )
        )
    return records


def parse_browser_missing_export(text: str) -> list[DiagnosticRecord]:
    records: list[DiagnosticRecord] = []
    for line in text.splitlines():
        stripped = line.strip()
        match = BROWSER_RUNTIME_RE.search(stripped)
        if not match:
            continue
        records.append(
            DiagnosticRecord(
                tool="vite",
                language="typescript",
                severity="error",
                category="runtime_missing_export",
                message=stripped,
                module=match.group("module"),
                symbol=match.group("name"),
                raw_excerpt=stripped,
                parser_name="typescript_fallback",
                confidence=0.85,
            )
        )
    return records


def parse(text: str) -> list[DiagnosticRecord]:
    return parse_tsc_errors(text) + parse_missing_export(text) + parse_browser_missing_export(text)
