"""The commands that are about the harness itself rather than about work.

Help, quitting, the frame, how much of a turn to show, and what the context
window is doing. Everything here is cheap and synchronous - nothing in this
module may reach a model or the network, because these are the commands people
type when something else is already stuck.
"""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from shamsu.cli.commands import Command, CommandContext, unique_commands
from shamsu.runtime.settings import VERBOSITY_LEVELS, update_settings, verbosity


def help_(context: CommandContext, _argument: str = "") -> None:
    table = Table(box=None, pad_edge=False, show_header=False)
    table.add_column("command", style="cyan", no_wrap=True)
    table.add_column("what it does")
    for command in unique_commands():
        table.add_row(command.name, command.summary)
    context.console.print(
        Panel(
            table,
            title="SHAMSU small harness",
            subtitle="anything that is not a /command runs as a prompt",
            border_style="cyan",
        )
    )


def exit_(context: CommandContext, _argument: str = "") -> None:
    if context.app is not None:
        context.app.request_exit()
    context.echo("Goodbye.")


def tui(context: CommandContext, argument: str) -> None:
    """Toggle the framed TUI, or force it with ``on``/``off``."""
    app = context.app
    if app is None:
        context.echo("[yellow]No terminal app is attached.[/yellow]")
        return
    wanted = argument.strip().lower()
    running = getattr(app, "frame", None) is not None
    if wanted == "on" or (not wanted and not running):
        app.start_frame()
        return
    if wanted == "off" or (not wanted and running):
        app.stop_frame()
        context.echo("[dim]Frame closed.[/dim]")
        return
    context.echo("[red]Usage: /tui [on|off][/red]")


def context_(context: CommandContext, _argument: str = "") -> None:
    """What the last turn did to the context window.

    Worth its own command because the number that matters on a small model is
    not the model name, it is how close the window is to full - that is what
    decides whether the next turn remembers the last one.
    """
    telemetry = getattr(context.app, "telemetry", None)
    if telemetry is None or not getattr(telemetry, "ctx_total", 0):
        context.echo("[dim]No model turn has reported context usage yet.[/dim]")
        return
    used = telemetry.ctx_used
    total = telemetry.ctx_total
    pct = telemetry.ctx_pct or 0
    bar_width = 24
    filled = min(bar_width, round(bar_width * pct / 100))
    bar = "#" * filled + "." * (bar_width - filled)
    colour = "red" if pct >= 90 else "yellow" if pct >= 70 else "green"
    context.echo(f"[{colour}]{bar}[/{colour}] {used}/{total} tokens ({pct}%)")


def verbosity_(context: CommandContext, argument: str) -> None:
    """Read or set how much of a turn every surface shows."""
    wanted = argument.strip().lower()
    if not wanted:
        current = verbosity()
        options = ", ".join(sorted(VERBOSITY_LEVELS))
        context.echo(f"[cyan]Verbosity:[/cyan] {current}  [dim](choices: {options})[/dim]")
        return
    if wanted not in VERBOSITY_LEVELS:
        context.echo(
            "[red]Unknown verbosity. Choose one of: "
            + ", ".join(sorted(VERBOSITY_LEVELS))
            + "[/red]"
        )
        return
    update_settings(verbosity=wanted)
    context.echo(f"[green]Verbosity is now {wanted}.[/green] [dim]Applies to the next turn.[/dim]")


COMMANDS = (
    Command(
        name="/help",
        summary="List the commands",
        handler=help_,
        midturn=True,
    ),
    Command(
        name="/context",
        summary="Show how full the context window is",
        handler=context_,
        midturn=True,
    ),
    Command(
        name="/verbosity",
        summary="How much of a turn to show (quiet / normal / verbose)",
        handler=verbosity_,
        completions=tuple(sorted(VERBOSITY_LEVELS)),
    ),
    Command(
        name="/tui",
        summary="Toggle the framed terminal UI",
        handler=tui,
        completions=("on", "off"),
    ),
    Command(
        name="/exit",
        aliases=("/quit",),
        summary="Leave SHAMSU",
        handler=exit_,
    ),
)
