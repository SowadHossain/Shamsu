"""``/session`` - the conversation-thread commands carried over from the REPL.

The old REPL had seventeen ``/sessions`` sub-commands. Most of them existed to
inspect an orchestrator that no longer runs. What survives is the set that a
chat harness genuinely needs: see the threads, start one, go back to one, name
one, and end one.
"""

from __future__ import annotations

from rich.table import Table

from shamsu.cli.commands import Command, CommandContext


def _manager(context: CommandContext):
    manager = context.manager
    if manager is None:
        context.echo("[red]No session manager is attached to this surface.[/red]")
    return manager


def show(context: CommandContext, _argument: str = "") -> None:
    """Bare ``/session``: which thread is live, and what else is on disk."""
    manager = _manager(context)
    if manager is None:
        return
    current = context.session_logger
    current_id = current.session_id if current is not None else ""
    sessions = manager.list_sessions()
    if not sessions:
        context.echo("[dim]No sessions recorded for this workspace yet.[/dim]")
        return

    table = Table(box=None, pad_edge=False, show_header=True, header_style="dim")
    table.add_column("#", style="dim", width=3)
    table.add_column("title")
    table.add_column("id", style="dim")
    table.add_column("", style="dim")
    for index, metadata in enumerate(sessions[:20], start=1):
        marks = []
        if metadata.session_id == current_id:
            marks.append("active")
        if getattr(metadata, "closed_at", None):
            marks.append("closed")
        table.add_row(str(index), metadata.title or "(untitled)", metadata.session_id, ", ".join(marks))
    context.console.print(table)
    context.echo("[dim]/session new [title]   /session resume <id|title>   /session rename <title>[/dim]")


def new(context: CommandContext, argument: str) -> None:
    manager = _manager(context)
    if manager is None:
        return
    logger = manager.create_session(argument.strip() or None)
    _adopt(context, logger)
    context.echo(f"[green]Started[/green] {logger.metadata.title} [dim]({logger.session_id})[/dim]")


def resume(context: CommandContext, argument: str) -> None:
    manager = _manager(context)
    if manager is None:
        return
    query = argument.strip()
    if not query:
        context.echo("[red]Usage: /session resume <id or title>[/red]")
        return
    try:
        logger = manager.resume_session(query)
    except (KeyError, ValueError) as exc:
        context.echo(f"[red]{exc}[/red]")
        return
    _adopt(context, logger)
    context.echo(f"[green]Resumed[/green] {logger.metadata.title} [dim]({logger.session_id})[/dim]")


def rename(context: CommandContext, argument: str) -> None:
    manager = _manager(context)
    current = context.session_logger
    if manager is None or current is None:
        return
    title = argument.strip()
    if not title:
        context.echo("[red]Usage: /session rename <new title>[/red]")
        return
    metadata = manager.rename_session(current.session_id, title)
    context.echo(f"[green]Renamed to[/green] {metadata.title}")


def close(context: CommandContext, _argument: str = "") -> None:
    manager = _manager(context)
    current = context.session_logger
    if manager is None or current is None:
        return
    manager.close_session(current.session_id)
    logger = manager.create_session()
    _adopt(context, logger)
    context.echo(f"[green]Closed. Now on[/green] {logger.metadata.title}")


def _adopt(context: CommandContext, logger) -> None:
    """Point the surface at a different thread.

    The REPL rebuilt its whole world on a session switch. Here the app holds
    exactly one reference, so swapping it is the entire operation - the next
    turn reads history from whichever logger is attached.
    """
    if context.app is not None:
        context.app.session_logger = logger


_SUBCOMMANDS = {
    "list": show,
    "status": show,
    "current": show,
    "new": new,
    "resume": resume,
    "rename": rename,
    "close": close,
}


def handle(context: CommandContext, argument: str) -> None:
    verb, _, rest = argument.strip().partition(" ")
    action = _SUBCOMMANDS.get(verb.lower())
    if action is None:
        if argument.strip():
            context.echo(
                "[red]Usage: /session list|new|resume|rename|close[/red]"
            )
        else:
            show(context)
        return
    action(context, rest.strip())


COMMANDS = (
    Command(
        name="/session",
        aliases=("/sessions",),
        summary="List, start, resume, rename, or close a conversation thread",
        handler=handle,
        completions=("list", "new ", "resume ", "rename ", "close"),
    ),
)
