"""Reading the documents a repository carries but a text decoder cannot.

`file.read` decodes UTF-8 with replacement, which is right for source and wrong
for everything else. A `.docx` is a zip of XML: decoded as text it produces
several kilobytes of replacement characters, and a model handed that will
describe the document anyway — a live run reported what a PRD was "about"
having read nothing but noise.

Two rules.

**Extraction is parsing, not guessing** (invariant 8). A `.docx` paragraph is
`<w:p>` and its text is the `<w:t>` runs inside it; that is read with the
stdlib XML parser, not with a regex over the bytes.

**A format that cannot be extracted says so.** `.pdf` needs a dependency this
project does not have, and reporting "I could not read this, it is a PDF" is
worth more than a page of mojibake — it is the difference between the agent
knowing it is missing something and confidently inventing the contents.

No third-party dependency: `zipfile` and `xml.etree` are both stdlib.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

#: WordprocessingML. The one namespace needed to find paragraphs and runs.
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: Suffixes this module can turn into text. `.pdf` is here even though its
#: extractor is optional: the honest report for a missing extra is "PDF support
#: is not installed, run this", not "PDF files cannot be read", which is false.
EXTRACTABLE: frozenset[str] = frozenset({".docx", ".pdf"})

#: Suffixes that are definitely not text and that this module cannot read
#: either. Named so the refusal can say what the file *is*.
UNREADABLE: dict[str, str] = {
    ".doc": "legacy Word",
    ".xls": "legacy Excel",
    ".xlsx": "Excel",
    ".pptx": "PowerPoint",
    ".zip": "archive",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".ico": "image",
    ".mp4": "video",
    ".mp3": "audio",
    ".woff": "font",
    ".woff2": "font",
    ".ttf": "font",
    ".so": "binary",
    ".dll": "binary",
    ".dylib": "binary",
    ".exe": "binary",
    ".pyc": "compiled Python",
    ".sqlite": "database",
    ".db": "database",
}


class ExtractionFailed(Exception):
    """The file is a known document type but could not be read."""


@dataclass(frozen=True)
class Document:
    """Text pulled out of a non-plain-text file."""

    text: str
    kind: str
    paragraphs: int

    def render(self, path: str) -> str:
        header = f"{path} ({self.kind}, {self.paragraphs} paragraph(s), extracted text)"
        return f"{header}\n\n{self.text}"


def is_extractable(path: Path) -> bool:
    return path.suffix.lower() in EXTRACTABLE


def describe_unreadable(path: Path) -> str | None:
    """What this file is, when it is binary and cannot be extracted."""
    return UNREADABLE.get(path.suffix.lower())


def extract(path: Path) -> Document:
    """Pull the text out of a supported document.

    Raises:
        ExtractionFailed: the file is not a valid document of its type.
    """
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".pdf":
        return _extract_pdf(path)
    raise ExtractionFailed(f"no extractor for {suffix or 'this file'}")


def _extract_pdf(path: Path) -> Document:
    """Text from a PDF, page by page.

    `pypdf` is imported here rather than at module scope so the absence of an
    optional extra costs nothing until a PDF is actually opened — and so the
    failure names the fix instead of being an ImportError at startup.

    A PDF that yields no text is reported as such rather than as an empty
    document: scanned pages are images, and "this PDF has no extractable text,
    it is probably scanned" is a fact the agent can act on, where an empty
    string invites it to describe a document it never read.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ExtractionFailed(
            f"reading {path.name} needs PDF support, which is not installed. "
            "Install it with: pip install 'shamsu[documents]'"
        ) from exc

    try:
        reader = PdfReader(path)
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - pypdf raises a wide, unstable set
        raise ExtractionFailed(f"{path.name} could not be read as a PDF ({exc})") from exc

    lines = [line.rstrip() for page in pages for line in page.splitlines()]
    text = "\n".join(lines).strip()

    if not text:
        raise ExtractionFailed(
            f"{path.name} has {len(pages)} page(s) but no extractable text. "
            "It is most likely scanned images rather than text."
        )

    return Document(
        text=text,
        kind=f"PDF, {len(pages)} page(s)",
        paragraphs=sum(1 for line in lines if line.strip()),
    )


def _extract_docx(path: Path) -> Document:
    try:
        with zipfile.ZipFile(path) as archive:
            body = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ExtractionFailed(
            f"{path.name} is named .docx but is not a readable Word document ({exc})"
        ) from exc
    except OSError as exc:
        raise ExtractionFailed(f"could not open {path.name}: {exc}") from exc

    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise ExtractionFailed(f"{path.name}: its document.xml does not parse ({exc})") from exc

    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_WORD_NS}p"):
        # Runs are joined without separators: Word splits a single sentence
        # across several `<w:t>` whenever formatting changes mid-word, so
        # joining with spaces would insert them inside words.
        text = "".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t"))
        paragraphs.append(text.strip())

    # Runs of blank paragraphs collapse to one: Word documents are full of
    # empty spacing paragraphs, and a page of blank lines is budget spent on
    # nothing.
    lines: list[str] = []
    for line in paragraphs:
        if line or (lines and lines[-1]):
            lines.append(line)

    return Document(
        text="\n".join(lines).strip(),
        kind="Word document",
        paragraphs=sum(1 for line in lines if line),
    )


__all__ = [
    "EXTRACTABLE",
    "UNREADABLE",
    "Document",
    "ExtractionFailed",
    "describe_unreadable",
    "extract",
    "is_extractable",
]
