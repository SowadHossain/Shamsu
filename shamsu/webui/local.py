"""The `/web` REPL command.

One portal per REPL process, in a daemon thread, exactly as the Telegram
bridge does it (`integrations/telegram/local.py`). That shape is a workaround,
not a design: it exists because `run_control._RUNS` is a module-level dict, so
sharing the live run state means sharing the process. When the control plane
lands, the portal can move out into `shamsu serve` and this becomes the
fallback rather than the only option.

The token is printed exactly once, as part of the URL, and never again - not in
`/web status`, not in a log. A URL that has scrolled off is recovered by
restarting the portal, which mints a new one.
"""
from __future__ import annotations

import threading
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from shamsu.webui.server import DEFAULT_PORT, WebPortal


class WebPortalManager:
    def __init__(self) -> None:
        self._portal: WebPortal | None = None
        self._lock = threading.Lock()

    def start(self, workspace: Path, port: int = DEFAULT_PORT) -> tuple[WebPortal, bool]:
        """Returns the portal and whether this call is what started it."""
        with self._lock:
            if self._portal is not None:
                return self._portal, False
            portal = WebPortal(workspace, port=port)
            portal.start()
            self._portal = portal
            return portal, True

    def stop(self) -> bool:
        with self._lock:
            portal, self._portal = self._portal, None
        if portal is None:
            return False
        portal.stop()
        return True

    @property
    def running(self) -> WebPortal | None:
        return self._portal


_MANAGER = WebPortalManager()


def handle_web_command(user_input: str, workspace: Path, console: Console) -> None:
    parts = (user_input or "").split()
    subcommand = parts[1].lower() if len(parts) > 1 else "start"
    port = _port_from(parts)

    if subcommand in {"stop", "off"}:
        stopped = _MANAGER.stop()
        console.print(
            Panel(
                "Web view stopped." if stopped else "The web view was not running.",
                title="Web View",
            )
        )
        return

    if subcommand == "status":
        portal = _MANAGER.running
        console.print(
            Panel(
                f"Running at {portal.base_url}\n\n"
                "The access token is only shown when the portal starts. "
                "Run `/web stop` then `/web` to get a fresh link."
                if portal
                else "Not running. Start it with `/web`.",
                title="Web View",
            )
        )
        return

    if subcommand not in {"start", "on"} and not subcommand.isdigit():
        console.print(
            Panel(
                "Usage:\n\n"
                "  /web [port]     Start the local web view\n"
                "  /web status     Show whether it is running\n"
                "  /web stop       Stop it",
                title="Web View",
                border_style="yellow",
            )
        )
        return

    try:
        portal, started = _MANAGER.start(workspace, port)
    except OSError as exc:
        console.print(
            Panel(
                f"Could not start the web view: {exc}\n\n"
                f"Port {port} may already be in use - try `/web {port + 1}`.",
                title="Web View",
                border_style="red",
            )
        )
        return

    if not started:
        console.print(
            Panel(
                f"Already running at {portal.base_url}.\n"
                "Use `/web stop` first if you want a new link.",
                title="Web View",
            )
        )
        return

    console.print(
        Panel(
            f"Web view: {portal.url}\n\n"
            "Loopback only, and the link above carries a one-time access token "
            "for this run.\n"
            "It is read-only: it follows runs started from the CLI or Telegram, "
            "and cannot start or stop one.",
            title="Web View",
            border_style="green",
        )
    )


def stop_web_portal() -> bool:
    """Used on REPL shutdown, so the thread does not outlive the console."""
    return _MANAGER.stop()


def _port_from(parts: list[str]) -> int:
    for part in parts[1:]:
        if part.isdigit():
            return int(part)
    return DEFAULT_PORT
