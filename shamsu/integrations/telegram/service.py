"""Lifecycle owner for Telegram remote control."""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shamsu.integrations.telegram.authentication import TelegramAuthenticator
from shamsu.integrations.telegram.approvals import TelegramApprovalBroker
from shamsu.integrations.telegram.callbacks import CallbackRegistry
from shamsu.integrations.telegram.commands import parse_command
from shamsu.integrations.telegram.controller import TelegramController
from shamsu.integrations.telegram.formatter import TelegramFormatter
from shamsu.integrations.telegram.models import OutboundMessage, RemoteControlStatus, TelegramUpdate
from shamsu.integrations.telegram.pairing import PairingCode, PairingManager
from shamsu.integrations.telegram.sessions import LocalShamsuSessionGateway, SessionGateway
from shamsu.integrations.telegram.storage import TelegramStateStore
from shamsu.integrations.telegram.transport import TelegramBotApiTransport, TelegramTransport
from shamsu.safety.sandbox import Sandbox

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
        cli_mirror: Callable[[str, str], None] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store or TelegramStateStore(self.workspace)
        self.installation_id = installation_id or self._installation_id()
        self.formatter = TelegramFormatter()
        self.callbacks = CallbackRegistry(self.store)
        self.pairing = PairingManager(self.store, installation_id=self.installation_id)
        self.authenticator = TelegramAuthenticator(self.store)
        self.status = RemoteControlStatus.DISABLED
        self.cli_mirror = cli_mirror
        self.token, self.token_source = (
            (token, "injected") if token is not None else load_telegram_bot_token(self.workspace)
        )
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
        self._background_tasks: set[asyncio.Task[Any]] = set()
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

    def set_cli_mirror(self, cli_mirror: Callable[[str, str], None] | None) -> None:
        self.cli_mirror = cli_mirror

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
                "Telegram is not configured.\n\n"
                "Use one of:\n"
                "1. Set SHAMSU_TELEGRAM_BOT_TOKEN\n"
                "2. Create .shamsu/telegram.env with SHAMSU_TELEGRAM_BOT_TOKEN=...\n"
                "3. Create .env with SHAMSU_TELEGRAM_BOT_TOKEN=...\n\n"
                "Then run /remote_control again.\n\n"
                "The bot token is never displayed.",
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
        if self._background_tasks:
            tasks = list(self._background_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.clear()
        if self.transport is not None:
            await self.transport.close()
        self.status = RemoteControlStatus.DISCONNECTED

    async def process_update(self, update: TelegramUpdate) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        if self._should_process_in_background(update):
            self._start_background_update(update)
            return
        await self._handle_update_and_send(update)

    async def _handle_update_and_send(self, update: TelegramUpdate) -> None:
        messages = await self.controller.handle_update(update)
        if not messages:
            return
        self._mirror_inbound(update)
        for message in messages:
            await self._send(message)

    def _start_background_update(self, update: TelegramUpdate) -> None:
        task = asyncio.create_task(self._handle_update_and_send(update))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._record_background_task_result)

    def _record_background_task_result(self, task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            self.status = RemoteControlStatus.ERROR
            self.store.increment_metric("telegram_send_failures")

    def _should_process_in_background(self, update: TelegramUpdate) -> bool:
        message = update.message
        if message is None or update.callback_query is not None:
            return False
        if message.document is not None:
            return False
        text = (message.text or "").strip()
        if not text:
            return False
        if _looks_like_pairing_code(text):
            return False
        if parse_command(text) is not None:
            return False
        return self.authenticator.authorize(message.user, message.chat).ok

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
            self._mirror_outbound(message)
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
                f"Bot token: configured ({self.token_source})" if self.token else "Bot token: missing",
            ]
        )

    def _mirror_inbound(self, update: TelegramUpdate) -> None:
        if self.cli_mirror is None:
            return
        if update.message is not None:
            message = update.message
            if not self.authenticator.authorize(message.user, message.chat).ok:
                return
            self.cli_mirror(
                f"Telegram {message.user.display_name}",
                _safe_inbound_preview(message.text, has_document=message.document is not None),
            )
            return
        if update.callback_query is not None:
            callback = update.callback_query
            if not self.authenticator.authorize(callback.user, callback.message.chat).ok:
                return
            self.cli_mirror(
                f"Telegram {callback.user.display_name}",
                "pressed a Telegram action button.",
            )

    def _mirror_outbound(self, message: OutboundMessage) -> None:
        if self.cli_mirror is None:
            return
        if not self._chat_is_authorized(message.chat_id):
            return
        text = message.text
        if message.edit_message_id is not None:
            text = f"(edited Telegram message)\n{text}"
        self.cli_mirror("SHAMSU -> Telegram", text)

    def _chat_is_authorized(self, chat_id: int) -> bool:
        return any(int(user["telegram_chat_id"]) == int(chat_id) for user in self.store.authorized_users())

    def _offset_getter(self) -> int:
        raw = self.store.get_meta("telegram_update_offset", "0")
        try:
            return int(raw)
        except ValueError:
            return 0

    def _offset_setter(self, offset: int) -> None:
        self.store.set_meta("telegram_update_offset", str(int(offset)))


def load_telegram_bot_token(workspace: Path) -> tuple[str, str]:
    env_token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token, "environment"
    workspace = Path(workspace).resolve()
    candidates = [
        workspace / ".shamsu" / "telegram.env",
        workspace / ".env",
    ]
    for path in candidates:
        token = _read_token_file(path)
        if token:
            source = ".shamsu/telegram.env" if path.name == "telegram.env" else ".env"
            return token, source
    return "", "missing"


def configure_telegram_bot_token(workspace: Path, token: str) -> Path:
    clean = (token or "").strip().strip("'\"")
    if not _looks_like_bot_token(clean):
        raise ValueError("That does not look like a Telegram bot token.")
    sandbox = Sandbox(Path(workspace).resolve())
    path = sandbox.validate(Path(".shamsu") / "telegram.env")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{TOKEN_ENV_VAR}={clean}\n", encoding="utf-8")
    return path


def _read_token_file(path: Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != TOKEN_ENV_VAR:
            continue
        value = value.strip().strip("'\"")
        return value
    return ""


def _looks_like_bot_token(token: str) -> bool:
    if ":" not in token:
        return False
    left, _, right = token.partition(":")
    return left.isdigit() and len(right) >= 20 and not any(char.isspace() for char in token)


def _safe_inbound_preview(text: str, *, has_document: bool = False) -> str:
    stripped = (text or "").strip()
    if _looks_like_pairing_code(stripped):
        return "entered a pairing code."
    command = parse_command(stripped)
    if command and command.name == "/start":
        return "/start"
    if has_document and not stripped:
        return "sent a file."
    return stripped or "sent a message."


def _looks_like_pairing_code(text: str) -> bool:
    return len(text) == 6 and text.isdigit()
