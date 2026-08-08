"""The command registry, and completion over it.

One table describes every `/` command: its name, aliases, argument hint, and
one-line summary. `/help`, the autocomplete dropdown, and the dispatcher all
read from it, so a command cannot exist in one and be missing from another --
which is exactly how v1 ended up with commands nobody could discover.

**Completion is a pure function.** `complete(text)` takes what has been typed
and returns what matches. No terminal, no state, no side effects, so every
matching rule below is asserted by calling it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    """One slash command, as the interface describes it to a user."""

    name: str
    summary: str
    argument: str = ""
    aliases: tuple[str, ...] = ()

    @property
    def usage(self) -> str:
        return f"{self.name} {self.argument}".rstrip()

    def matches(self, prefix: str) -> bool:
        """Whether `prefix` could be the start of this command or an alias."""
        return self.name.startswith(prefix) or any(a.startswith(prefix) for a in self.aliases)


#: Ordered so the dropdown reads sensibly: what you do, then what you change,
#: then the way out. Alphabetical would put /exit first, which is unhelpful.
COMMANDS: tuple[Command, ...] = (
    Command("/help", "this list", aliases=("/?",)),
    Command("/new", "start a fresh task, keeping the workspace", aliases=("/clear",)),
    Command("/sessions", "recent runs in this workspace", aliases=("/runs",)),
    Command("/status", "model, workspace, database, mode"),
    Command("/mode", "build (can edit) or plan (read-only)", argument="[build|plan]"),
    Command("/model", "show the model, or switch to another", argument="[name]"),
    Command("/workspace", "show the workspace, or move to another", argument="[path]"),
    Command("/context", "show the context window, or set it", argument="[tokens]"),
    Command("/details", "show full tool output, or just the summary line"),
    Command("/theme", "colour on or off", argument="[on|off]", aliases=("/themes",)),
    Command("/exit", "leave", aliases=("/quit", "/q")),
)

_BY_NAME: dict[str, Command] = {}
for _command in COMMANDS:
    _BY_NAME[_command.name] = _command
    for _alias in _command.aliases:
        _BY_NAME[_alias] = _command


def lookup(name: str) -> Command | None:
    """The command a name or alias refers to."""
    return _BY_NAME.get(name.lower())


def complete(text: str) -> tuple[Command, ...]:
    """Commands matching what has been typed so far.

    Empty unless the line *is* a command being typed: a request that happens to
    contain a slash is prose, and offering completions over it would be noise.
    Once a complete command has been typed and a space follows, the choice is
    made and the dropdown closes.
    """
    if not text.startswith("/"):
        return ()

    head, separator, _ = text.partition(" ")
    if separator:
        return ()

    prefix = head.lower()
    if prefix == "/":
        return COMMANDS

    return tuple(command for command in COMMANDS if command.matches(prefix))


def common_prefix(commands: tuple[Command, ...]) -> str:
    """The longest prefix every match shares, for Tab to fill in.

    Completing to the shared prefix rather than to the first match is what
    makes Tab feel predictable: `/s` becomes `/s` (sessions and status differ
    immediately), never a silent jump to one of them.
    """
    if not commands:
        return ""
    if len(commands) == 1:
        return commands[0].name

    shortest = min((c.name for c in commands), key=len)
    for index in range(len(shortest)):
        if any(c.name[index] != shortest[index] for c in commands):
            return shortest[:index]
    return shortest


def file_fragment(text: str, cursor: int) -> tuple[int, str] | None:
    """The `@…` reference being typed at the cursor, and where it starts.

    `None` unless the cursor sits inside one. A reference runs from an `@` that
    begins a word to the next whitespace, so an email address in the middle of
    a sentence does not open a file picker.
    """
    if cursor > len(text):
        return None

    before = text[:cursor]
    at = before.rfind("@")
    if at == -1:
        return None
    if at > 0 and not before[at - 1].isspace():
        return None
    if any(character.isspace() for character in before[at + 1 :]):
        return None
    return at, before[at + 1 :]


def match_files(fragment: str, paths: Sequence[str], limit: int = 8) -> tuple[str, ...]:
    """Workspace paths matching `fragment`, best first.

    Substring rather than fuzzy-subsequence matching, and deliberately: a
    subsequence matcher turns `test` into a match for `t/e/s/t.py`, which reads
    as noise at a prompt where the user usually knows most of the name. Ranked
    so a hit in the filename beats a hit anywhere in the path, because that is
    what was almost certainly meant.
    """
    if not fragment:
        return tuple(sorted(paths)[:limit])

    needle = fragment.lower()
    scored: list[tuple[int, int, str]] = []
    for path in paths:
        lowered = path.lower()
        name = lowered[lowered.rfind("/") + 1 :]
        stem = name.rsplit(".", 1)[0] if "." in name else name

        # Exact stem first, so `theme` offers theme.py ahead of
        # theme-notes.md — both start with it, but only one *is* it.
        if stem == needle:
            rank = 0
        elif name.startswith(needle):
            rank = 1
        elif needle in name:
            rank = 2
        elif needle in lowered:
            rank = 3
        else:
            continue
        scored.append((rank, len(path), path))

    return tuple(path for _, _, path in sorted(scored)[:limit])


def help_lines() -> tuple[str, ...]:
    """`/help`, rendered from the same table the dropdown uses."""
    width = max(len(command.usage) for command in COMMANDS)
    lines = [f"  {'<request>'.ljust(width)}  run it against this workspace"]
    lines.extend(f"  {command.usage.ljust(width)}  {command.summary}" for command in COMMANDS)
    return tuple(lines)


__all__ = [
    "COMMANDS",
    "Command",
    "common_prefix",
    "complete",
    "file_fragment",
    "help_lines",
    "lookup",
    "match_files",
]
