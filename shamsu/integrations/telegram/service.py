"""Lifecycle owner for Telegram remote control."""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shamsu.integrations.telegram.authentication import TelegramAuthenticator
from shamsu.integrations.telegram.approvals import TelegramApprovalBroker
from shamsu.integrations.telegram.callbacks import CallbackRegistry
from shamsu.integrations.telegram.controller import TelegramController
from shamsu.integrations.telegram.formatter import TelegramFormatter
from shamsu.integrations.telegram.models import OutboundMessage, RemoteControlStatus
from shamsu.integrations.telegram.pairing import PairingCode, PairingManager
from shamsu.integrations.telegram.sessions import LocalShamsuSessionGateway, SessionGateway
from shamsu.integrations.telegram.storage import TelegramStateStore
from shamsu.integrations.telegram.transport import TelegramBotApiTransport, TelegramTransport

TOKEN_ENV_VAR = "SHAMSU_TELEGRAM_BOT_TOKEN"


@dataclass(frozen=True)
class RemoteControlPanel:
    status: RemoteControlStatus
    message: str
    pairing: PairingCode | None = None


class TelegramService:
    def __init__(
        self,
        workspace: Path,
        *,
        transport: TelegramTransport | None = None,
        token: str | None = None,
        store: TelegramStateStore | None = None,
        gateway: SessionGateway | None = None,
        installation_id: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store or TelegramStateStore(self.workspace)
        self.installation_id = installation_id or self._installation_id()
        self.formatter = TelegramFormatter()
        self.callbacks = CallbackRegistry(self.store)
        self.pairing = PairingManager(self.store, installation_id=self.installation_id)
        self.authenticator = TelegramAuthenticator(self.store)
        self.status = RemoteControlStatus.DISABLED
        self.token = token if token is not None else os.environ.get(TOKEN_ENV_VAR, "")
        self._loop: asyncio.AbstractEventLoop | None = None
        self.transport = transport or (
            TelegramBotApiTransport(
                bot_token=self.token,
                offset_getter=self._offset_getter,
                offset_setter=self._offset_setter,
            )
            if self.token
            else None
        )
        self._poll_task: asyncio.Task[Any] | None = None
        self.approval_broker = TelegramApprovalBroker(
            self.store,
            self.callbacks,
            formatter=self.formatter,
            notify=self._notify_from_thread,
        )
        self.gateway = gateway or LocalShamsuSessionGateway(
            self.workspace,
            approval_broker=self.approval_broker,
        )
        self.controller = TelegramController(
            store=self.store,
            authenticator=self.authenticator,
            pairing=self.pairing,
            callbacks=self.callbacks,
            gateway=self.gateway,
            workspace=self.workspace,
            formatter=self.formatter,
            approval_resolver=self.resolve_approval_callback,
        )

    def local_panel(self, subcommand: str = "") -> RemoteControlPanel:
        subcommand = (subcommand or "").strip().lower()
        if subcommand == "disconnect":
            for user in self.store.authorized_users():
                self.store.disconnect_user(int(user["telegram_user_id"]))
            self.status = RemoteControlStatus.DISCONNECTED
            return RemoteControlPanel(self.status, "Telegram remote control disconnected.")
        if not self.token:
            return RemoteControlPanel(
                RemoteControlStatus.DISABLED,
                "Telegram is not configured.\n\n1. Set SHAMSU_TELEGRAM_BOT_TOKEN\n2. Run /remote_control again\n3. Open your Telegram bot\n\nThe bot token is never displayed.",
            )
        if subcommand == "status":
            return RemoteControlPanel(self._status_from_store(), self._render_status())
        pairing = self.pairing.create_code()
        self.status = RemoteControlStatus.WAITING_FOR_PAIR
        return RemoteControlPanel(
            self.status,
            "\n".join(
                [
                    "Telegram Remote Control",
                    "",
                    "Status: Waiting for pairing",
                    "",
                    f"Pairing code: {pairing.code}",
                    "Expires in 5 minutes.",
                    "",
                    "Open your Telegram bot, press Start, then enter the pairing code.",
                    "",
                    "The bot token is configured but never displayed.",
                ]
            ),
            pairing=pairing,
        )

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        if self.transport is None:
            self.status = RemoteControlStatus.DISABLED
            return
        if self._poll_task is not None and not self._poll_task.done():
            return
        self.status = RemoteControlStatus.STARTING
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        if self.transport is not None:
            await self.transport.close()
        self.status = RemoteControlStatus.DISCONNECTED

    async def process_update(self, update) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        messages = await self.controller.handle_update(update)
        for message in messages:
            await self._send(message)

    async def _poll_loop(self) -> None:
        assert self.transport is not None
        self.status = RemoteControlStatus.CONNECTED if self.store.authorized_users() else RemoteControlStatus.WAITING_FOR_PAIR
        while True:
            try:
                async for update in self.transport.updates():
                    await self.process_update(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status = RemoteControlStatus.ERROR
                self.store.increment_metric("telegram_send_failures")
                await asyncio.sleep(2)
                self.status = RemoteControlStatus.CONNECTED

    async def _send(self, message: OutboundMessage) -> None:
        if self.transport is None:
            return
        try:
            await self.transport.send(message)
            self.store.increment_metric("telegram_messages_sent")
        except Exception:
            self.store.increment_metric("telegram_send_failures")

    def _notify_from_thread(self, message: OutboundMessage) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(lambda: asyncio.create_task(self._send(message)))

    def resolve_approval_callback(self, approval_id: str, approved: bool) -> bool:
        self.store.increment_metric("telegram_approvals")
        return self.approval_broker.resolve(approval_id, approved)

    def _installation_id(self) -> str:
        key = "telegram_installation_id"
        existing = self.store.get_meta(key)
        if existing:
            return existing
        value = f"shamsu-{uuid.uuid4().hex[:16]}"
        self.store.set_meta(key, value)
        return value

    def _status_from_store(self) -> RemoteControlStatus:
        if not self.token:
            return RemoteControlStatus.DISABLED
        if self.store.authorized_users():
            return RemoteControlStatus.CONNECTED
        return self.status if self.status != RemoteControlStatus.DISABLED else RemoteControlStatus.WAITING_FOR_PAIR

    def _render_status(self) -> str:
        users = self.store.authorized_users()
        owner = users[0]["display_name"] if users else "Not paired"
        return "\n".join(
            [
                "Telegram Remote Control",
                "",
                f"Status: {self._status_from_store().value}",
                f"Owner: {owner}",
                f"Active remote users: {len(users)}",
                "",
                "Bot token: configured" if self.token else "Bot token: missing",
            ]
        )

    def _offset_getter(self) -> int:
        raw = self.store.get_meta("telegram_update_offset", "0")
        try:
            return int(raw)
        except ValueError:
            return 0

    def _offset_setter(self, offset: int) -> None:
        self.store.set_meta("telegram_update_offset", str(int(offset)))
