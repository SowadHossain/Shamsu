"""
User approval prompt for risky actions.
"""
from __future__ import annotations

import sys

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
_MAX_EMPTY_TTY_READS = 3


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

    answer = _read_approval_answer(console)
    if answer is None:
        return False, "none"
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


_TIER_ANSWERS = {"1": "light", "2": "default", "3": "heavy"}


def ask_tier_choice(console: Console | None = None) -> str | None:
    """First-run model tier picker (light/default/heavy).

    Returns the chosen tier name, or None if no interactive answer could be
    obtained (piped/non-interactive stdin, closed input, or repeated
    unrecognized answers) - the caller should silently fall back to the
    default tier rather than blocking startup."""
    console = console or Console()
    console.print(
        Panel(
            "  1. light   - ~3B models, runs on 8GB RAM with no GPU\n"
            "  2. default - ~7-8B models, the original 8GB cookbook (recommended)\n"
            "  3. heavy   - up to 14B models, needs 16GB+ RAM\n\n"
            "You can change this later with `/models tier light|default|heavy`.",
            title="Choose a Model Tier",
            border_style="cyan",
        )
    )
    if not console.is_terminal:
        return None
    console.print("Which tier? [1/2/3]")
    _pause_console_live(console)
    empty_reads = 0
    while True:
        answer = _prompt_toolkit_answer()
        if answer is None:
            try:
                answer = input("> ").strip().lower()
            except EOFError:
                console.print("[yellow]No input available. Defaulting to the default tier.[/yellow]")
                return None
        if answer in _TIER_ANSWERS:
            return _TIER_ANSWERS[answer]
        if answer in {"light", "default", "heavy"}:
            return answer
        if not answer:
            empty_reads += 1
            if empty_reads >= _MAX_EMPTY_TTY_READS:
                console.print("[yellow]No answer received. Defaulting to the default tier.[/yellow]")
                return None
            continue
        console.print("[yellow]Please choose 1, 2, or 3.[/yellow]")


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
    try:
        answer = input("> ").strip().lower()
    except EOFError:
        console.print("[yellow]Approval input was closed. Not remembering this choice.[/yellow]")
        return "none"
    if answer in {"2", "s", "session"}:
        return "session"
    if answer in {"3", "w", "workspace"}:
        return "workspace"
    return "none"


def _prompt_toolkit_answer() -> str | None:
    """Read one line via prompt_toolkit.

    SHAMSU's launcher runs Python so that built-in ``input()`` can see a
    non-interactive stdin (prompt_toolkit drives the Windows console directly),
    which made ``input()`` return an empty string and silently cancel approval
    menus. prompt_toolkit uses the same reliable console path as the main REPL
    prompt. Returns the stripped, lowercased answer, or None if prompt_toolkit
    is unavailable or the read was interrupted/closed.
    """
    try:
        from prompt_toolkit import prompt as ptk_prompt
    except Exception:
        return None
    try:
        return ptk_prompt("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    except Exception:
        return None


def _read_approval_answer(console: Console) -> str | None:
    """Read a menu answer reliably, without crashing on closed stdin.

    On an interactive terminal, read through prompt_toolkit — it drives the
    console directly and works even when built-in ``input()`` sees a
    non-interactive stdin (the cause of approval menus cancelling without
    waiting for a keypress). Falls back to the Windows single-key reader, then
    to ``input()`` for non-interactive/piped test contexts where an empty string
    means "No".
    """
    _pause_console_live(console)
    if console.is_terminal:
        answer = _prompt_toolkit_answer()
        if answer is not None:
            return answer
        fallback = _read_windows_console_answer(console)
        if fallback is not None:
            return fallback

    empty_reads = 0
    while True:
        _pause_console_live(console)
        try:
            answer = input("> ").strip().lower()
        except EOFError:
            fallback = _read_windows_console_answer(console)
            if fallback is not None:
                return fallback
            console.print("[yellow]Approval input was closed. Action cancelled.[/yellow]")
            return None
        if answer or not sys.stdin.isatty():
            return answer
        empty_reads += 1
        if empty_reads >= _MAX_EMPTY_TTY_READS:
            console.print("[yellow]No approval answer was received. Action cancelled.[/yellow]")
            return None
        console.print("[yellow]Please choose 1 or 2.[/yellow]")


def _read_windows_console_answer(console: Console) -> str | None:
    """Fallback for Windows terminals where prompt-toolkit closes stdin.

    After prompt-toolkit has owned the console, built-in ``input()`` can raise
    EOF even though the terminal is still interactive. ``msvcrt.getwch`` reads a
    single key directly from the Windows console, which is enough for SHAMSU's
    numbered approval menus.
    """
    if sys.platform != "win32" or not console.is_terminal:
        return None
    try:
        import msvcrt
    except ImportError:
        return None
    console.print("[dim]Press 1 to approve or 2 to cancel.[/dim]")
    while True:
        try:
            char = msvcrt.getwch()
        except (OSError, EOFError):
            return None
        if char in {"\x00", "\xe0"}:
            try:
                msvcrt.getwch()
            except (OSError, EOFError):
                pass
            continue
        lowered = char.lower()
        if lowered in {"\x03", "\x1a"}:  # Ctrl+C / Ctrl+Z
            return None
        if lowered == "\x1b":  # Esc
            console.print("2")
            return "2"
        if lowered in {"1", "2", "3", "y", "n"}:
            console.print(char)
            return lowered
