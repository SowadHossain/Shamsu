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
