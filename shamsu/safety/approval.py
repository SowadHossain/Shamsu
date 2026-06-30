"""
User approval prompt for risky actions.
"""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from shamsu.types import ApprovalRequest


def ask_approval(request: ApprovalRequest, console: Console | None = None) -> bool:
    console = console or Console()
    body = Text()
    body.append(f"Action: {request.action_type}\n", style="bold")
    body.append(f"Risk: {request.risk_level}\n")
    if request.working_dir:
        body.append(f"Working dir: {request.working_dir}\n")
    if request.reason:
        body.append(f"Reason: {request.reason}\n")
    body.append(f"\n{request.description}")
    if request.preview:
        body.append("\n\nPreview:\n", style="bold")
        body.append(request.preview)

    console.print(Panel(body, title="Approval Required", border_style="yellow"))
    answer = input("Proceed? [y/N] ").strip().lower()
    return answer in {"y", "yes"}
