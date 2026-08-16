"""Local `/remote_control` command support for the REPL."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from shamsu.integrations.telegram.service import TelegramService, configure_telegram_bot_token
from shamsu.safety.commands import redact


class ConsoleTelegramMirror:
    def __init__(self, console: Console) -> None:
        self.console = console
        self._lock = threading.Lock()

    def __call__(self, title: str, text: str) -> None:
        body = _mirror_text(text)
        with self._lock:
            self.console.print()
            self.console.print(Panel(body, title=title, border_style="cyan"))


class LocalTelegramBridgeManager:
    def __init__(self) -> None:
        self._service: TelegramService | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._workspace: Path | None = None
        self._mirror: ConsoleTelegramMirror | None = None
        self._lock = threading.Lock()

    def service_for(self, workspace: Path, console: Console | None = None) -> TelegramService:
        resolved = Path(workspace).resolve()
        with self._lock:
            if console is not None:
                self._mirror = ConsoleTelegramMirror(console)
            if self._service is None or self._workspace != resolved:
                self._service = TelegramService(resolved, cli_mirror=self._mirror)
                self._workspace = resolved
            elif self._mirror is not None:
                self._service.set_cli_mirror(self._mirror)
            return self._service

    def reload_service(self, workspace: Path, console: Console | None = None) -> TelegramService:
        resolved = Path(workspace).resolve()
        with self._lock:
            if console is not None:
                self._mirror = ConsoleTelegramMirror(console)
            self._service = TelegramService(resolved, cli_mirror=self._mirror)
            self._workspace = resolved
            return self._service

    def start(self, workspace: Path, console: Console | None = None) -> None:
        service = self.service_for(workspace, console)
        if service.transport is None:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_loop,
                args=(self._loop, service),
                name="shamsu-telegram-remote",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            loop = self._loop
            service = self._service
        if loop is None or service is None:
            return
        future = asyncio.run_coroutine_threadsafe(service.stop(), loop)
        try:
            future.result(timeout=5)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)

    @staticmethod
    def _run_loop(loop: asyncio.AbstractEventLoop, service: TelegramService) -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(service.start())
        loop.run_forever()


_MANAGER = LocalTelegramBridgeManager()


def handle_remote_control_command(user_input: str, workspace: Path, console: Console) -> None:
    parts = user_input.split(maxsplit=1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    subcommand = rest.lower()
    if subcommand.startswith("configure"):
        token = rest.partition(" ")[2].strip()
        if not token:
            console.print(
                Panel(
                    "Usage:\n\n/remote_control configure <bot-token>\n\n"
                    "The token will be saved to .shamsu/telegram.env and will not be displayed.",
                    title="Remote Control",
                    border_style="yellow",
                )
            )
            return
        try:
            path = configure_telegram_bot_token(workspace, token)
        except ValueError as exc:
            console.print(Panel(str(exc), title="Remote Control", border_style="red"))
            return
        _MANAGER.stop()
        service = _MANAGER.reload_service(workspace, console)
        _MANAGER.start(workspace, console)
        panel = service.local_panel("status")
        console.print(
            Panel(
                f"Telegram bot token saved to {path}.\n\n{panel.message}",
                title="Remote Control",
                border_style="green",
            )
        )
        return
    if subcommand == "disconnect":
        panel = _MANAGER.service_for(workspace, console).local_panel("disconnect")
        _MANAGER.stop()
        console.print(Panel(panel.message, title="Remote Control"))
        return
    service = _MANAGER.service_for(workspace, console)
    panel = service.local_panel(subcommand)
    if subcommand in {"", "connect", "repair"}:
        _MANAGER.start(workspace, console)
    console.print(Panel(panel.message, title="Remote Control"))


def redact_remote_control_command(text: str) -> str:
    stripped = (text or "").strip()
    lowered = stripped.lower()
    prefixes = ("/remote_control configure", "remote_control configure")
    if not any(lowered.startswith(prefix) for prefix in prefixes):
        return text
    head = stripped.split(maxsplit=2)[:2]
    if len(head) < 2:
        return text
    prefix = " ".join(head)
    return f"{prefix} [REDACTED]"


def _mirror_text(text: str) -> str:
    clean = redact(text or "").strip()
    if len(clean) <= 1200:
        return clean or "-"
    return clean[:1200].rstrip() + "\n\n[truncated for CLI mirror]"
