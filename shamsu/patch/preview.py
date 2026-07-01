"""
Rich rendering helpers for unified diff previews.
"""
from __future__ import annotations

from rich.console import Console, Group
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from shamsu.patch.engine import parse_unified_diff
from shamsu.safety.sandbox import Sandbox


def build_diff_preview(diff_text: str, sandbox: Sandbox | None = None) -> Panel:
    patches = parse_unified_diff(diff_text, sandbox)

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(justify="right", style="green")
    table.add_column(justify="right", style="red")
    table.add_row("File", "+", "-")
    for patch in patches:
        table.add_row(patch.display_path, str(patch.additions), str(patch.deletions))

    syntax = Syntax(diff_text, "diff", theme="ansi_dark", word_wrap=False)
    return Panel(Group(table, Text(), syntax), title="Patch Preview", border_style="cyan")


def print_diff_preview(
    diff_text: str,
    console: Console | None = None,
    sandbox: Sandbox | None = None,
) -> None:
    (console or Console()).print(build_diff_preview(diff_text, sandbox))
