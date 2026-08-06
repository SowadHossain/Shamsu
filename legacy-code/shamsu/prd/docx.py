"""DOCX PRD extraction.

Word is where most PRDs are actually written, but ``.docx`` was not a
supported PRD input at all: :class:`~shamsu.prd.input.PRDInputParser` raised
``PRDParseError`` and ``read_file`` refused the path, so a user whose PRD was a
Word document had nothing to point SHAMSU at.

Converting the file is not enough on its own. Real Word documents almost never
use Word's ``Heading`` styles - the OpenBazaar PRD that motivated this module
styles all fifteen of its section titles as **bold body paragraphs**. Every
converter (pandoc included) therefore emits bold runs rather than headings, and
because ``parse_prd_text`` keys sections off headings, the whole document
collapsed into a single ``Overview`` section: zero entities, forty junk
"features", and a plan that built nothing.

So this module reconstructs the structure Word threw away:

* paragraphs styled ``Heading N`` become ``#``-headings at that depth;
* **bold-only paragraphs** that read like titles become headings too, at a
  depth taken from their own numbering ("5.2 Database Schema" -> ``###``);
* list paragraphs keep their bullet;
* tables become GFM tables, and are also published on
  :attr:`~shamsu.types.ParsedPRD.tables`;
* SQL DDL and ASCII architecture diagrams are fenced, so they stop being lexed
  as prose requirements. The text survives in ``raw_text``, which is where
  :mod:`shamsu.prd.sql_schema` reads the schema from.

Parsing is done against the raw OOXML with the standard library. python-docx
would be a new runtime dependency for one file format whose relevant subset is
a handful of elements.
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DC = "{http://purl.org/dc/elements/1.1/}"

_DOCUMENT_PART = "word/document.xml"
_CORE_PROPERTIES_PART = "docProps/core.xml"

# Deepest heading ``parse_prd_text`` recognises (its HEADING_RE caps at ###).
_MAX_HEADING_LEVEL = 3

# A bold paragraph this long is emphasised prose, not a section title.
_MAX_HEADING_CHARS = 120
_MAX_HEADING_WORDS = 14

# Title-page boilerplate: a real document title, if there is one, comes after.
_GENERIC_TITLE_LINES = frozenset(
    {
        "prd",
        "product requirement document",
        "product requirements document",
        "product requirement document (prd)",
        "product requirements document (prd)",
    }
)

_NUMBER_PREFIX_RE = re.compile(r"^(?P<number>\d+(?:\.\d+)*)[.)]?\s+\S")
_HEADING_STYLE_RE = re.compile(r"^heading\s*(?P<level>\d+)$", re.IGNORECASE)
_CREATE_TABLE_RE = re.compile(r"\bcreate\s+table\b", re.IGNORECASE)


class DocxParseError(Exception):
    """Raised when a .docx cannot be opened or contains no document part."""


@dataclass
class DocxDocument:
    """Markdown reconstruction of a Word document."""

    text: str = ""
    title: str = ""
    tables: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    heading_count: int = 0


def extract_docx_document(path: Path) -> DocxDocument:
    """Read *path* and rebuild it as Markdown."""
    try:
        with zipfile.ZipFile(path) as archive:
            try:
                document_xml = archive.read(_DOCUMENT_PART)
            except KeyError as exc:
                raise DocxParseError(
                    f"'{Path(path).name}' is not a Word document: no {_DOCUMENT_PART} part."
                ) from exc
            core_xml = _read_optional(archive, _CORE_PROPERTIES_PART)
    except zipfile.BadZipFile as exc:
        raise DocxParseError(f"Could not read DOCX PRD: {exc}") from exc
    except OSError as exc:
        raise DocxParseError(f"Could not open DOCX PRD: {exc}") from exc

    try:
        body = ElementTree.fromstring(document_xml).find(f"{_W}body")
    except ElementTree.ParseError as exc:
        raise DocxParseError(f"Could not parse DOCX PRD: {exc}") from exc
    if body is None:
        raise DocxParseError("DOCX PRD contains no document body.")

    document = DocxDocument(title=_core_title(core_xml))
    lines: list[str] = []
    title_claimed = bool(document.title)

    for child in body:
        if child.tag == f"{_W}p":
            text = _paragraph_text(child)
            if not text:
                continue
            if not title_claimed and len(lines) < 5:
                candidate = _title_candidate(text)
                if candidate:
                    document.title = candidate
                    title_claimed = True
                    continue
            lines.extend(_render_paragraph(child, text, document))
        elif child.tag == f"{_W}tbl":
            rows = _table_rows(child)
            if rows:
                document.tables.append({"page": 0, "table": len(document.tables) + 1, "rows": rows})
                lines.extend(_render_table(rows))

    if document.title:
        lines.insert(0, f"# {document.title}")
    if not document.heading_count:
        document.warnings.append(
            "No headings were found in the DOCX; it will be read as one section. "
            "Style the section titles as headings or bold text."
        )

    document.text = "\n\n".join(lines)
    if not re.search(r"\w", document.text):
        raise DocxParseError(
            "Could not extract text from DOCX PRD. The file may be empty or contain only images."
        )
    return document


def _read_optional(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        return archive.read(name)
    except KeyError:
        return b""


def _core_title(core_xml: bytes) -> str:
    if not core_xml:
        return ""
    try:
        root = ElementTree.fromstring(core_xml)
    except ElementTree.ParseError:
        return ""
    node = root.find(f"{_DC}title")
    text = (node.text or "").strip() if node is not None else ""
    return "" if _is_generic_title(text) else text


def _is_generic_title(text: str) -> bool:
    return not text or text.strip().lower().rstrip(":") in _GENERIC_TITLE_LINES


def _title_candidate(text: str) -> str:
    """The document's own title, if this opening paragraph looks like one."""
    if _is_generic_title(text):
        return ""
    if len(text) > _MAX_HEADING_CHARS or text.endswith((".", "!", "?")):
        return ""
    return text


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    """Visible text of a paragraph, including hyperlink runs, tabs and breaks."""
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag == f"{_W}tab":
            parts.append(" ")
        elif node.tag in {f"{_W}br", f"{_W}cr"}:
            parts.append("\n")
    return " ".join("".join(parts).split())


def _style_name(paragraph: ElementTree.Element) -> str:
    properties = paragraph.find(f"{_W}pPr")
    if properties is None:
        return ""
    style = properties.find(f"{_W}pStyle")
    return (style.get(f"{_W}val") or "").strip() if style is not None else ""


def _is_list_paragraph(paragraph: ElementTree.Element) -> bool:
    properties = paragraph.find(f"{_W}pPr")
    if properties is not None and properties.find(f"{_W}numPr") is not None:
        return True
    return "list" in _style_name(paragraph).lower()


def _toggle_is_on(node: ElementTree.Element | None) -> bool:
    """A Word toggle property (``w:b``) is on unless it says otherwise."""
    if node is None:
        return False
    return (node.get(f"{_W}val") or "true").strip().lower() not in {"0", "false", "off"}


def _is_bold_paragraph(paragraph: ElementTree.Element) -> bool:
    """True when every run carrying text in this paragraph is bold."""
    properties = paragraph.find(f"{_W}pPr")
    if properties is not None:
        run_defaults = properties.find(f"{_W}rPr")
        if run_defaults is not None and _toggle_is_on(run_defaults.find(f"{_W}b")):
            return True

    saw_text = False
    for run in paragraph.iter(f"{_W}r"):
        if not any((node.text or "").strip() for node in run.iter(f"{_W}t")):
            continue
        saw_text = True
        run_properties = run.find(f"{_W}rPr")
        if run_properties is None or not _toggle_is_on(run_properties.find(f"{_W}b")):
            return False
    return saw_text


def _heading_level(paragraph: ElementTree.Element, text: str) -> int:
    """Heading depth for *paragraph*, or 0 when it is body text.

    Word's own ``Heading N`` style wins. Failing that a bold, short, non-terminal
    paragraph is treated as a section title - the shape every PRD in the wild
    uses - and its depth comes from its own numbering, so "5. Technical
    Architecture" nests "5.2 Database Schema" underneath it.
    """
    styled = _HEADING_STYLE_RE.match(_style_name(paragraph))
    if styled:
        return min(int(styled.group("level")) + 1, _MAX_HEADING_LEVEL)

    if _is_list_paragraph(paragraph):
        return 0
    if not _is_bold_paragraph(paragraph):
        return 0
    if len(text) > _MAX_HEADING_CHARS or len(text.split()) > _MAX_HEADING_WORDS:
        return 0
    if text.endswith((".", "!", "?", ",", ";")):
        return 0

    numbering = _NUMBER_PREFIX_RE.match(text)
    if numbering:
        return min(2 + numbering.group("number").count("."), _MAX_HEADING_LEVEL)
    return 2


def _looks_like_code_or_diagram(text: str) -> tuple[bool, str]:
    """Whether *text* is a code block or ASCII diagram, and its fence language.

    Word stores a pasted SQL schema or box diagram as one very long paragraph.
    Left as prose it becomes a "requirement" - the OpenBazaar DDL was extracted
    as a single 3,000-character feature - so it is fenced instead.
    """
    if _CREATE_TABLE_RE.search(text):
        return True, "sql"
    if text.count("+--") >= 2 or (text.count("|") >= 6 and "+" in text):
        return True, "text"
    return False, ""


def _render_paragraph(
    paragraph: ElementTree.Element, text: str, document: DocxDocument
) -> list[str]:
    level = _heading_level(paragraph, text)
    if level:
        document.heading_count += 1
        return [f"{'#' * level} {text}"]

    is_code, language = _looks_like_code_or_diagram(text)
    if is_code:
        return [f"```{language}\n{text}\n```"]

    if _is_list_paragraph(paragraph):
        return [f"- {text}"]
    return [text]


def _table_rows(table: ElementTree.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.findall(f"{_W}tr"):
        cells = [
            " ".join(
                _paragraph_text(paragraph)
                for paragraph in cell.findall(f"{_W}p")
            ).strip()
            for cell in row.findall(f"{_W}tc")
        ]
        if any(cell for cell in cells):
            rows.append(cells)
    return rows


def _render_table(rows: list[list[str]]) -> list[str]:
    width = max(len(row) for row in rows)
    rendered = [
        "| " + " | ".join(_escape_cell(row[index]) if index < len(row) else "" for index in range(width)) + " |"
        for row in rows
    ]
    rendered.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return ["\n".join(rendered)]


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|")
