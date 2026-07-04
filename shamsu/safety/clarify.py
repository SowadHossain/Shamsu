"""Free-text clarifying question, distinct from the binary approve/deny gate.

ask_approval() answers "is this specific action OK?" — a yes/no gate.
ask_clarifying_question() is for when the agent is genuinely stuck (e.g. a
long-running loop keeps repeating the same action with no progress) and
needs open-ended input to proceed, not a permission decision.
"""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

from shamsu.safety.approval import _pause_console_live


def ask_clarifying_question(question: str, console: Console | None = None) -> str:
    console = console or Console()
    console.print(Panel(question, title="Need Input", border_style="cyan"))
    _pause_console_live(console)
    return input("Your answer: ").strip()
