"""Slash commands for the small harness.

The old REPL kept ~150 slash commands and their handlers in one 20,000-line
module, which is why nobody could tell which ones still worked. The small
harness keeps a much smaller set, and keeps each one in a named module with a
single registration point, so the answer to "what commands exist" is this
file's :data:`REGISTRY` rather than a grep.

A handler takes a :class:`CommandContext` and the raw argument string (already
stripped of the command word) and returns nothing. Printing is the handler's
job - it holds the console.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.console import Console

    from shamsu.session.manager import SessionLogger, SessionManager


@dataclass
class CommandContext:
    """Everything a slash command is allowed to touch.

    Deliberately narrow. A handler that needs more than this is doing
    orchestration, and orchestration belongs in the app, not in a command.
    """

    workspace: Path
    console: "Console"
    app: Any = None

    @property
    def manager(self) -> "SessionManager | None":
        return getattr(self.app, "manager", None)

    @property
    def session_logger(self) -> "SessionLogger | None":
        return getattr(self.app, "session_logger", None)

    def echo(self, message: str) -> None:
        self.console.print(message)


Handler = Callable[[CommandContext, str], None]


@dataclass(frozen=True)
class Command:
    name: str
    summary: str
    handler: Handler
    #: Completion suffixes offered after the command word, e.g. ``("use ", "tier")``.
    completions: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    #: Safe to run while a turn is still in flight. Only commands that read
    #: state qualify: a turn already holds the model, the session log and the
    #: tool loop, so anything that WRITES config mid-turn changes the rules
    #: underneath a run that is halfway through obeying them.
    midturn: bool = False

    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


def _build_registry() -> dict[str, Command]:
    from shamsu.cli.commands import basics, mcp, models, sessions, skills

    commands: Iterable[Command] = (
        *basics.COMMANDS,
        *models.COMMANDS,
        *sessions.COMMANDS,
        *skills.COMMANDS,
        *mcp.COMMANDS,
    )
    registry: dict[str, Command] = {}
    for command in commands:
        for name in command.names():
            registry[name] = command
    return registry


_REGISTRY: dict[str, Command] | None = None


def registry() -> dict[str, Command]:
    """Command word (with leading ``/``) -> :class:`Command`, aliases included.

    Built lazily: the command modules import the runtime and session layers,
    and importing those at module scope would make ``shamsu.cli.commands`` an
    expensive import for anything that only wants :class:`CommandContext`.
    """
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def unique_commands() -> list[Command]:
    """Every registered command once, in registration order."""
    seen: list[Command] = []
    for command in registry().values():
        if command not in seen:
            seen.append(command)
    return seen


def completion_words() -> list[str]:
    """Every completable token, e.g. ``/model``, ``/model use ``, ``/exit``."""
    words: list[str] = []
    for command in unique_commands():
        words.append(command.name)
        words.extend(f"{command.name} {suffix}" for suffix in command.completions)
    return words


def split(user_input: str) -> tuple[str, str]:
    """``"/model use foo"`` -> ``("/model", "use foo")``."""
    text = (user_input or "").strip()
    if not text.startswith("/"):
        return "", text
    head, _, rest = text.partition(" ")
    return head.lower(), rest.strip()


def dispatch(user_input: str, context: CommandContext, *, midturn: bool = False) -> bool:
    """Run the matching command. False means "not a command, treat as a prompt".

    An unknown ``/word`` is still handled (with a "no such command" note)
    rather than being sent to the model: a mistyped command reaching the model
    as a prompt is how the old REPL turned typos into full agent turns.
    """
    name, argument = split(user_input)
    if not name:
        return False
    command = registry().get(name)
    if command is None:
        known = ", ".join(sorted({c.name for c in unique_commands()}))
        context.echo(f"[red]No such command: {name}[/red]")
        context.echo(f"[dim]Commands: {known}[/dim]")
        return True
    if midturn and not command.midturn:
        context.echo(f"[dim]{name} waits until this turn finishes.[/dim]")
        return True
    command.handler(context, argument)
    return True


__all__ = [
    "Command",
    "CommandContext",
    "Handler",
    "completion_words",
    "dispatch",
    "registry",
    "split",
    "unique_commands",
]
