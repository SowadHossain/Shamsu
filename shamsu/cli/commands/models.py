"""``/model`` - see and change the model the harness runs on.

The old REPL spelled this ``/models status|pull|repair|tier|use``: five
sub-commands spanning two different concepts (is the runtime healthy, and which
model do I want). The small harness keeps the concepts and drops the ceremony -
bare ``/model`` shows what is running and what is available, and every other
form is a verb on that list.

Precedence is not this module's invention; see
:func:`shamsu.runtime.models.model_source`. It is printed on every listing
because a workspace pin silently shadows an install-wide choice, and a picker
that appears to do nothing is worse than no picker at all.
"""

from __future__ import annotations

from pathlib import Path

from rich.table import Table

from shamsu.cli.commands import Command, CommandContext
from shamsu.runtime.models import (
    ModelTier,
    active_model_override,
    active_tier,
    clear_model_override,
    is_allowed_model,
    model_source,
    set_model_override,
    set_model_tier,
    tier_model_specs,
)
from shamsu.runtime.ollama import (
    RuntimeStatus,
    collect_status,
    list_loaded_models,
    pull_model_streaming,
    unload_model,
)

#: What each precedence level means, phrased the way the user set it.
_SOURCE_NOTE = {
    "env": "pinned by the SHAMSU_MODEL environment variable",
    "workspace": "pinned for this workspace by /model use",
    "install": "chosen install-wide (settings.json or the web settings panel)",
    "tier": "the active tier default",
}

_RESET_WORDS = {"tier", "tiers", "default", "off", "reset", "clear", "none"}


def _installed(context: CommandContext, status: RuntimeStatus) -> list[str]:
    """Installed model names, or an empty list plus the reason it is empty."""
    if not status.ollama_found:
        context.echo("[red]Ollama was not found on this machine.[/red]")
        return []
    if not status.server_running:
        context.echo(
            "[yellow]Ollama is not running, so installed models cannot be listed.[/yellow]\n"
            "[dim]Start it with: ollama serve[/dim]"
        )
        return []
    return list(status.installed_models)


def show(context: CommandContext, _argument: str = "") -> None:
    """Bare ``/model``: the active model, where it came from, and the choices."""
    source, name = model_source()
    context.echo(f"[cyan]Active model:[/cyan] {name}")
    context.echo(f"[dim]{_SOURCE_NOTE.get(source, source)} - tier {active_tier().value}[/dim]")

    status = collect_status()
    installed = _installed(context, status)
    if not installed:
        return
    loaded = set(list_loaded_models())

    table = Table(box=None, pad_edge=False, show_header=True, header_style="dim")
    table.add_column("#", style="dim", width=3)
    table.add_column("model")
    table.add_column("", style="dim")
    for index, model in enumerate(installed, start=1):
        marks = []
        if model == name:
            marks.append("active")
        if model in loaded:
            marks.append("in VRAM")
        if is_allowed_model(model):
            marks.append("cookbook")
        table.add_row(str(index), model, ", ".join(marks))
    context.console.print(table)
    context.echo(
        "[dim]/model use <name|#>   /model use tier   "
        "/model tier <light|default|heavy>   /model pull <name>[/dim]"
    )


def use(context: CommandContext, argument: str) -> None:
    """Pin a model for this workspace, by name or by its number in ``/model``."""
    choice = argument.strip()
    if not choice:
        show(context)
        return
    if choice.lower() in _RESET_WORDS:
        clear_model_override(context.workspace)
        source, name = model_source()
        context.echo(f"[green]Workspace pin cleared.[/green] Now using {name} ({source}).")
        return

    installed = _installed(context, collect_status())
    if not installed:
        return
    resolved = _resolve(context, choice, installed)
    if resolved is None:
        return

    set_model_override(context.workspace, resolved)
    context.echo(f"[green]Now using[/green] {resolved}")
    _warn_if_shadowed(context, resolved)


def _resolve(context: CommandContext, choice: str, installed: list[str]) -> str | None:
    """Turn what was typed into an installed model name, or explain why not.

    Picking by number is the point of printing a numbered list - typing a
    30-character tag by hand is where switching models stops getting used.
    """
    if choice.isdigit():
        index = int(choice)
        if not 1 <= index <= len(installed):
            context.echo(f"[red]There is no model {index}. Run /model to see the list.[/red]")
            return None
        return installed[index - 1]
    if choice in installed:
        return choice
    near = [name for name in installed if name.startswith(choice)]
    if len(near) == 1:
        return near[0]
    context.echo(f"[red]Model is not installed: {choice}[/red]")
    if near:
        context.echo("[dim]Did you mean: " + ", ".join(near) + "[/dim]")
    context.echo("[dim]/model pull <name> installs one; /model lists what you have.[/dim]")
    return None


def _warn_if_shadowed(context: CommandContext, chosen: str) -> None:
    """A ``SHAMSU_MODEL`` env pin outranks the pin just written. Say so, or the
    next turn quietly runs on a different model than the one selected."""
    source, effective = model_source()
    if effective != chosen:
        context.echo(
            f"[yellow]Note: {effective} still wins - it is "
            f"{_SOURCE_NOTE.get(source, source)}.[/yellow]"
        )


def tier(context: CommandContext, argument: str) -> None:
    """Switch the hardware tier, which sets the default model for every role."""
    requested = argument.strip().lower()
    current = active_tier()
    if not requested:
        context.echo(f"[cyan]Active tier:[/cyan] {current.value}")
        for candidate in ModelTier:
            marker = "*" if candidate is current else " "
            names = ", ".join(spec.name for spec in tier_model_specs(candidate))
            context.echo(f"  {marker} {candidate.value:8} {names}")
        context.echo("[dim]/model tier light|default|heavy[/dim]")
        return
    try:
        chosen = ModelTier(requested)
    except ValueError:
        context.echo(
            "[red]Unknown tier. Choose one of: "
            + ", ".join(t.value for t in ModelTier)
            + "[/red]"
        )
        return
    set_model_tier(context.workspace, chosen)
    context.echo(f"[green]Switched to the {chosen.value} tier.[/green]")
    if active_model_override():
        context.echo("[dim]A workspace model pin is still set; /model use tier clears it.[/dim]")
    status = collect_status()
    missing = [
        spec.name
        for spec in tier_model_specs(chosen)
        if spec.required and spec.name not in status.installed_models
    ]
    if missing:
        context.echo("[yellow]Not installed yet:[/yellow] " + ", ".join(missing))
        context.echo(f"[dim]/model pull {missing[0]}[/dim]")


def pull(context: CommandContext, argument: str) -> None:
    """Download a model. Typing the command is the consent - no second prompt."""
    status = collect_status()
    if not status.ollama_found:
        context.echo("[red]Ollama was not found, so nothing can be downloaded.[/red]")
        return
    if not status.server_running:
        context.echo("[yellow]Ollama is not running. Start it with: ollama serve[/yellow]")
        return
    wanted = [argument.strip()] if argument.strip() else list(status.missing_models)
    if not wanted:
        context.echo("[green]Every model the active tier needs is already installed.[/green]")
        return
    executable = Path(status.ollama_path)
    write = context.console.file.write
    for model in wanted:
        context.echo(f"[cyan]Downloading[/cyan] {model} ...")
        code = pull_model_streaming(executable, model, progress_callback=write)
        if code == 0:
            context.echo(f"\n[green]Installed[/green] {model}")
        else:
            context.echo(f"\n[red]Download failed for {model} (exit {code}).[/red]")


def unload(context: CommandContext, argument: str) -> None:
    """Evict a model from VRAM.

    The reason switching models on an 8GB card needs a verb at all: two anchors
    cannot be co-resident, so the old one has to go before the new one fits.
    """
    loaded = list_loaded_models()
    target = argument.strip() or (loaded[0] if loaded else "")
    if not target:
        context.echo("[dim]No model is currently loaded.[/dim]")
        return
    if unload_model(target):
        context.echo(f"[green]Unloaded[/green] {target}")
    else:
        context.echo(f"[yellow]Could not unload {target} - it may not be loaded.[/yellow]")


_SUBCOMMANDS = {
    "use": use,
    "tier": tier,
    "pull": pull,
    "unload": unload,
    "status": show,
    "list": show,
}


def handle(context: CommandContext, argument: str) -> None:
    verb, _, rest = argument.strip().partition(" ")
    action = _SUBCOMMANDS.get(verb.lower())
    if action is not None:
        action(context, rest.strip())
        return
    # `/model qwen3:8b` is what people actually type, and there is nothing else
    # a bare argument could reasonably mean.
    if argument.strip():
        use(context, argument)
    else:
        show(context)


COMMANDS = (
    Command(
        name="/model",
        aliases=("/models",),
        summary="Show or switch the model (use / tier / pull / unload)",
        handler=handle,
        completions=(
            "use ",
            "use tier",
            "tier light",
            "tier default",
            "tier heavy",
            "pull ",
            "unload",
        ),
    ),
)
