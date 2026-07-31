"""Unified PRD input parsing for Markdown, TXT, and PDF files."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from shamsu.types import ParsedPRD


class _LazyPdfPlumber:
    def open(self, *args: Any, **kwargs: Any):
        import pdfplumber as module

        return module.open(*args, **kwargs)


pdfplumber = _LazyPdfPlumber()


class PRDParseError(Exception):
    """Raised when a PRD file cannot be converted into text sections."""


SUPPORTED_PRD_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf"}

# Phrases (beyond the bare "prd" acronym) that mark a file as a PRD by name.
# "prd" itself is matched as a plain substring — it is a rare letter sequence
# in real words, so this keeps names like `myprd.md` while not matching
# ordinary words (e.g. "upward" has no "prd" substring).
_PRD_NAME_PHRASES = (
    "prd",
    "product requirements",
    "product requirement",
    "requirements document",
)


def is_prd_filename(name: str) -> bool:
    """True if a filename looks like a PRD (by extension + name heuristics).

    Recognizes both the `prd` acronym and spelled-out names like
    `Product Requirements Document.pdf` that contain no literal "prd".
    """
    lowered = name.lower()
    if Path(lowered).suffix not in SUPPORTED_PRD_EXTENSIONS:
        return False
    stem = Path(lowered).stem
    return any(phrase in stem for phrase in _PRD_NAME_PHRASES)


class PRDInputParser:
    def parse(self, file_path: Path) -> ParsedPRD:
        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix in {".md", ".markdown"}:
            from shamsu.prd.parser import MarkdownPRDParser

            parsed = MarkdownPRDParser().parse(path)
            parsed.source_path = str(path.resolve())
            parsed.source_kind = "markdown"
            return parsed
        if suffix == ".txt":
            from shamsu.prd.parser import parse_prd_text

            raw_text = path.read_text(encoding="utf-8")
            parsed = parse_prd_text(raw_text, fallback_title=path.stem)
            parsed.source_path = str(path.resolve())
            parsed.source_kind = "text"
            return parsed
        if suffix == ".pdf":
            from shamsu.prd.parser import parse_prd_text

            document = _extract_pdf_document(path)
            parsed = parse_prd_text(
                document.text,
                fallback_title=path.stem,
                line_pages=document.line_pages,
            )
            parsed.source_path = str(path.resolve())
            parsed.source_kind = "pdf"
            parsed.tables = document.tables
            parsed.extraction_confidence = document.confidence
            parsed.extraction_warnings = document.warnings
            parsed.title = _product_name(parsed) or parsed.title
            return parsed
        supported = ", ".join(sorted(SUPPORTED_PRD_EXTENSIONS))
        raise PRDParseError(f"Unsupported PRD file type '{suffix}'. Supported: {supported}")


def _extract_pdf_document(path: Path):
    from shamsu.prd.document import normalize_pdf_pages

    try:
        with pdfplumber.open(path) as pdf:
            page_text = [(page.extract_text() or "").strip() for page in pdf.pages]
            tables = _extract_pdf_tables(pdf.pages)
    except Exception as exc:  # pdfplumber exposes backend-specific exceptions.
        raise PRDParseError(f"Could not read PDF PRD: {exc}") from exc

    raw_text = "\n\n".join(text for text in page_text if text)
    if not re.search(r"\w", raw_text):
        raise PRDParseError(
            "Could not extract text from PDF PRD. The file may be empty, encrypted, "
            "unreadable, or image-only."
        )
    return normalize_pdf_pages(page_text, tables)


def _extract_pdf_text(path: Path) -> str:
    """Backward-compatible normalized text helper."""
    return _extract_pdf_document(path).text


def _extract_pdf_tables(pages) -> list[dict]:
    tables: list[dict] = []
    for page_number, page in enumerate(pages, start=1):
        extract_tables = getattr(page, "extract_tables", None)
        for index, rows in enumerate(extract_tables() if extract_tables else [], start=1):
            clean_rows = [
                [" ".join(str(cell or "").split()) for cell in row]
                for row in rows or []
                if any(str(cell or "").strip() for cell in row)
            ]
            if clean_rows:
                tables.append({"page": page_number, "table": index, "rows": clean_rows})
    return tables


def _product_name(parsed: ParsedPRD) -> str:
    for heading, lines in parsed.sections.items():
        if "product name" not in heading.lower():
            continue
        for line in lines:
            candidate = str(line).strip()
            if candidate:
                return candidate
    return ""


def parse_prd_file(file_path: Path) -> ParsedPRD:
    return PRDInputParser().parse(file_path)
