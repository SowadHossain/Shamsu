"""Repair-loop error model.

The diagnostics layer already produces a per-command `ErrorPacket` holding
normalized `DiagnosticRecord`s. The repair loop needs a *flatter*, per-error
view carrying the command context (command/exit code) plus a coarse `kind`
and a per-error `signature` for stall detection. `RepairError` is that view -
it is derived from the existing diagnostics, never a second parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from shamsu.diagnostics.types import DiagnosticRecord, ErrorPacket


class ErrorKind(str, Enum):
    SYNTAX_ERROR = "SYNTAX_ERROR"
    TYPE_ERROR = "TYPE_ERROR"
    IMPORT_ERROR = "IMPORT_ERROR"
    EXPORT_ERROR = "EXPORT_ERROR"
    MODULE_NOT_FOUND = "MODULE_NOT_FOUND"
    MISSING_SYMBOL = "MISSING_SYMBOL"
    JSX_ERROR = "JSX_ERROR"
    UNKNOWN = "UNKNOWN"


# Root-cause priority for the repair loop, per the issue's ordering:
# syntax first, import/module second, missing exports/symbols third,
# type errors fourth, implicit-any last. Lower number = fixed first.
KIND_PRIORITY: dict[ErrorKind, int] = {
    ErrorKind.SYNTAX_ERROR: 0,
    ErrorKind.IMPORT_ERROR: 1,
    ErrorKind.MODULE_NOT_FOUND: 1,
    ErrorKind.EXPORT_ERROR: 2,
    ErrorKind.MISSING_SYMBOL: 2,
    ErrorKind.JSX_ERROR: 3,
    ErrorKind.TYPE_ERROR: 3,
    ErrorKind.UNKNOWN: 4,
}
_IMPLICIT_ANY_PRIORITY = 5  # always last, even though its kind is TYPE_ERROR

_IMPLICIT_ANY_RE = re.compile(r"implicitly has an? .*any.* type", re.IGNORECASE)
_JSX_HINT_RE = re.compile(r"\bJSX\b", re.IGNORECASE)

# TS error codes are the most reliable classifier; message/category are the
# fallback for Vite/runtime lines that carry no TS code.
_TS_CODE_KIND: dict[str, ErrorKind] = {
    "TS1005": ErrorKind.SYNTAX_ERROR,
    "TS1109": ErrorKind.SYNTAX_ERROR,
    "TS1128": ErrorKind.SYNTAX_ERROR,
    "TS1002": ErrorKind.SYNTAX_ERROR,
    "TS2304": ErrorKind.MISSING_SYMBOL,   # Cannot find name 'X'
    "TS2305": ErrorKind.EXPORT_ERROR,     # Module has no exported member 'X'
    "TS2307": ErrorKind.MODULE_NOT_FOUND,  # Cannot find module 'X'
    "TS2614": ErrorKind.EXPORT_ERROR,
    "TS2724": ErrorKind.EXPORT_ERROR,     # has no exported member named 'X'
    "TS6142": ErrorKind.JSX_ERROR,
    "TS17004": ErrorKind.JSX_ERROR,
    "TS7006": ErrorKind.TYPE_ERROR,       # parameter implicitly 'any'
    "TS7031": ErrorKind.TYPE_ERROR,
}

_CATEGORY_KIND: dict[str, ErrorKind] = {
    "syntax_error": ErrorKind.SYNTAX_ERROR,
    "import_error": ErrorKind.IMPORT_ERROR,
    "module_not_found": ErrorKind.MODULE_NOT_FOUND,
    "missing_export": ErrorKind.EXPORT_ERROR,
    "import_export_mismatch": ErrorKind.EXPORT_ERROR,
    "runtime_missing_export": ErrorKind.EXPORT_ERROR,
    "type_error": ErrorKind.TYPE_ERROR,
    "compiler_error": ErrorKind.TYPE_ERROR,
}


def classify(record: DiagnosticRecord) -> ErrorKind:
    """Map a normalized diagnostic to a coarse repair kind. Code first
    (precise), then category, then message keywords."""
    code = (record.code or "").upper()
    if code in _TS_CODE_KIND:
        return _TS_CODE_KIND[code]
    message = record.message or ""
    if _JSX_HINT_RE.search(message) and "jsx" in message.lower():
        return ErrorKind.JSX_ERROR
    kind = _CATEGORY_KIND.get(record.category)
    if kind is not None:
        return kind
    lowered = message.lower()
    if "failed to resolve import" in lowered:
        return ErrorKind.IMPORT_ERROR
    if "cannot find module" in lowered or "can't resolve" in lowered:
        return ErrorKind.MODULE_NOT_FOUND
    if "has no exported member" in lowered or "does not provide an export" in lowered:
        return ErrorKind.EXPORT_ERROR
    if "cannot find name" in lowered:
        return ErrorKind.MISSING_SYMBOL
    return ErrorKind.UNKNOWN


@dataclass(frozen=True)
class RepairError:
    command: str
    exit_code: int
    tool: str
    kind: ErrorKind
    file: str
    line: int | None
    column: int | None
    code: str
    symbol: str
    module: str
    message: str
    raw_block: str
    severity: str

    @property
    def is_implicit_any(self) -> bool:
        return bool(_IMPLICIT_ANY_RE.search(self.message))

    @property
    def priority(self) -> int:
        if self.is_implicit_any:
            return _IMPLICIT_ANY_PRIORITY
        return KIND_PRIORITY.get(self.kind, 4)

    def signature(self) -> str:
        """Stable per-error identity for before/after comparison and stall
        detection. Excludes column (noisy) and the raw block (volatile)."""
        return f"{self.kind.value}:{self.code}:{self.file}:{self.line}:{self.symbol}:{self.message.strip()}"


def _record_to_error(packet: ErrorPacket, record: DiagnosticRecord) -> RepairError:
    return RepairError(
        command=packet.command,
        exit_code=packet.exit_code,
        tool=record.tool or packet.tool,
        kind=classify(record),
        file=record.file,
        line=record.line,
        column=record.column,
        code=record.code,
        symbol=record.symbol,
        module=record.module,
        message=record.message,
        raw_block=record.raw_excerpt,
        severity=record.severity,
    )


def repair_errors_from_packet(packet: ErrorPacket) -> list[RepairError]:
    """Flatten a DiagnosticDigest ErrorPacket into RepairErrors. Root
    diagnostics come first (they are the cascade source), then secondary."""
    errors = [_record_to_error(packet, r) for r in packet.root_diagnostics]
    errors += [_record_to_error(packet, r) for r in packet.secondary_diagnostics]
    return errors


def select_primary_error(errors: list[RepairError]) -> RepairError | None:
    """Pick the single blocking root error to fix this iteration, so debug
    mode patches one root cause at a time. Priority order (issue §3):
    syntax > import/module > missing export/symbol > type > implicit any.
    Ties break toward errors that carry a file location (actionable)."""
    if not errors:
        return None
    return min(
        errors,
        key=lambda e: (e.priority, 0 if e.file else 1, e.line if e.line is not None else 1_000_000),
    )
