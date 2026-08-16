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

Every Office `x` format is the same trick: a zip of XML parts. `.docx`, `.xlsx`
and `.pptx` therefore cost one extractor each and no dependency at all — only
their pre-2007 binary ancestors (`.doc`, `.xls`) are genuinely out of reach.
`.pdf` is the single exception, and its extractor is imported lazily so a
repository holding no PDFs pays nothing for the capability.

Plain text needs nothing here. `file.read` decodes anything that is not a known
binary, so source in any language — `.rs`, `.go`, `.zig`, `.cbl`, a `Dockerfile`
with no extension at all — is read without this module being consulted. There
is no language allowlist to fall off.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

#: WordprocessingML. The one namespace needed to find paragraphs and runs.
_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: Longest bold paragraph still treated as a heading. Beyond this it is a bold
#: sentence — emphasis, not structure — and promoting it would invent headings
#: rather than recover them.
_BOLD_HEADING_LIMIT = 120

#: Suffixes this module can turn into text. `.pdf` is here even though its
#: extractor is optional: the honest report for a missing extra is "PDF support
#: is not installed, run this", not "PDF files cannot be read", which is false.
EXTRACTABLE: frozenset[str] = frozenset({".docx", ".pdf", ".xlsx", ".pptx"})

#: Suffixes that are definitely not text and that this module cannot read
#: either. Named so the refusal can say what the file *is*.
#:
#: `.doc` and `.xls` are the pre-2007 binary formats — genuinely undecodable
#: without a third-party library, unlike their `x` successors, which are zips
#: of XML and need nothing but the stdlib.
UNREADABLE: dict[str, str] = {
    ".doc": "legacy Word",
    ".xls": "legacy Excel",
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
    if suffix == ".xlsx":
        return _extract_xlsx(path)
    if suffix == ".pptx":
        return _extract_pptx(path)
    raise ExtractionFailed(f"no extractor for {suffix or 'this file'}")


def _open_ooxml(path: Path, kind: str) -> zipfile.ZipFile:
    """Open an Office file as the zip archive it is.

    Shared by every `x` format, which differ only in which parts they hold.
    A file named `.xlsx` that is not a zip is almost always something renamed,
    and saying so beats a `BadZipFile` traceback.
    """
    try:
        return zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ExtractionFailed(
            f"{path.name} is named {path.suffix} but is not a readable {kind} file ({exc})"
        ) from exc
    except OSError as exc:
        raise ExtractionFailed(f"could not open {path.name}: {exc}") from exc


def _text_of(blob: bytes, tag: str) -> list[str]:
    """Every `<tag>` string in an OOXML part, in document order.

    Sibling runs are joined by the caller, not here: Office splits one sentence
    across several runs whenever formatting changes mid-word, so what counts as
    a break depends on the format's own grouping element.
    """
    try:
        root = ElementTree.fromstring(blob)
    except ElementTree.ParseError:
        return []
    return [node.text or "" for node in root.iter(tag)]


def _extract_xlsx(path: Path) -> Document:
    """Cell text from every sheet, one row per line, tab-separated.

    Excel stores repeated strings once in `sharedStrings.xml` and refers to
    them by index, so a cell reading `<v>3</v>` with `t="s"` is the *fourth
    shared string*, not the number three. Resolving that is the whole job;
    without it a spreadsheet extracts as a column of meaningless integers.

    Rows rather than a flat dump, because a table's meaning is positional — a
    requirements matrix read column-first says something else entirely.
    """
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

    with _open_ooxml(path, "Excel") as archive:
        names = archive.namelist()
        shared = (
            _text_of(archive.read("xl/sharedStrings.xml"), f"{ns}t")
            if "xl/sharedStrings.xml" in names
            else []
        )
        sheets = sorted(
            n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
        )
        if not sheets:
            raise ExtractionFailed(f"{path.name} contains no worksheets")

        lines: list[str] = []
        for sheet in sheets:
            try:
                root = ElementTree.fromstring(archive.read(sheet))
            except ElementTree.ParseError:
                continue
            if len(sheets) > 1:
                lines.append(f"--- {sheet.rsplit('/', 1)[-1].removesuffix('.xml')} ---")
            for row in root.iter(f"{ns}row"):
                cells: list[str] = []
                for cell in row.iter(f"{ns}c"):
                    value = cell.find(f"{ns}v")
                    inline = cell.find(f"{ns}is")
                    if inline is not None:
                        cells.append("".join(n.text or "" for n in inline.iter(f"{ns}t")))
                    elif value is None or value.text is None:
                        cells.append("")
                    elif cell.get("t") == "s":
                        index = int(value.text)
                        cells.append(shared[index] if 0 <= index < len(shared) else "")
                    else:
                        cells.append(value.text)
                lines.append("\t".join(cells).rstrip())

    text = "\n".join(lines).strip()
    if not text:
        raise ExtractionFailed(f"{path.name} has no cell contents to read")

    return Document(
        text=text,
        kind=f"Excel workbook, {len(sheets)} sheet(s)",
        paragraphs=sum(1 for line in lines if line.strip()),
    )


def _extract_pptx(path: Path) -> Document:
    """Slide text, in slide order, each slide headed by its number.

    `slide10.xml` sorts before `slide2.xml` as a string, which would silently
    reorder a deck, so the numeric suffix is what orders them.
    """
    ns = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

    def number(name: str) -> int:
        digits = "".join(c for c in name.rsplit("/", 1)[-1] if c.isdigit())
        return int(digits) if digits else 0

    with _open_ooxml(path, "PowerPoint") as archive:
        slides = sorted(
            (
                n
                for n in archive.namelist()
                if n.startswith("ppt/slides/slide") and n.endswith(".xml")
            ),
            key=number,
        )
        if not slides:
            raise ExtractionFailed(f"{path.name} contains no slides")

        lines: list[str] = []
        for slide in slides:
            body = [t for t in _text_of(archive.read(slide), f"{ns}t") if t.strip()]
            if not body:
                continue
            lines.append(f"--- slide {number(slide)} ---")
            lines.extend(body)

    text = "\n".join(lines).strip()
    if not text:
        raise ExtractionFailed(
            f"{path.name} has {len(slides)} slide(s) but no text. "
            "Its content is most likely images rather than text boxes."
        )

    return Document(
        text=text,
        kind=f"PowerPoint deck, {len(slides)} slide(s)",
        paragraphs=sum(1 for line in lines if not line.startswith("--- slide")),
    )


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


def _as_markdown(paragraph: ElementTree.Element, text: str) -> str:
    """One Word paragraph as the markdown it was always meant to be.

    Extraction used to emit bare text, and a `.docx` PRD then arrived as an
    undifferentiated wall of lines: "Features" looked exactly like the feature
    under it. Structure is the most valuable thing in a specification and it was
    being thrown away at the door — the same document saved as `.md` planned a
    real project, because `## Features` was visible as a heading.

    Markdown rather than a bespoke shape because it is what the model has read
    most of, and because it survives every later hop — a heading stays a heading
    through the context compiler, the frame, and the prompt.

    Three signals, in the order Word makes them available:

    **`pStyle` naming a heading level** is the reliable one, and `Heading2`
    becomes `##` so nesting is preserved rather than flattened.

    **`numPr`** means the paragraph is in a numbered or bulleted list; both
    become `-`, since which one it was is presentation and the *itemisation* is
    the content.

    **A fully bold paragraph** is the fallback, and the one that matters most in
    practice: authors write specification headings by bolding a line far more
    often than by applying a style. v1 learned this the expensive way — its
    extractor missed those headings and produced plans that built nothing.
    """
    properties = paragraph.find(f"{_WORD_NS}pPr")

    if properties is not None:
        style = properties.find(f"{_WORD_NS}pStyle")
        name = (style.get(f"{_WORD_NS}val") or "") if style is not None else ""
        if name.lower().startswith("heading"):
            level = "".join(c for c in name if c.isdigit())
            return f"{'#' * min(int(level) if level else 1, 6)} {text}"
        if name.lower() in {"title", "subtitle"}:
            return f"# {text}" if name.lower() == "title" else f"## {text}"
        if properties.find(f"{_WORD_NS}numPr") is not None:
            return f"- {text}"

    runs = paragraph.findall(f"{_WORD_NS}r")
    if runs and all(_is_bold(run) for run in runs) and len(text) < _BOLD_HEADING_LIMIT:
        # Length-capped: a whole bold *paragraph* is emphasis, not a heading,
        # and promoting it would invent structure rather than recover it.
        return f"## {text}"

    return text


def _is_bold(run: ElementTree.Element) -> bool:
    """Whether a run carries bold, and any text at all to carry it."""
    if not any((node.text or "").strip() for node in run.iter(f"{_WORD_NS}t")):
        return True  # an empty run neither confirms nor denies
    properties = run.find(f"{_WORD_NS}rPr")
    if properties is None:
        return False
    bold = properties.find(f"{_WORD_NS}b")
    return bold is not None and bold.get(f"{_WORD_NS}val") not in {"0", "false"}


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
        text = "".join(node.text or "" for node in paragraph.iter(f"{_WORD_NS}t")).strip()
        paragraphs.append(_as_markdown(paragraph, text) if text else "")

    # Runs of blank paragraphs collapse to one: Word documents are full of
    # empty spacing paragraphs, and a page of blank lines is budget spent on
    # nothing.
    lines: list[str] = []
    for line in paragraphs:
        if line or (lines and lines[-1]):
            lines.append(line)

    return Document(
        text="\n".join(lines).strip(),
        kind="Word document, converted to markdown",
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
