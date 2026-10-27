"""`shamsu web` - serve the local web view without opening a REPL.

Worth being clear about why this is useful rather than just convenient. The
portal reads `activity.jsonl` **from disk**, not `run_control._RUNS` from
memory. So a standalone `shamsu web` in one terminal shows you, live, a turn
being driven by a REPL in another terminal - or by the Telegram bot - with no
shared process and no control plane. Watching already crosses process
boundaries; only *controlling* a run does not, which is exactly the line this
view is drawn along.

It blocks. The server runs on a daemon thread, so a process that returned would
take the portal down with it. Ctrl-C stops it.
"""
from __future__ import annotations

import signal
import sys
import threading
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from shamsu.runtime.workspaces import discover_workspaces, remember_all
from shamsu.webui import api
from shamsu.webui.server import DEFAULT_PORT, WebPortal

__all__ = ["serve", "DEFAULT_PORT"]


def _watching(portal: WebPortal) -> str:
    """What the portal can see, said in a way that is useful when it is nothing.

    An empty list is the interesting case: it means SHAMSU has not been opened
    anywhere this install knows about, and the fix is `--scan`, so say so here
    rather than showing a blank pane and letting the user guess.
    """
    known = portal.workspaces()
    total = sum(api.workspace_summary(item)["session_count"] for item in known)
    if not known:
        return (
            "No projects yet. Add a project in the browser to start working."
        )
    return f"Watching {len(known)} workspace(s), {total} thread(s)."


def _flush(console: Console) -> None:
    for stream in (getattr(console, "file", None), sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except (AttributeError, ValueError, OSError):
            pass


def serve(
    workspace: Path | None = None,
    port: int = DEFAULT_PORT,
    console: Console | None = None,
    stop: threading.Event | None = None,
    scan: list[str] | None = None,
    *,
    host: str | None = None,
    token: str | None = None,
    runner=None,
) -> int:
    """Serve until `stop` is set, Ctrl-C, or SIGTERM.

    `stop` is injectable so a caller - a test, or `shamsu serve` later - can
    shut the portal down without a signal. It is the only thing this loop waits
    on, so patching anything more general would reach into the HTTP server's
    own machinery.
    """
    console = console or Console()
    found: list[Path] = []
    for root in scan or []:
        discovered = discover_workspaces(root)
        remember_all(discovered)
        found.extend(discovered)
        console.print(
            f"[dim]Scanned {root}: found {len(discovered)} workspace(s).[/dim]"
        )
    options = {key: value for key, value in {"host": host, "token": token, "runner": runner}.items() if value is not None}
    portal = WebPortal(workspace, port=port, **options)
    try:
        portal.start()
    except OSError as exc:
        console.print(
            Panel(
                f"Could not start the web view: {exc}\n\n"
                f"Port {port} is probably in use. Try `shamsu web --port {port + 1}`, "
                "or `--port 0` to let the OS pick one.",
                title="Web View",
                border_style="red",
            )
        )
        return 2

    worker = runner or getattr(portal, "runner", None)
    if worker is not None:
        worker.start()

    console.print(
        Panel(
            f"[bold]{portal.url}[/bold]\n\n"
            f"{_watching(portal)}\n"
            + (
                "Loopback only, so the plain link is the whole thing - no "
                "token, nothing to re-copy.\n\n"
                if not portal.requires_token
                else "This bind is reachable beyond this machine, so the link "
                "carries an access token. Keep this link private.\n\n"
            )
            # It was never read-only. `POST .../prompt` has always started a
            # turn, and saying otherwise made the portal look safer than it is.
            + "The web service owns its coding worker and can start one of its own runs. No interactive CLI is required. "
            "Tasks keep running when the browser is closed.\n\n"
            "Ctrl-C to stop.",
            title="SHAMSU Web View",
            border_style="green",
        )
    )
    # Flushed explicitly: this process then blocks forever, and stdout is
    # block-buffered whenever it is not a terminal. Without this, anyone who
    # pipes or redirects the output - `shamsu web | tee`, a wrapper script, a
    # service manager - never sees the URL at all, because the buffer is not
    # written until a process that never exits exits.
    _flush(console)

    stop = stop or threading.Event()

    def _handle_signal(_signum, _frame) -> None:
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        handler = getattr(signal, name, None)
        if handler is not None:
            try:
                signal.signal(handler, _handle_signal)
            except (ValueError, OSError):
                # Not the main thread, or a platform without it. Ctrl-C still
                # raises KeyboardInterrupt below.
                pass
    try:
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        if worker is not None:
            worker.stop()
        portal.stop()
    console.print("[dim]Web view stopped.[/dim]")
    return 0
