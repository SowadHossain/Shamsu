"""Structural facts about the code a step is about, put in the frame.

The prime directive says *deterministic retrieval before inference*. In
practice retrieval was the model choosing grep terms: `code.search` is text
search, and `PythonCodeIndex` — 425 lines with a symbol table, a call graph and
its own accuracy eval — was imported by nothing outside its tests.
`FrameInputs.source_excerpts` was defined, rendered by the compiler, and never
populated by anything.

What that cost is measurable in turns. A change step has eight actions and
needs four for the work itself, so the "where is this code?" turn comes out of
the margin for error — and the §31.1 failures are full of runs that spent two
actions locating a function and had nothing left when the first patch missed.

So the runtime answers the question it can answer deterministically, before the
first decision:

* **For each file the step names**, a table of contents: every symbol with its
  line range. The model learns the shape of the file without spending a turn on
  it, and an anchor drawn from a real line range is not a guess.
* **For each identifier the step mentions**, where it is *defined*. This is the
  case grep is worst at — a symbol re-exported through a package `__init__`
  looks identical at three call sites and is defined at one.

**This does not credit a read.** `file.patch` in `replace_text` mode still
requires the file to have been read through the gateway, because a table of
contents is not the file's text and an anchor composed from it would still be
invented. What this removes is the *search*, not the read.

Everything here degrades to silence. An unparseable repository, a missing file,
a symbol that does not resolve — each yields no section rather than an error,
because a grounding block is an optimisation and must never be able to fail a
run that would otherwise have worked.
"""

from __future__ import annotations

import keyword
import re
from pathlib import Path

from shamsu.code_intelligence.index import PythonCodeIndex
from shamsu.interfaces.code_intelligence import SymbolRef
from shamsu.security.paths import workspace_key
from shamsu.state.records import PlanStepRecord

#: Symbols listed per file. A long module would otherwise crowd out the step
#: itself; the point is orientation, not a second copy of the source.
MAX_SYMBOLS_PER_FILE = 25

#: Definitions resolved for names mentioned in the step's prose.
MAX_DEFINITIONS = 8

#: Files indexed before the index is judged too expensive to build inside a
#: run. Well above any repository the eval suite uses and below the point where
#: parsing everything would be felt at the start of every task.
MAX_INDEXED_FILES = 4_000

#: Identifiers in a step's prose that are words, not code. Without this every
#: title contributes "add", "file", "test" and the definitions block fills with
#: whatever the repository happens to have called something.
_PROSE = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "has",
        "have",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "not",
        "of",
        "on",
        "or",
        "that",
        "the",
        "then",
        "there",
        "these",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "add",
        "address",
        "all",
        "also",
        "any",
        "append",
        "apply",
        "back",
        "build",
        "call",
        "case",
        "change",
        "check",
        "class",
        "class_",
        "code",
        "create",
        "data",
        "default",
        "define",
        "delete",
        "do",
        "does",
        "done",
        "edit",
        "ensure",
        "error",
        "exist",
        "exists",
        "expected",
        "export",
        "field",
        "file",
        "files",
        "find",
        "fix",
        "function",
        "functions",
        "get",
        "handle",
        "handler",
        "implement",
        "import",
        "include",
        "instead",
        "keep",
        "line",
        "lines",
        "list",
        "load",
        "logic",
        "make",
        "method",
        "methods",
        "module",
        "modules",
        "name",
        "names",
        "need",
        "needs",
        "new",
        "next",
        "note",
        "now",
        "number",
        "object",
        "only",
        "open",
        "other",
        "output",
        "pass",
        "patch",
        "path",
        "paths",
        "print",
        "project",
        "provide",
        "raise",
        "read",
        "remove",
        "rename",
        "replace",
        "report",
        "result",
        "return",
        "returns",
        "run",
        "running",
        "same",
        "section",
        "set",
        "should",
        "show",
        "side",
        "simple",
        "so",
        "some",
        "source",
        "start",
        "state",
        "step",
        "steps",
        "still",
        "store",
        "string",
        "support",
        "sure",
        "system",
        "take",
        "test",
        "tests",
        "text",
        "than",
        "them",
        "they",
        "thing",
        "time",
        "type",
        "update",
        "use",
        "used",
        "user",
        "using",
        "value",
        "values",
        "verify",
        "version",
        "view",
        "way",
        "when",
        "where",
        "which",
        "while",
        "why",
        "work",
        "write",
        "written",
    ]
)

_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")


def build_index(workspace: Path, *, use_git: bool = True) -> PythonCodeIndex | None:
    """Index the workspace, or return None if that is not worth doing.

    None is a normal answer, not a failure: a repository with no Python, one
    too large to parse inside a run, or one that cannot be scanned at all still
    has to be workable. The caller simply gets no grounding block.
    """
    # Deliberately broad. This module's whole contract is that it cannot fail a
    # run that would otherwise have worked, and a narrow `except` clause does
    # not deliver that: the first version caught OSError, ValueError and
    # RecursionError, then called `indexed_files` — a property — as a method,
    # and the resulting TypeError broke *every* task in the suite. An
    # optimisation that can take the runtime down is not an optimisation.
    #
    # Nothing is silenced that matters: a failure here means no grounding
    # section, the model falls back to `code.search`, and the run proceeds
    # exactly as it did before this module existed.
    try:
        index = PythonCodeIndex(workspace, use_git=use_git).build()
        files = index.indexed_files
        if not files or len(files) > MAX_INDEXED_FILES:
            return None
    except Exception:  # noqa: BLE001 - see above; grounding must never be fatal
        return None
    return index


def ground(index: PythonCodeIndex | None, step: PlanStepRecord) -> tuple[tuple[str, str], ...]:
    """`(label, body)` pairs for the compiler's source-excerpt section."""
    if index is None:
        return ()

    try:
        sections: list[tuple[str, str]] = []
        listed: set[str] = set()

        for path in step.inputs:
            outline = _outline(index, path)
            if outline is not None:
                sections.append(outline)
                listed.add(_key(path))

        definitions = _definitions(index, step, skip=listed)
        if definitions is not None:
            sections.append(definitions)
    except Exception:  # noqa: BLE001 - a missing section beats a dead run
        return ()

    return tuple(sections)


def _outline(index: PythonCodeIndex, path: str) -> tuple[str, str] | None:
    """Every symbol in one file, with the lines it occupies."""
    try:
        symbols = list(index.symbols_in(path))
    except (OSError, KeyError):
        return None
    if not symbols:
        return None

    lines = [
        f"  {symbol.lines.start:>4}-{symbol.lines.end:<4} {symbol.kind:<9}"
        f" {symbol.signature or symbol.qualified_name}"
        for symbol in symbols[:MAX_SYMBOLS_PER_FILE]
    ]
    if len(symbols) > MAX_SYMBOLS_PER_FILE:
        lines.append(f"  … and {len(symbols) - MAX_SYMBOLS_PER_FILE} more")

    return (
        f"{path} — what is in this file",
        "\n".join(lines) + "\n  (line numbers locate the code; read the file before editing it)",
    )


def _definitions(
    index: PythonCodeIndex, step: PlanStepRecord, *, skip: set[str]
) -> tuple[str, str] | None:
    """Where the names this step mentions are actually defined.

    The retrieval grep is worst at. A symbol re-exported through a package
    `__init__` reads identically at every call site, and the one place that can
    be edited is the one the text search does not distinguish.
    """
    found: list[tuple[str, SymbolRef]] = []
    seen: set[str] = set()

    for name in _candidate_names(step):
        if name in seen:
            continue
        seen.add(name)
        try:
            references = index.lookup_symbol(name)
        except (OSError, KeyError):
            continue
        for reference in references[:2]:
            if _key(reference.path) in skip:
                continue
            found.append((name, reference))
        if len(found) >= MAX_DEFINITIONS:
            break

    if not found:
        return None

    lines = [
        f"  {name} is defined at {reference.path}:{reference.lines.start}"
        f"  ({reference.signature or reference.kind})"
        for name, reference in found[:MAX_DEFINITIONS]
    ]
    return ("where the names in this step are defined", "\n".join(lines))


def _candidate_names(step: PlanStepRecord) -> tuple[str, ...]:
    """Identifiers in the step's own words, in the order they appear."""
    text = " ".join([step.title, *step.constraints, *step.acceptance_criteria])
    names: list[str] = []
    for match in _IDENTIFIER.finditer(text):
        name = match.group(0)
        lowered = name.lower()
        if lowered in _PROSE or keyword.iskeyword(lowered) or name.isupper():
            continue
        if name not in names:
            names.append(name)
    return tuple(names)


def _key(path: str) -> str:
    return workspace_key(path)


__all__ = [
    "MAX_DEFINITIONS",
    "MAX_INDEXED_FILES",
    "MAX_SYMBOLS_PER_FILE",
    "build_index",
    "ground",
]
