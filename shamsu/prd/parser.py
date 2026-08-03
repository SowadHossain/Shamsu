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
NUMBERED_HEADING_RE = re.compile(
    r"^(?P<number>\d+(?:\.\d+)*)\.?\s+(?P<title>[A-Z][^.!?]{1,100})$"
)
PRD_TITLE_RE = re.compile(
    r"^(?:prd|product requirements?(?: document)?)\s*[:\-]\s*(?P<title>.+)$",
    re.IGNORECASE,
)
LIST_MARKER_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")

TOP_LEVEL_NUMBERED_HEADINGS = {
    "overview",
    "goals",
    "non goals",
    "users and roles",
    "tech stack",
    "data model",
    "core features and user flows",
    "api surface",
    "permissions rules",
    "permissions and authorization",
    "frontend routes and screens",
    "seed data",
    "deliverables",
    "product overview",
    "product goals",
    "target users",
    "user roles",
    "main user journey",
    "functional requirements",
    "user registration",
    "user login",
    "logout",
    "authentication and authorization",
    "dashboard",
    "todo management",
    "view tasks",
    "update task",
    "complete and reopen tasks",
    "delete task",
    "search",
    "filtering",
    "sorting",
    "pagination",
    "category management",
    "user profile",
    "account deletion",
    "database requirements",
    "database schema",
    "backend api requirements",
    "authentication api",
    "user api",
    "task api",
    "category api",
    "dashboard statistics api",
    "frontend pages",
    "navigation",
    "user interface requirements",
    "loading and empty states",
    "error handling",
    "security requirements",
    "sqlite-specific requirements",
    "data validation rules",
    "date and time handling",
    "accessibility requirements",
    "responsive design requirements",
    "performance requirements",
    "audit and logging requirements",
    "business rules",
    "edge cases",
    "testing requirements",
    "acceptance criteria",
    "initial release scope",
    "out of scope for initial release",
    "future enhancements",
    "final product definition",
}


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
    line_pages: list[int] | None = None,
) -> ParsedPRD:
    title = fallback_title
    sections: dict[str, list[str]] = {}
    current_section = "Overview"
    source_refs: dict[str, list[dict[str, int | str]]] = {}
    last_top_level_number = 0
    in_code_fence = False

    for index, raw_line in enumerate(raw_text.splitlines()):
        line = raw_line.strip()
        page = line_pages[index] if line_pages and index < len(line_pages) else 0
        if not line:
            continue

        # A fenced block is code, not a requirement. Left in, a pasted SQL
        # schema or ASCII architecture diagram becomes a multi-thousand
        # character "feature" and the compiled plan tries to build it. The text
        # stays in `raw_text`, which is where schema extraction reads from.
        if markdown and FENCE_RE.match(line):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue

        prefixed_title = PRD_TITLE_RE.match(line)
        if prefixed_title and title == fallback_title and not sections:
            title = prefixed_title.group("title").strip()
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
                _add_source_ref(source_refs, current_section, page, "heading")
            continue

        numbered = _match_numbered_heading(line, last_top_level_number)
        if not markdown and numbered:
            if "." not in numbered.group("number"):
                last_top_level_number = int(numbered.group("number"))
            current_section = f"{numbered.group('number')} {numbered.group('title').strip()}"
            sections.setdefault(current_section, [])
            _add_source_ref(source_refs, current_section, page, "heading")
            continue

        if not markdown and _looks_like_plain_heading(line):
            if title == fallback_title and not sections:
                title = line.rstrip(":")
            else:
                current_section = line.rstrip(":")
                sections.setdefault(current_section, [])
                _add_source_ref(source_refs, current_section, page, "heading")
            continue

        if (
            not markdown
            and title == fallback_title
            and not sections
            and PLAIN_HEADING_RE.match(line)
            and line.lower() not in {"product requirements document", "product requirement document"}
        ):
            title = line.rstrip(":")
            continue

        cleaned = _clean_line(line)
        if cleaned:
            sections.setdefault(current_section, []).append(cleaned)
            _add_source_ref(source_refs, current_section, page, "content")

    return ParsedPRD(title=title, sections=sections, raw_text=raw_text, source_refs=source_refs)


def _looks_like_plain_heading(line: str) -> bool:
    if line.endswith("."):
        return False
    if len(line.split()) > 7:
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
        "product goals",
        "target users",
        "user roles",
        "functional requirements",
        "user registration",
        "user login",
        "authentication and authorization",
        "todo management",
        "category management",
        "database requirements",
        "database schema",
        "backend api requirements",
        "frontend pages",
        "security requirements",
        "testing requirements",
        "acceptance criteria",
        "initial release scope",
        "out of scope for initial release",
        "final product definition",
    }
    lowered = line.rstrip(":").lower()
    return lowered in known


def _match_numbered_heading(
    line: str,
    last_top_level_number: int,
) -> re.Match[str] | None:
    match = NUMBERED_HEADING_RE.match(line)
    if match is None:
        return None
    if "." in match.group("number"):
        return match
    raw_title = match.group("title").lower().replace("&", " and ")
    title = re.sub(r"[^a-z0-9]+", " ", raw_title).strip()
    known_heading = any(
        title == candidate or title.startswith(f"{candidate} ")
        for candidate in TOP_LEVEL_NUMBERED_HEADINGS
    )
    if known_heading and int(match.group("number")) > last_top_level_number:
        return match
    return None


def _add_source_ref(
    refs: dict[str, list[dict[str, int | str]]],
    section: str,
    page: int,
    kind: str,
) -> None:
    if page <= 0:
        return
    item = {"page": page, "kind": kind}
    if item not in refs.setdefault(section, []):
        refs[section].append(item)


def parse_markdown_prd(file_path: Path) -> ParsedPRD:
    return MarkdownPRDParser().parse(file_path)
