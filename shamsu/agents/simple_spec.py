"""Turn a spec document into checkable requirements, without asking the model.

The Definition of Done only means anything if it says what the USER asked for.
Until now it said whatever the model remembered of what the user asked for, and
a small model remembers badly: live 2026-08-31 it was handed eight requirements
and wrote a contract with one assertion, which was the eight of them printed as
a Python list into a single text field.

So the extraction is DETERMINISTIC. Reading a document and listing what it asks
for is exactly the job a 3B does worst and a regex does perfectly well - the
document already says "must", already numbers its features, already puts them
under a heading called Requirements. Handing that to the model and hoping is how
five of eight requirements go missing without anyone noticing they are gone.

`shamsu/prd/` has a much larger version of this idea - entities, fields, SQL
schemas, a classifier - built around `PRDContract` and reachable only from the
legacy pipeline. This is deliberately not that. It answers one question, "what
did the document ask for", and produces one thing, a list of sentences that can
become contract assertions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Requirement", "extract_requirements", "read_spec"]

#: Headings whose contents are requirements rather than prose. Matched on the
#: heading TEXT, so `## 3. Functional Requirements` and `Acceptance Criteria:`
#: both count.
_REQUIREMENT_HEADINGS = re.compile(
    r"(?i)\b(requirement|feature|acceptance|criteria|must[- ]have|user stor|"
    r"functional|deliverable|scope|goal|objective)",
)

#: A heading, in any of the shapes a spec is written in.
_HEADING = re.compile(
    r"^\s{0,3}(?:#{1,6}\s+(?P<hash>.+?)|(?P<bold>\*\*.+?\*\*)|(?P<colon>[A-Z][^.!?]{2,60}:))\s*$"
)

#: A list item. The spec's own enumeration is the most reliable signal there is:
#: someone typed a bullet because they were listing things that must be true.
_LIST_ITEM = re.compile(r"^\s{0,8}(?:[-*+•]|\d{1,2}[.)])\s+(?P<text>\S.*)$")

#: An obligation stated in a sentence. Deliberately narrow - `will` and `can`
#: are not obligations, and including them turns half a design discussion into
#: requirements.
_OBLIGATION = re.compile(r"(?i)\b(must|shall|should|has to|needs? to|is required to)\b")

#: Lines that are structure or chatter rather than a requirement.
_NOISE = re.compile(
    r"(?i)^\s*(?:table of contents|overview|introduction|background|note[:s]?|"
    r"see (?:also|below|above)|tbd|n/?a|todo)\b"
)

#: Bounds. A spec with two hundred bullets is a document to read, not a contract
#: to satisfy in one turn, and a contract nobody can finish is the same failure
#: as a contract with nothing in it.
MAX_REQUIREMENTS = 24
MIN_CHARS = 12
MAX_CHARS = 200


@dataclass(frozen=True)
class Requirement:
    """One thing the document asks for."""

    text: str
    #: Which signal found it, for the tool result. A user looking at a contract
    #: they did not write needs to know where each line came from.
    source: str
    line: int


def read_spec(workspace: Path, filepath: str) -> tuple[str, str]:
    """`(text, error)` for a spec file, using the same extraction `read_file` does.

    `.docx` and `.pdf` go through `extract_document_text`, so a PRD in the format
    people actually write them in works without a conversion step.
    """
    target = (Path(workspace) / filepath).resolve()
    try:
        target.relative_to(Path(workspace).resolve())
    except ValueError:
        return ("", f"{filepath} is outside this workspace.")
    if not target.is_file():
        return ("", f"{filepath} is not a file in this workspace.")
    suffix = target.suffix.lower()
    try:
        from shamsu.tools.workspace import DOCUMENT_EXTENSIONS, extract_document_text

        if suffix in DOCUMENT_EXTENSIONS:
            return (extract_document_text(target), "")
        return (target.read_text(encoding="utf-8", errors="replace"), "")
    except Exception as exc:  # noqa: BLE001 - report, never raise into a turn
        return ("", f"Could not read {filepath}: {exc}")


def _clean(text: str) -> str:
    text = re.sub(r"^\*\*(.+?)\*\*:?\s*", r"\1: ", text.strip())
    text = re.sub(r"[`*_]", "", text)
    return " ".join(text.split()).rstrip(".;,")


def _usable(text: str) -> bool:
    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        return False
    if _NOISE.match(text):
        return False
    # A line with no verb is a label - "Frontend", "Phase 2" - and a label is
    # not something that can be true or false.
    return bool(re.search(r"[a-z]{3,}\s+[a-z]{2,}", text, re.IGNORECASE))


def extract_requirements(text: str) -> list[Requirement]:
    """Every requirement-shaped line in *text*, in document order.

    Three signals, strongest first, and a line is taken once:

    1. a list item under a heading that says these are requirements;
    2. any list item, anywhere - someone enumerated it for a reason;
    3. a sentence stating an obligation.

    Ordered by how much the DOCUMENT is telling us. A bullet under
    "Acceptance Criteria" is unambiguous; a `must` in a paragraph is a good
    guess. Recording which one found it means the user can see the difference
    without reading the source.
    """
    found: list[Requirement] = []
    seen: set[str] = set()
    in_requirements = False

    for number, raw in enumerate(text.splitlines(), start=1):
        heading = _HEADING.match(raw)
        if heading:
            title = next(
                (g for g in heading.groupdict().values() if g), ""
            )
            in_requirements = bool(_REQUIREMENT_HEADINGS.search(title))
            continue

        item = _LIST_ITEM.match(raw)
        candidate = _clean(item.group("text")) if item else _clean(raw)
        if not _usable(candidate):
            continue
        key = candidate.lower()
        if key in seen:
            continue

        if item and in_requirements:
            source = "listed under a requirements heading"
        elif item:
            source = "a list item"
        elif _OBLIGATION.search(candidate):
            source = "states an obligation"
        else:
            continue

        seen.add(key)
        found.append(Requirement(text=candidate, source=source, line=number))
        if len(found) >= MAX_REQUIREMENTS:
            break
    return found
