"""
Markdown PRD parser.

The parser keeps the contract deliberately simple: H1 becomes the title, H2/H3
headings become section keys, and non-empty paragraph/list lines become section
items. mistletoe is used to validate that the document is parseable Markdown;
the line walk preserves the original prose without lossy renderer behavior.
"""
from __future__ import annotations

import re
from pathlib import Path

from mistletoe import Document

from shamsu.interfaces import IPRDParser
from shamsu.types import ParsedPRD

HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
PLAIN_HEADING_RE = re.compile(
    r"^(?P<title>[A-Z][A-Za-z0-9 /&_-]{2,60})(?::)?$"
)
LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _clean_line(line: str) -> str:
    return LIST_MARKER_RE.sub("", line.strip()).strip()


class MarkdownPRDParser(IPRDParser):
    def parse(self, file_path: Path) -> ParsedPRD:
        path = Path(file_path)
        raw_text = path.read_text(encoding="utf-8")
        Document(raw_text.splitlines())
        return parse_prd_text(raw_text, fallback_title=path.stem, markdown=True)


def parse_prd_text(
    raw_text: str,
    fallback_title: str = "PRD",
    markdown: bool = False,
) -> ParsedPRD:
    title = fallback_title
    sections: dict[str, list[str]] = {}
    current_section = "Overview"

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            heading_text = heading.group(2).strip().strip("#").strip()
            if level == 1 and title == fallback_title:
                title = heading_text
            else:
                current_section = heading_text
                sections.setdefault(current_section, [])
            continue

        if not markdown and _looks_like_plain_heading(line):
            if title == fallback_title and not sections:
                title = line.rstrip(":")
            else:
                current_section = line.rstrip(":")
                sections.setdefault(current_section, [])
            continue

        cleaned = _clean_line(line)
        if cleaned:
            sections.setdefault(current_section, []).append(cleaned)

    return ParsedPRD(title=title, sections=sections, raw_text=raw_text)


def _looks_like_plain_heading(line: str) -> bool:
    if line.endswith("."):
        return False
    if len(line.split()) > 8:
        return False
    known = {
        "overview",
        "entities",
        "data model",
        "data models",
        "api",
        "api endpoints",
        "endpoints",
        "pages",
        "screens",
        "features",
        "requirements",
        "non functional requirements",
    }
    lowered = line.rstrip(":").lower()
    return lowered in known or bool(PLAIN_HEADING_RE.match(line))


def parse_markdown_prd(file_path: Path) -> ParsedPRD:
    return MarkdownPRDParser().parse(file_path)
