"""
User approval prompt for risky actions.
"""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from shamsu.types import ApprovalRequest

# Friendly labels for the "don't ask again" menu line.
_ACTION_LABELS = {
    "file_write": "file writes",
    "file_edit": "file edits",
}

_YES_ANSWERS = {"1", "y", "yes"}


def _action_label(action_type: str) -> str:
    return _ACTION_LABELS.get(action_type, action_type)


def _render_request(request: ApprovalRequest, console: Console) -> None:
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


def _pause_console_live(console: Console) -> None:
    """Stop Rich's active Live/status before blocking on stdin.

    Rich Live/status rendering and Python's built-in input() can fight over a
    real interactive TTY. If approval is requested while a spinner is active,
    input() may read an empty value immediately. The outer status context will
    stop again on exit; Rich's stop is safe to call twice.
    """
    live = getattr(console, "_live", None)
    if live is None:
        live = None
    targets = []
    if live is not None:
        targets.append(live)
    targets.extend(getattr(console, "_shamsu_active_statuses", []))
    for target in reversed(targets):
        try:
            target.stop()
        except Exception:
            pass


def ask_approval_menu(
    request: ApprovalRequest,
    offer_remember: bool = False,
    console: Console | None = None,
) -> tuple[bool, str]:
    """Claude-Code-style numbered approval menu.

    Returns (approved, remember_scope). remember_scope is "workspace" when the
    user picks the "don't ask again" option, else "none". `offer_remember`
    should only be True for actions that are actually auto-approvable, so the
    "don't ask again" option never appears for commands/deletes/etc.
    """
    console = console or Console()
    _render_request(request, console)

    console.print("Do you want to proceed?")
    console.print("  1. Yes")
    if offer_remember:
        console.print(f"  2. Yes, and don't ask again for {_action_label(request.action_type)} in this workspace")
        console.print("  3. No")
        no_answers = {"3", "n", "no", ""}
        remember_answer = "2"
    else:
        console.print("  2. No")
        no_answers = {"2", "n", "no", ""}
        remember_answer = None

    _pause_console_live(console)
    answer = input("> ").strip().lower()
    if offer_remember and answer == remember_answer:
        return True, "workspace"
    if answer in _YES_ANSWERS:
        return True, "none"
    if answer in no_answers:
        return False, "none"
    # Anything unrecognized is treated as "no" — the safe default.
    return False, "none"


def ask_approval(request: ApprovalRequest, console: Console | None = None) -> bool:
    """Binary approval prompt (numbered menu, no remember option).

    Kept as the default `approval_func` signature everywhere; returns just a
    bool. The remember-folding single menu is `ask_approval_menu`, used by the
    interactive REPL via ApprovalManager.
    """
    approved, _scope = ask_approval_menu(request, offer_remember=False, console=console)
    return approved


def ask_remember_choice(action_type: str, console: Console | None = None) -> str:
    """Ask whether to auto-approve future actions of this type.

    Returns "session", "workspace", or "none". Retained for callers/tests that
    use the separate two-step remember flow; the interactive REPL now folds
    this into `ask_approval_menu` instead.
    """
    console = console or Console()
    console.print(f"Remember this choice for future '{action_type}' actions?")
    console.print("  1. No, ask me again")
    console.print("  2. Yes, for this session")
    console.print("  3. Yes, for this workspace (saved)")
    _pause_console_live(console)
    answer = input("> ").strip().lower()
    if answer in {"2", "s", "session"}:
        return "session"
    if answer in {"3", "w", "workspace"}:
        return "workspace"
    return "none"
