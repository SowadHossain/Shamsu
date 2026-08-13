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

    #: Fixed values the argument accepts. Where the set is known and small,
    #: naming it here means the dropdown can offer it and `handle_command` can
    #: reject anything else against the same list.
    values: tuple[str, ...] = ()

    #: Where the argument's values come from when they are not fixed:
    #: "model" (what Ollama has pulled) or "path" (workspace directories).
    #: A name rather than a callable, so this table stays data.
    source: str = ""

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
    Command("/tools", "what the agent can run, and the arguments each takes"),
    Command("/sessions", "recent runs in this workspace", aliases=("/runs",)),
    Command("/status", "model, workspace, database, mode"),
    Command(
        "/mode",
        "build (can edit) or plan (read-only)",
        argument="[build|plan]",
        values=("build", "plan"),
    ),
    Command(
        "/model",
        "show the model, or switch to another",
        argument="[name]",
        source="model",
    ),
    Command(
        "/workspace",
        "show the workspace, or move to another",
        argument="[path]",
        source="path",
    ),
    Command("/context", "show the context window, or set it", argument="[tokens]"),
    Command("/details", "show full tool output, or just the summary line"),
    Command(
        "/theme",
        "colour on or off",
        argument="[on|off]",
        values=("on", "off"),
        aliases=("/themes",),
    ),
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


def allowed_values(name: str) -> tuple[str, ...]:
    """The fixed values a command's argument accepts, or `()`.

    So a handler validates against the same list the dropdown offers, instead
    of holding a second copy that can drift from it.
    """
    command = lookup(name)
    return command.values if command else ()


def complete_argument(
    text: str,
    *,
    models: Sequence[str] = (),
    paths: Sequence[str] = (),
    limit: int = 8,
) -> tuple[str, ...]:
    """Values for the argument being typed, once a command has been chosen.

    The counterpart to `complete`, which deliberately stops at the space. Where
    the valid set is known — two modes, two theme states, whatever Ollama has
    pulled — leaving the user to remember it is a worse interface than showing
    it, and every one of these sets is already known to the program.

    Dynamic values arrive as arguments rather than being fetched here, for the
    same reason `complete` takes `models`: this stays pure, and a slow or absent
    Ollama cannot wedge the prompt.
    """
    if not text.startswith("/"):
        return ()

    head, separator, rest = text.partition(" ")
    if not separator:
        return ()

    command = lookup(head)
    if command is None:
        return ()

    # Only the first argument. Nothing here takes a second, and guessing at one
    # would offer values for a position that does not exist.
    if " " in rest.strip():
        return ()

    if command.values:
        available: Sequence[str] = command.values
    elif command.source == "model":
        available = models
    elif command.source == "path":
        available = paths
    else:
        return ()

    fragment = rest.strip().lower()
    matches = [value for value in available if value.lower().startswith(fragment)]
    return tuple(matches[:limit])


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
    "allowed_values",
    "common_prefix",
    "complete_argument",
    "complete",
    "file_fragment",
    "help_lines",
    "lookup",
    "match_files",
]
