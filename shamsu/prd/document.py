"""Deterministic normalization for paged PRD documents."""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

PAGE_NUMBER_RE = re.compile(r"^(?:page\s+)?\d+(?:\s+of\s+\d+)?$", re.IGNORECASE)
BULLET_RE = re.compile(r"^(?:[\u2022*-]|\d+[.)])\s+")
NUMBERED_HEADING_RE = re.compile(r"^\d+(?:\.\d+)*\.?\s+[A-Z][^.!?]{1,100}$")


@dataclass(frozen=True)
class DocumentLine:
    text: str
    page: int


@dataclass
class NormalizedPRDDocument:
    lines: list[DocumentLine]
    tables: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 1.0
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(item.text for item in self.lines)

    @property
    def line_pages(self) -> list[int]:
        return [item.page for item in self.lines]


def normalize_pdf_pages(
    page_text: list[str],
    tables: list[dict[str, Any]] | None = None,
) -> NormalizedPRDDocument:
    """Remove layout noise and conservatively join PDF-wrapped prose."""
    raw_pages = [_clean_page_lines(text, index + 1) for index, text in enumerate(page_text)]
    repeated = _repeated_margin_lines(raw_pages)
    output: list[DocumentLine] = []
    empty_pages = 0
    for page, lines in enumerate(raw_pages, start=1):
        lines = [line for line in lines if _key(line) not in repeated]
        if not lines:
            empty_pages += 1
            continue
        for text in _join_wrapped_lines(lines):
            output.append(DocumentLine(text=text, page=page))

    warnings: list[str] = []
    total_pages = max(1, len(page_text))
    empty_ratio = empty_pages / total_pages
    char_count = sum(len(item.text) for item in output)
    confidence = 0.98
    if empty_ratio > 0.2:
        confidence -= 0.35
        warnings.append(f"Text extraction was empty on {empty_pages} of {total_pages} pages.")
    if char_count < total_pages * 80:
        confidence -= 0.35
        warnings.append("Very little text was extracted for the document length.")
    if not tables:
        confidence -= 0.03
    return NormalizedPRDDocument(
        lines=output,
        tables=list(tables or []),
        confidence=max(0.0, min(1.0, confidence)),
        warnings=warnings,
    )


def _clean_page_lines(text: str, page: int) -> list[str]:
    lines: list[str] = []
    for raw in str(text or "").splitlines():
        line = " ".join(raw.replace("\u00a0", " ").split()).strip()
        if not line or PAGE_NUMBER_RE.fullmatch(line):
            continue
        lines.append(line)
    return lines


def _repeated_margin_lines(pages: list[list[str]]) -> set[str]:
    candidates: Counter[str] = Counter()
    for lines in pages:
        for line in [*lines[:2], *lines[-2:]]:
            if len(line) <= 100 and not NUMBERED_HEADING_RE.match(line):
                candidates[_key(line)] += 1
    threshold = max(3, len(pages) // 2)
    return {line for line, count in candidates.items() if line and count >= threshold}


def _join_wrapped_lines(lines: list[str]) -> list[str]:
    output: list[str] = []
    for line in lines:
        if not output:
            output.append(line)
            continue
        previous = output[-1]
        if previous.endswith("-") and line[:1].islower():
            output[-1] = previous[:-1] + line
        elif _continues_paragraph(previous, line):
            output[-1] = f"{previous} {line}"
        else:
            output.append(line)
    return output


def _continues_paragraph(previous: str, current: str) -> bool:
    if BULLET_RE.match(previous) or BULLET_RE.match(current):
        return False
    if NUMBERED_HEADING_RE.match(previous) or NUMBERED_HEADING_RE.match(current):
        return False
    if previous.endswith((".", ":", ";", "?", "!", "}", "]")):
        return False
    return bool(current[:1].islower())


def _key(text: str) -> str:
    return " ".join(text.lower().split())
