"""Tool/language detection and secret redaction shared by the digest pipeline."""
from __future__ import annotations

import re

from shamsu.diagnostics.types import DiagnosticRecord
from shamsu.safety.commands import redact

_TOOL_HINTS: list[tuple[str, str, str]] = [
    # (command/output substring, tool name, language)
    ("tsc", "tsc", "typescript"),
    ("vite", "vite", "typescript"),
    ("eslint", "eslint", "javascript"),
    ("npm run build", "npm build", "javascript"),
    ("npm run dev", "npm dev", "javascript"),
    ("npm test", "npm test", "javascript"),
    # Generic npm (install/ci/...) - keep AFTER the specific npm scripts so they
    # win; also matches "npm ERR!"/"npm error" in output for bare commands.
    ("npm", "npm", "javascript"),
    ("pytest", "pytest", "python"),
    ("ruff", "ruff", "python"),
    ("manage.py test", "django test", "python"),
    ("go test", "go test", "go"),
    ("cargo test", "cargo", "rust"),
    ("cargo build", "cargo", "rust"),
    ("python", "python", "python"),
    ("node", "node", "javascript"),
]


def detect_tool(command: str, stdout: str = "", stderr: str = "") -> tuple[str, str]:
    """Best-effort (tool, language) detection from the command line first,
    then the output when the command alone is ambiguous (e.g. `npm run
    build` invoking `tsc` under the hood)."""
    lowered_command = command.lower()
    for hint, tool, language in _TOOL_HINTS:
        if hint in lowered_command:
            combined = f"{stdout}\n{stderr}".lower()
            if hint.startswith("npm") and "tsc" in combined:
                return "tsc", "typescript"
            if hint.startswith("npm") and "eslint" in combined:
                return "eslint", "javascript"
            return tool, language

    combined = f"{stdout}\n{stderr}".lower()
    for hint, tool, language in _TOOL_HINTS:
        if hint in combined:
            return tool, language
    return "", ""


def redact_records(records: list[DiagnosticRecord]) -> list[DiagnosticRecord]:
    for record in records:
        record.message = redact(record.message)
        record.raw_excerpt = redact(record.raw_excerpt)
    return records


def build_summary(tool: str, exit_code: int, root_diagnostics: list[DiagnosticRecord]) -> str:
    if exit_code == 0 and not root_diagnostics:
        return f"{tool or 'command'} succeeded."
    if not root_diagnostics:
        return f"{tool or 'command'} failed (exit {exit_code}); no structured diagnostics were extracted."
    lead = root_diagnostics[0]
    location = f" at {lead.file}:{lead.line}" if lead.file else ""
    count_note = f" ({len(root_diagnostics)} root diagnostic(s))" if len(root_diagnostics) > 1 else ""
    code = f"{lead.code} " if lead.code else ""
    return f"{tool or 'command'} failed: {code}{lead.category}{location} - {lead.message}{count_note}".strip()


_SYMBOL_FILTER_RE = re.compile(r"^[A-Za-z_$][\w$]*$")


def collect_target_files(records: list[DiagnosticRecord]) -> list[str]:
    files: list[str] = []
    for record in records:
        if record.file and record.file not in files:
            files.append(record.file)
    return files


def collect_target_symbols(records: list[DiagnosticRecord]) -> list[str]:
    symbols: list[str] = []
    for record in records:
        if record.symbol and _SYMBOL_FILTER_RE.match(record.symbol) and record.symbol not in symbols:
            symbols.append(record.symbol)
    return symbols
