"""Deterministic Telegram command parsing."""
from __future__ import annotations

from dataclasses import dataclass


COMMANDS = {
    "/start",
    "/help",
    "/status",
    "/plan",
    "/sessions",
    "/switch",
    "/new",
    "/changes",
    "/tests",
    "/logs",
    "/pause",
    "/resume",
    "/cancel",
    "/settings",
}


@dataclass(frozen=True)
class ParsedCommand:
    name: str
    argument: str = ""


def parse_command(text: str) -> ParsedCommand | None:
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None
    first, _, rest = stripped.partition(" ")
    # Telegram can send /command@botname in group-like contexts; groups are
    # blocked by default, but normalizing keeps tests and future webhooks sane.
    name = first.split("@", 1)[0].lower()
    if name not in COMMANDS:
        return ParsedCommand(name=name, argument=rest.strip())
    return ParsedCommand(name=name, argument=rest.strip())

