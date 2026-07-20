"""Normalized diagnostic types shared across the diagnostics package.

`DiagnosticRecord` is a SARIF-style normalized diagnostic (one compiler
error, lint finding, test failure, or traceback). `ErrorPacket` is the
compact, deterministic digest of a single command's output that SHAMSU's
model-facing layers should read instead of raw logs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class DiagnosticRecord:
    tool: str = ""
    language: str = ""
    severity: str = "error"
    code: str = ""
    category: str = "generic"
    message: str = ""
    file: str = ""
    line: int | None = None
    column: int | None = None
    symbol: str = ""
    module: str = ""
    stack_frame: str = ""
    related_locations: list[str] = field(default_factory=list)
    raw_excerpt: str = ""
    parser_name: str = ""
    confidence: float = 1.0
    count: int = 1

    def identity(self) -> tuple[str, str, int | None, str]:
        """Grouping key used to dedupe/count repeated diagnostics."""
        return (self.code, self.file, self.line, self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "language": self.language,
            "severity": self.severity,
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "symbol": self.symbol,
            "module": self.module,
            "stack_frame": self.stack_frame,
            "related_locations": list(self.related_locations),
            "raw_excerpt": self.raw_excerpt,
            "parser_name": self.parser_name,
            "confidence": self.confidence,
            "count": self.count,
        }


@dataclass
class RecommendedSnippet:
    file: str
    line_start: int
    line_end: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "reason": self.reason,
        }


@dataclass
class ErrorPacket:
    command: str = ""
    cwd: str = ""
    exit_code: int = 0
    tool: str = ""
    parser_chain: list[str] = field(default_factory=list)
    summary: str = ""
    root_diagnostics: list[DiagnosticRecord] = field(default_factory=list)
    secondary_diagnostics: list[DiagnosticRecord] = field(default_factory=list)
    repeated_noise_removed: int = 0
    target_files: list[str] = field(default_factory=list)
    target_symbols: list[str] = field(default_factory=list)
    related_code_facts: list[str] = field(default_factory=list)
    recommended_snippets: list[RecommendedSnippet] = field(default_factory=list)
    compact_log: str = ""
    raw_log_path: str = ""
    classification: str = "auto"
    actionable: bool = False
    phase: str = "execution"
    operation_id: str = ""
    exception_class: str = ""
    exception_message: str = ""
    traceback_path: str = ""
    suggested_next_check: str = ""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self) -> None:
        if self.classification == "auto":
            self.classification = "success" if self.exit_code == 0 else "command_failure"
            self.actionable = self.exit_code != 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.root_diagnostics

    def signature(self) -> str:
        """Stable identity for stall-guard comparisons across iterations."""
        if self.root_diagnostics:
            return "|".join(
                f"{d.category}:{d.code}:{d.file}:{d.line}:{d.message}" for d in self.root_diagnostics
            )
        return f"exit={self.exit_code}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "tool": self.tool,
            "parser_chain": list(self.parser_chain),
            "summary": self.summary,
            "root_diagnostics": [d.to_dict() for d in self.root_diagnostics],
            "secondary_diagnostics": [d.to_dict() for d in self.secondary_diagnostics],
            "repeated_noise_removed": self.repeated_noise_removed,
            "target_files": list(self.target_files),
            "target_symbols": list(self.target_symbols),
            "related_code_facts": list(self.related_code_facts),
            "recommended_snippets": [s.to_dict() for s in self.recommended_snippets],
            "compact_log": self.compact_log,
            "raw_log_path": self.raw_log_path,
            "classification": self.classification,
            "actionable": self.actionable,
            "phase": self.phase,
            "operation_id": self.operation_id,
            "exception_class": self.exception_class,
            "exception_message": self.exception_message,
            "traceback_path": self.traceback_path,
            "suggested_next_check": self.suggested_next_check,
            "created_at": self.created_at,
        }

    def to_model_context(self) -> str:
        """Compact text block DiagnosticDigest's consumers should feed the LLM
        instead of raw logs."""
        lines = [f"ErrorPacket for `{self.command}` (exit {self.exit_code}, tool={self.tool or 'unknown'})"]
        if self.summary:
            lines.append(self.summary)
        lines.append(f"Classification: {self.classification} (actionable={self.actionable})")
        if self.root_diagnostics:
            lines.append("Root diagnostics:")
            for record in self.root_diagnostics:
                location = f"{record.file}:{record.line}" if record.file else ""
                lines.append(f"- [{record.category}] {record.code} {location} {record.message}".strip())
        if self.related_code_facts:
            lines.append("Related code facts:")
            lines.extend(f"- {fact}" for fact in self.related_code_facts)
        if self.recommended_snippets:
            lines.append("Recommended snippets to inspect:")
            for snippet in self.recommended_snippets:
                lines.append(f"- {snippet.file} lines {snippet.line_start}-{snippet.line_end} ({snippet.reason})")
        if self.repeated_noise_removed:
            lines.append(f"({self.repeated_noise_removed} repeated/boilerplate line(s) removed)")
        if self.suggested_next_check:
            lines.append(f"Suggested next check: {self.suggested_next_check}")
        return "\n".join(lines)
