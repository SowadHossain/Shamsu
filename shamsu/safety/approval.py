"""
User approval prompt for risky actions.
"""
from __future__ import annotations

import contextlib
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

_MAX_EMPTY_TTY_READS = 3

# Live displays stopped for a prompt, keyed by console, so the same prompt can
# hand them back afterwards.
_PAUSED: dict[int, list] = {}

# Non-zero while a prompt is waiting on a human. Anything that PAINTS - the
# thinking spinner, the tool heartbeat - must hold off, or it redraws over the
# question and over the half-typed answer. Live 2026-08-18 the 5s heartbeat did
# exactly that: answering inside 5s worked, waiting did not.
_PROMPT_DEPTH = 0


def prompt_is_active() -> bool:
    """True while SHAMSU is blocked waiting for a typed answer."""
    return _PROMPT_DEPTH > 0


#: Things that must stand down before a prompt is drawn, newest last.
#:
#: `prompt_is_active()` alone is a flag someone has to notice, and the live
#: console noticing it by polling leaves a window - the flag goes up, and until
#: the next poll two prompt_toolkit applications are live on one terminal,
#: which is not a thing prompt_toolkit supports. This is the synchronous half:
#: whoever is holding the console is TOLD, on this thread, before the question
#: is rendered, and rendering the question then buys the event loop the moment
#: it needs to act on it.
_ON_PROMPT_OPEN: list = []


def on_prompt_open(callback):
    """Register `callback`, called just before any prompt takes the terminal.

    Returns a function that unregisters it. The callback may be invoked from a
    WORKER thread - tools run on one - so it must not touch an event loop
    directly; see `LiveConsole.stand_down`.
    """
    _ON_PROMPT_OPEN.append(callback)

    def release() -> None:
        with contextlib.suppress(ValueError):
            _ON_PROMPT_OPEN.remove(callback)

    return release


@contextlib.contextmanager
def reading_input():
    """Mark the window in which nothing else may write to the terminal."""
    global _PROMPT_DEPTH
    _PROMPT_DEPTH += 1
    # Copied on purpose: a callback that unregisters itself would otherwise
    # mutate the list mid-iteration.
    for callback in list(_ON_PROMPT_OPEN):  # noqa: PERF101
        # A broken observer must never be the reason a human cannot be asked.
        with contextlib.suppress(Exception):
            callback()
    try:
        yield
    finally:
        _PROMPT_DEPTH = max(0, _PROMPT_DEPTH - 1)


def _action_label(action_type: str) -> str:
    return _ACTION_LABELS.get(action_type, action_type)


def _render_request(request: ApprovalRequest, console: Console) -> None:
    body = Text()
    body.append(f"Action: {request.action_type}\n", style="bold")
    body.append(f"Risk: {request.risk_level}\n")
    if request.working_dir:
        body.append(f"Working dir: {request.working_dir}\n")
    if request.target_paths:
        body.append("Targets:\n")
        for path in request.target_paths:
            body.append(f"  - {path}\n")
    if request.reason:
        body.append(f"Reason: {request.reason}\n")
    body.append(f"\n{request.description}")
    if request.preview:
        body.append("\n\nPreview:\n", style="bold")
        body.append(request.preview)
    console.print(Panel(body, title="Approval Required", border_style="yellow"))


def _resume_console_live(paused: list) -> None:
    """Restart what `_pause_console_live` stopped.

    Without this the spinner never comes back after an approval, so the rest of
    a turn runs with no sign of life - the same "is it stuck?" the pause was
    added to fix, just moved later.
    """
    for target in paused:
        try:
            target.start()
        except Exception:
            pass


def _pause_console_live(console: Console) -> list:
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
    stopped = []
    for target in reversed(targets):
        try:
            if getattr(target, "is_started", True):
                target.stop()
                stopped.append(target)
        except Exception:
            pass
    return stopped


def ask_approval_menu(
    request: ApprovalRequest,
    offer_remember: bool = False,
    console: Console | None = None,
) -> tuple[bool, str]:
    """Prompt for one stable semantic approval decision.

    Returns (approved, remember_scope). remember_scope is "workspace" when the
    user picks the "don't ask again" option, else "none". `offer_remember`
    should only be True for actions that are actually auto-approvable, so the
    "don't ask again" option never appears for commands/deletes/etc.
    """
    console = console or Console()
    _render_request(request, console)

    console.print("Do you want to proceed?")
    console.print("  [y] Allow once", markup=False)
    if offer_remember:
        console.print(
            f"  [a] Always allow {_action_label(request.action_type)} "
            "in this workspace",
            markup=False,
        )
    console.print("  [n] Deny", markup=False)

    answer = _read_approval_answer(console)
    _resume_console_live(_PAUSED.pop(id(console), []))
    if answer is None:
        return False, "none"
    if offer_remember and answer in {"a", "always"}:
        return True, "workspace"
    if answer in {"y", "yes"}:
        return True, "none"
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
    _PAUSED[id(console)] = _pause_console_live(console)
    with reading_input():
        if console.is_terminal:
            answer = _prompt_toolkit_answer()
            if answer is not None:
                return answer
            fallback = _read_windows_console_answer(console)
            if fallback is not None:
                return fallback

        # A second pass, because the branch above may have started something
        # back up. Accumulate rather than overwrite - dropping the first list
        # would leave the spinner stopped for the rest of the turn.
        _PAUSED.setdefault(id(console), []).extend(_pause_console_live(console))
        try:
            return input("> ").strip().lower()
        except EOFError:
            fallback = _read_windows_console_answer(console)
            if fallback is not None:
                return fallback
            console.print("[yellow]Approval input was closed. Action cancelled.[/yellow]")
            return None


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
    console.print("[dim]Press y to allow once, a to always allow when offered, or n to deny.[/dim]")
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
            console.print("n")
            return "n"
        if lowered in {"y", "a", "n"}:
            console.print(char)
            return lowered
        # Anything else used to loop here in total silence - Enter, a stray
        # arrow key, a paste - and a prompt that ignores you without a word is
        # indistinguishable from a hung one. Say so, and keep waiting.
        if char.isprintable() and char.strip():
            console.print(f"[dim]'{char}' is not an option - press y, a, or n.[/dim]")
        else:
            console.print("[dim]Press y, a, or n (no Enter needed).[/dim]")
