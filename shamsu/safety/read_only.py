"""The user's "do not change files" instruction, as a first-class signal.

Before this module the phrase existed as two near-identical regexes (one in
`cli/repl.py`, one in `routing/operations.py`) that only ever produced a
boolean nobody enforced. The 2026-07-20 dogfood showed what that cost:

* "Use web search ... **Do not modify files.**" routed to `file.write`, because
  the route detector saw the verb `modify` and the noun `files` and never
  noticed the `Do not` in front of them. The identical prompt without that
  sentence routed correctly;
* the same phrase then made `_request_requires_workspace_change` demand a
  mutation, so a correct web answer was reported as
  "I did not complete the requested workspace change";
* and under `--approval allow` the agent overwrote a file the prompt had
  explicitly told it not to touch, because nothing enforced the instruction.

So: keyword detectors must not read a NEGATED verb as intent. `strip()` masks
the constraint clause before any such scan, `applies()` reports the signal, and
`AgentToolRegistry.set_read_only()` turns it into a hard deny at the tool
boundary. Approval mode is a separate question - "you may act without asking"
is not "you may ignore what I said".
"""
from __future__ import annotations

import re

# The verbs a read-only clause negates, and the objects it negates them on.
_FORBID = r"(?:do\s+not|do\s?n[o']t|dont|never|no\s+need\s+to|avoid)"
_CHANGE_VERB = r"(?:change|edit|modify|write|create|delete|remove|touch|alter|update|overwrite)"
_CHANGE_GERUND = (
    r"(?:changing|editing|modifying|writing|creating|deleting|touching|altering|updating|overwriting)"
)
# "any files", "my code", "the workspace", "any other files", ...
_TARGET = (
    r"(?:any\s+|my\s+|the\s+|other\s+|existing\s+|else\s+)*"
    r"(?:files?|code|anything|source|the\s+workspace|workspace)"
)

# "Do not modify any OTHER files" is a carve-out, not a ban: it permits the one
# change just asked for and forbids collateral damage. Treating it as blanket
# read-only made SHAMSU refuse to create the very file the prompt requested
# ("Create shamsu_smoke_note.md ... Do not modify any other files"), which is
# a different way of failing the same task. Detected, reported separately, and
# deliberately NOT wired to the tool gate - scoping enforcement needs to know
# the permitted target, which belongs with the per-run semantic contract.
_CARVE_OUT_RE = re.compile(
    r"\b(?:other|else|remaining|rest\s+of|besides|except|outside)\b", re.IGNORECASE
)

# What "read only" is READING, when the two words are a verb and an adverb
# rather than the name of a mode.
#
# Live 2026-08-20: "Fix the file part by part: read the skeleton first, **read
# only the functions you need**, then fix the issues" was classified read-only,
# so a run that fixed a real bug and left the tests better than it found them
# reported `contract violation: prompt forbade file changes but 2 changed`. The
# sentence asks the model to read SELECTIVELY - it is the opposite of a refusal
# to write, and it is exactly the phrasing the outline-first read path invites.
#
# The hyphenated and closed forms stay unconditional: "read-only" and "readonly"
# are only ever the mode. Only the spaced form has to prove it is not governing
# an object.
_READ_OBJECT = (
    r"(?:the|that|this|these|those|what|whatever|which|a|an|any|some|each|"
    r"its|their|your|my|our|his|her|first|last|one|two|three|"
    r"lines?|files?|functions?|methods?|classes|parts?|sections?|symbols?|"
    r"code|source|tests?|docs?|enough)\b"
)

# NOTE: "dry run only" is deliberately NOT here. A dry run is "plan the change
# but don't apply it" - the opposite of a read-only refusal, which blocks the
# write outright. When both fired, read-only won and the tool refused before the
# dry-run recorder could preview anything, so a `--dry-run` create-file produced
# an empty plan. Dry run is its own mode (see is_dry_run + shamsu/safety/dry_run.py).
READ_ONLY_RE = re.compile(
    rf"\b{_FORBID}\s+{_CHANGE_VERB}\s+{_TARGET}\b"
    rf"|\bwithout\s+{_CHANGE_GERUND}\s+{_TARGET}\b"
    rf"|\b(?:read-only|readonly)\b"
    rf"|\bread\s+only\b(?!\s+{_READ_OBJECT})"
    rf"|\b(?:no\s+file\s+changes?|don'?t\s+save\s+anything)\b"
    rf"|\bleave\s+(?:the\s+)?(?:files?|code|workspace)\s+(?:alone|untouched|unchanged)\b",
    re.IGNORECASE,
)

# A dry-run REQUEST expressed in prose. The `--dry-run` flag is the primary
# trigger; this lets "dry run only: create X" work in the interactive REPL too,
# and is what the 2026-07-20 dogfood prompt actually used.
DRY_RUN_RE = re.compile(r"\bdry[\s-]?run(?:\s+only)?\b", re.IGNORECASE)


def is_dry_run(text: str) -> bool:
    return bool(DRY_RUN_RE.search(text or ""))


def _matches(text: str) -> list[re.Match[str]]:
    return list(READ_ONLY_RE.finditer(text or ""))


def _is_scoped_match(text: str, match: re.Match[str]) -> bool:
    """Recognize carve-out words that immediately follow the regex match.

    ``_TARGET`` can legally finish at ``anything`` in ``anything else``. The
    old implementation inspected only ``match.group(0)``, so it missed the
    trailing ``else`` and turned a scoped write request into a blanket deny.
    Only inspect the rest of the same clause to avoid borrowing words from a
    later sentence.
    """
    if _CARVE_OUT_RE.search(match.group(0)):
        return True
    tail = (text or "")[match.end() :]
    same_clause = re.split(r"[.!?;\n\r]", tail, maxsplit=1)[0]
    return bool(
        re.match(
            r"^\s*(?:,\s*)?(?:else\b|other\b|besides\b|except\b|outside\b|remaining\b|rest\s+of\b)",
            same_clause,
            re.IGNORECASE,
        )
    )


def applies(text: str) -> bool:
    """True when the prompt forbids changing files OUTRIGHT.

    A carve-out ("do not modify any other files") is excluded: it asks for one
    change and forbids the rest, so blocking every write fails the request just
    as surely as ignoring it did.
    """
    return any(not _is_scoped_match(text, match) for match in _matches(text))


def is_scoped(text: str) -> bool:
    """True for a carve-out: this change is fine, leave everything else alone."""
    return any(_is_scoped_match(text, match) for match in _matches(text))


def strip(text: str) -> str:
    """The prompt with read-only clauses masked out.

    Run this before any keyword/verb scan that infers *intent to act*, so
    "Do not modify files" stops reading as "modify files". The clause is
    replaced by a space rather than deleted so surrounding word boundaries and
    offsets stay sane.
    """
    if not text:
        return text
    return READ_ONLY_RE.sub(" ", text)
