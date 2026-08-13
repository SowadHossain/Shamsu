"""Resolving `@file` references before a request reaches the agent.

The interface offers `@` completion and inserts `@src/auth.py ` into the input.
Nothing then removed the `@`, so the agent received the literal string — and a
live run answered *"the repository contains a single file named
@OpenBazaar_Marketplace_PRD.docx"*, having tried to read a path that does not
exist and given up.

What this does is small and deliberately dumb: strip the marker, check the path
is really in the workspace, and say so plainly at the end of the request. The
agent still decides whether to read it. Inlining the contents here would be
the opposite of the prime directive — the context compiler chooses what a
decision needs, and a caller that pastes a whole document into the task text
has taken that choice away from it.

**A mention that does not resolve is kept, not dropped.** If the user typed
`@notes.md` and there is no such file, the request still says `notes.md` and
the run reports it as unresolved. Silently deleting the only concrete noun in a
sentence produces a request that means something different from what was typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from shamsu.artifacts.hashing import is_ignored

#: An `@` that begins a word, followed by everything up to whitespace. Matches
#: `ui.commands.file_fragment`, which is what puts these in the text — a
#: reference the picker can create and this cannot read would be worse than no
#: picker at all.
_MENTION = re.compile(r"(?:(?<=\s)|^)@(?P<path>[^\s]+)")

#: Trailing punctuation that belongs to the sentence rather than the filename:
#: "look at @auth.py, then fix it".
_TRAILING = ",.;:!?)]}\"'"


@dataclass(frozen=True)
class Mentions:
    """What a request referred to, and what those references resolved to."""

    #: The request with `@` markers removed, plus a line naming what resolved.
    text: str

    #: Workspace-relative paths that exist.
    resolved: tuple[str, ...] = ()

    #: Referenced paths that do not exist in the workspace.
    unresolved: tuple[str, ...] = ()

    @property
    def any_found(self) -> bool:
        return bool(self.resolved)


def resolve(request: str, workspace: Path) -> Mentions:
    """Turn `@path` references into real workspace-relative paths.

    Resolution is by exact path first, then by unique filename — the picker
    inserts a full path, but a person typing `@auth.py` from memory means the
    one file called that. An ambiguous name resolves to nothing rather than to
    a guess: picking one of three `__init__.py` would be wrong twice.
    """
    found = list(_MENTION.finditer(request))
    if not found:
        return Mentions(text=request)

    resolved: list[str] = []
    unresolved: list[str] = []
    replacements: list[tuple[int, int, str]] = []

    for match in found:
        raw = match.group("path").rstrip(_TRAILING)
        if not raw:
            continue
        target = _locate(raw, workspace)
        if target is None:
            unresolved.append(raw)
        elif target not in resolved:
            resolved.append(target)
        # The marker goes whether or not it resolved: `@` is interface syntax
        # and means nothing to the agent.
        replacements.append((match.start(), match.start() + 1 + len(raw), target or raw))

    text = request
    for start, end, replacement in reversed(replacements):
        text = text[:start] + replacement + text[end:]

    if resolved:
        listed = ", ".join(resolved)
        text = f"{text.strip()}\n\nThe user referred to these files: {listed}"

    return Mentions(text=text, resolved=tuple(resolved), unresolved=tuple(unresolved))


def _locate(reference: str, workspace: Path) -> str | None:
    """The workspace-relative path a reference names, or None."""
    cleaned = reference.replace("\\", "/").lstrip("./")
    if not cleaned:
        return None

    direct = workspace / cleaned
    if direct.is_file():
        return cleaned

    # A bare filename: unique match only.
    if "/" not in cleaned:
        matches = [
            path.relative_to(workspace).as_posix()
            for path in workspace.rglob(cleaned)
            if path.is_file() and not is_ignored(path.relative_to(workspace))
        ]
        if len(matches) == 1:
            return matches[0]

    return None


__all__ = ["Mentions", "resolve"]
