"""Local `/remote_control` command support for the REPL."""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from shamsu.integrations.telegram.service import TelegramService


class LocalTelegramBridgeManager:
    def __init__(self) -> None:
        self._service: TelegramService | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._workspace: Path | None = None
        self._lock = threading.Lock()

    def service_for(self, workspace: Path) -> TelegramService:
        resolved = Path(workspace).resolve()
        with self._lock:
            if self._service is None or self._workspace != resolved:
                self._service = TelegramService(resolved)
                self._workspace = resolved
            return self._service

    def start(self, workspace: Path) -> None:
        service = self.service_for(workspace)
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
    subcommand = parts[1].strip().lower() if len(parts) > 1 else ""
    if subcommand == "disconnect":
        panel = _MANAGER.service_for(workspace).local_panel("disconnect")
        _MANAGER.stop()
        console.print(Panel(panel.message, title="Remote Control"))
        return
    service = _MANAGER.service_for(workspace)
    panel = service.local_panel(subcommand)
    if subcommand in {"", "connect", "repair"}:
        _MANAGER.start(workspace)
    console.print(Panel(panel.message, title="Remote Control"))

