"""Unified PRD input parsing for Markdown, TXT, and PDF files."""
from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
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

_DOCUMENT_SUFFIX_PATTERN = "|".join(
    re.escape(extension.lstrip("."))
    for extension in sorted(SUPPORTED_PRD_EXTENSIONS, key=len, reverse=True)
)
_QUOTED_DOCUMENT_REFERENCE_RE = re.compile(
    rf"@?(?P<quote>[`'\"])(?P<path>.+?\.(?:{_DOCUMENT_SUFFIX_PATTERN}))(?P=quote)",
    re.IGNORECASE,
)
_BARE_DOCUMENT_REFERENCE_RE = re.compile(
    rf"(?<![\w.])@?(?P<path>(?:[A-Za-z]:)?[\w./\\-]+?\.(?:{_DOCUMENT_SUFFIX_PATTERN}))\b",
    re.IGNORECASE,
)

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


def extract_document_reference(text: str) -> str:
    """Return the first explicit supported document path in user text.

    Quoting is required when a path contains spaces. Backticks are accepted
    alongside shell-style quotes because users commonly paste Markdown paths
    into prompts. An optional ``@`` prefix is syntax, not part of the path.
    """
    quoted = _QUOTED_DOCUMENT_REFERENCE_RE.search(text)
    if quoted:
        return quoted.group("path").strip()
    bare = _BARE_DOCUMENT_REFERENCE_RE.search(text)
    return bare.group("path") if bare else ""


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
            parsed.title = _product_name(parsed) or _document_title(document.text) or parsed.title
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

    ocr_pages = [index for index, text in enumerate(page_text, start=1) if _page_needs_ocr(text)]
    ocr_error = ""
    if ocr_pages:
        try:
            ocr_text = _ocr_pdf_pages(path, ocr_pages)
        except Exception as exc:
            ocr_error = str(exc)
        else:
            for page_number, text in zip(ocr_pages, ocr_text, strict=False):
                if re.search(r"\w", text):
                    page_text[page_number - 1] = text.strip()

    raw_text = "\n\n".join(text for text in page_text if text)
    if not re.search(r"\w", raw_text):
        detail = f" OCR fallback failed: {ocr_error}" if ocr_error else " OCR found no readable text."
        raise PRDParseError(
            "Could not extract text from PDF PRD. The file may be empty, encrypted, "
            f"or unreadable.{detail}"
        )
    document = normalize_pdf_pages(page_text, tables)
    recovered_pages = [page for page in ocr_pages if re.search(r"\w", page_text[page - 1])]
    if recovered_pages:
        pages = ", ".join(str(page) for page in recovered_pages)
        document.warnings.append(f"OCR fallback supplied text for PDF page(s): {pages}.")
        document.confidence = min(document.confidence, 0.82)
    if ocr_error:
        document.warnings.append(f"OCR fallback was unavailable for weak PDF pages: {ocr_error}")
        document.confidence = min(document.confidence, 0.65)
    return document


def _page_needs_ocr(text: str) -> bool:
    return len(re.findall(r"\w", str(text or ""), re.UNICODE)) < 8


@lru_cache(maxsize=1)
def _ocr_engine():
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs.
        raise RuntimeError(
            "local OCR dependencies are missing; install rapidocr, onnxruntime, and pypdfium2"
        ) from exc
    return RapidOCR(params={"Global.log_level": "warning"})


def _ocr_pdf_pages(path: Path, page_numbers: list[int]) -> list[str]:
    """Render and OCR selected one-based PDF pages locally."""
    cached = _read_ocr_cache(path, page_numbers)
    if cached is not None:
        return cached
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs.
        raise RuntimeError("local PDF rendering dependency pypdfium2 is missing") from exc

    engine = _ocr_engine()
    document = pdfium.PdfDocument(path)
    output: list[str] = []
    try:
        for page_number in page_numbers:
            if page_number < 1 or page_number > len(document):
                output.append("")
                continue
            page = document[page_number - 1]
            bitmap = page.render(scale=2.5)
            try:
                result = engine(bitmap.to_pil())
                texts = list(getattr(result, "txts", None) or [])
                output.append("\n".join(str(text).strip() for text in texts if str(text).strip()))
            finally:
                bitmap.close()
                page.close()
    finally:
        document.close()
    _write_ocr_cache(path, page_numbers, output)
    return output


def _ocr_cache_path(path: Path, page_numbers: list[int]) -> Path:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    page_key = "-".join(str(page) for page in page_numbers)
    configured = os.environ.get("SHAMSU_OCR_CACHE_DIR", "").strip()
    root = Path(configured).expanduser() if configured else Path.home() / ".shamsu" / "cache" / "ocr"
    return root / f"{digest.hexdigest()}-{page_key}.json"


def _read_ocr_cache(path: Path, page_numbers: list[int]) -> list[str] | None:
    try:
        payload = json.loads(_ocr_cache_path(path, page_numbers).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, list) or len(text) != len(page_numbers):
        return None
    return [str(item) for item in text]


def _write_ocr_cache(path: Path, page_numbers: list[int], text: list[str]) -> None:
    try:
        target = _ocr_cache_path(path, page_numbers)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)
    except OSError:
        return


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


def _document_title(text: str) -> str:
    for line in str(text or "").splitlines()[:8]:
        match = re.match(
            r"^(?:prd|product requirements?(?: document)?)\s*[:\-]\s*(?P<title>.+)$",
            line.strip(),
            re.IGNORECASE,
        )
        if match:
            return match.group("title").strip()
    return ""


def parse_prd_file(file_path: Path) -> ParsedPRD:
    return PRDInputParser().parse(file_path)
