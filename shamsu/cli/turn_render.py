"""The CLI's renderer over the turn stream.

This is deliberately thin. The CLI is the reference surface - what it prints is
the definition of "everything that happened" - so this file must not decide
anything the other renderers do not also see. It reads the shared
`body_kinds()` rule, prints those lines dim, and hands `status` to the live
spinner. Nothing else.

Observable behaviour is unchanged from the two lambdas it replaces
(`console.print(f"[dim]{message}[/dim]")` and the status updater): the same
lines, in the same order, painted the same way.
"""
from __future__ import annotations

from typing import Any, Callable

from rich.markup import escape

from shamsu.cli.prompt_label import prompt_label
from shamsu.runtime.turn_stream import TurnEvent, body_kinds


class CliTurnRenderer:
    """Paint a turn on a rich console.

    `echo_surface`, when set, prints a `shamsu (<title>) <surface>> <prompt>`
    line at `turn.start`, built by the shared `prompt_label`. The local REPL
    leaves it unset - it already shows the prompt the user typed - while a
    remote turn sets it, so a turn started elsewhere reads on the desktop like
    any other terminal turn instead of arriving as a coloured panel.
    """

    def __init__(
        self,
        console: Any,
        *,
        status_updater: Callable[[str], None] | None = None,
        verbosity: str = "normal",
        echo_surface: str = "",
        echo_title: str = "",
    ) -> None:
        self.console = console
        self.status_updater = status_updater
        self.verbosity = verbosity
        self.echo_surface = echo_surface
        self.echo_title = echo_title
        self._body = body_kinds(verbosity)
        #: Every body line this renderer printed, in order. The parity test
        #: compares this against the Telegram card's list.
        self.lines: list[str] = []

    def __call__(self, event: TurnEvent) -> None:
        if event.kind == "status":
            # The spinner owns the status; it REPLACES rather than appends, and
            # printing it as well would fill the scrollback with ticks.
            if self.status_updater is not None:
                self.status_updater(event.text)
            return
        if event.kind == "turn.start":
            if self.echo_surface and event.text:
                label = prompt_label(
                    self.echo_title, self.echo_surface, trailing_space=False
                )
                self.console.print(f"[bold]{escape(label)}[/bold] {event.text}")
            return
        if event.kind not in self._body or not event.text:
            return
        self.lines.append(event.text)
        self.console.print(f"[dim]{event.text}[/dim]")
