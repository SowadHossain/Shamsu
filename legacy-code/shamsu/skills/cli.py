"""Read-only skill inspection commands."""
from __future__ import annotations

import difflib
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from shamsu.skills.loader import discover_skills, skills_mode_from_env
from shamsu.skills.selector import render_skill_context, select_skills_for_task
from shamsu.skills.types import SkillCatalog


def handle_skills_command(user_input: str, workspace: Path, console: Console) -> None:
    parts = user_input.split(maxsplit=2)
    command = parts[1].strip().lower() if len(parts) > 1 else "list"
    catalog = discover_skills(workspace)

    if command in {"list", "status", ""}:
        _print_skill_list(catalog, console)
        return
    if command == "show":
        if len(parts) < 3 or not parts[2].strip():
            console.print("[red]Usage: /skills show <name>[/red]")
            return
        _print_skill_detail(catalog, parts[2].strip(), console)
        return
    if command in {"explain", "suggest"}:
        if len(parts) < 3 or not parts[2].strip():
            console.print("[red]Usage: /skills explain <prompt>[/red]")
            return
        _print_skill_explanation(workspace, parts[2].strip(), catalog, console)
        return
    console.print("[red]Usage: /skills [list|show <name>|explain <prompt>|suggest <prompt>][/red]")


def _print_skill_list(catalog: SkillCatalog, console: Console) -> None:
    table = Table(title=f"SHAMSU Skills (mode: {skills_mode_from_env()})")
    table.add_column("Name")
    table.add_column("Source")
    table.add_column("Tags")
    table.add_column("Description")
    for skill in catalog.sorted_skills():
        table.add_row(skill.name, skill.source, ", ".join(skill.tags), skill.description)
    console.print(table)
    if catalog.issues:
        console.print("[yellow]Skill issues:[/yellow]")
        for issue in catalog.issues:
            console.print(f"- {issue.severity}: {issue.path}: {issue.message}")


def _print_skill_detail(catalog: SkillCatalog, name: str, console: Console) -> None:
    skill = catalog.skills.get(name)
    if skill is None:
        console.print(f"[red]Skill not found: {name}[/red]")
        suggestions = difflib.get_close_matches(name, sorted(catalog.skills), n=5, cutoff=0.45)
        if suggestions:
            console.print("[yellow]Did you mean:[/yellow] " + ", ".join(suggestions))
        else:
            console.print("[dim]Run `/skills list` to see available skill names.[/dim]")
        return
    metadata = skill.to_summary()
    detail = "\n".join(
        [
            f"Name: {skill.name}",
            f"Source: {skill.source}",
            f"Path: {skill.skill_path}",
            f"Version: {skill.version}",
            f"Tags: {', '.join(skill.tags) or '-'}",
            f"Triggers: {', '.join(skill.triggers) or '-'}",
            f"Dependencies: {', '.join(skill.dependencies) or '-'}",
            f"Required MCP: {', '.join(skill.required_mcp) or '-'}",
            "",
            skill.instructions.strip(),
        ]
    )
    console.print(Panel(detail, title=f"Skill: {skill.name}", border_style="cyan"))
    if metadata.get("allowed_tools"):
        console.print(
            "[dim]Allowed tools are restrictions only; they do not bypass approvals.[/dim]"
        )


def _print_skill_explanation(
    workspace: Path,
    prompt: str,
    catalog: SkillCatalog,
    console: Console,
) -> None:
    selection = select_skills_for_task(workspace, prompt, catalog=catalog)
    if not selection.selected:
        console.print("[dim]No skills selected for that prompt.[/dim]")
        return
    table = Table(title=f"Skill Selection (mode: {selection.mode})")
    table.add_column("Skill")
    table.add_column("Score")
    table.add_column("Why")
    for item in selection.selected:
        table.add_row(item.skill.name, f"{item.score:.1f}", "; ".join(item.reasons))
    console.print(table)
    context = render_skill_context(selection)
    if context:
        console.print(Panel(context, title="Rendered Skill Context", border_style="cyan"))
