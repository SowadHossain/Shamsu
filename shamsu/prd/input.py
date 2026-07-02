"""Unified PRD input parsing for Markdown, TXT, and PDF files."""
from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from shamsu.prd.parser import MarkdownPRDParser, parse_prd_text
from shamsu.types import ParsedPRD


class PRDParseError(Exception):
    """Raised when a PRD file cannot be converted into text sections."""


SUPPORTED_PRD_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf"}


class PRDInputParser:
    def parse(self, file_path: Path) -> ParsedPRD:
        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix in {".md", ".markdown"}:
            return MarkdownPRDParser().parse(path)
        if suffix == ".txt":
            raw_text = path.read_text(encoding="utf-8")
            return parse_prd_text(raw_text, fallback_title=path.stem)
        if suffix == ".pdf":
            return parse_prd_text(_extract_pdf_text(path), fallback_title=path.stem)
        supported = ", ".join(sorted(SUPPORTED_PRD_EXTENSIONS))
        raise PRDParseError(f"Unsupported PRD file type '{suffix}'. Supported: {supported}")


def _extract_pdf_text(path: Path) -> str:
    try:
        with pdfplumber.open(path) as pdf:
            page_text = [(page.extract_text() or "").strip() for page in pdf.pages]
    except Exception as exc:  # pdfplumber exposes backend-specific exceptions.
        raise PRDParseError(f"Could not read PDF PRD: {exc}") from exc

    raw_text = "\n\n".join(text for text in page_text if text)
    if not re.search(r"\w", raw_text):
        raise PRDParseError(
            "Could not extract text from PDF PRD. The file may be empty, encrypted, "
            "unreadable, or image-only."
        )
    return raw_text


def parse_prd_file(file_path: Path) -> ParsedPRD:
    return PRDInputParser().parse(file_path)
