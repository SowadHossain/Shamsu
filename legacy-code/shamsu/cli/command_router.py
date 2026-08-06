"""Local slash-command validation for the REPL."""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import get_close_matches


@dataclass(frozen=True)
class CommandRoute:
    valid: bool
    command: str = ""
    normalized: str = ""
    error: str = ""
    suggestions: list[str] = field(default_factory=list)


class CommandRouter:
    def __init__(self, commands: tuple[str, ...]) -> None:
        self.commands = tuple(command.strip() for command in commands)
        self._names = sorted(
            {command.split()[0].rstrip().lstrip("/") for command in self.commands}
        )

    def route(self, user_input: str) -> CommandRoute:
        stripped = user_input.strip()
        if not stripped.startswith("/"):
            return CommandRoute(valid=False, error="Not a slash command.")
        normalized = stripped[1:].strip()
        if not normalized:
            return CommandRoute(
                valid=False,
                normalized="",
                error="Unknown command: /. Run /help for commands.",
                suggestions=["/help"],
            )
        name = normalized.split(maxsplit=1)[0]
        if name in self._names:
            return CommandRoute(valid=True, command=name, normalized=normalized)
        suggestions = [f"/{item}" for item in get_close_matches(name, self._names, n=3, cutoff=0.45)]
        return CommandRoute(
            valid=False,
            command=name,
            normalized=normalized,
            error=f"Unknown command: /{name}. Run /help for commands.",
            suggestions=suggestions,
        )
