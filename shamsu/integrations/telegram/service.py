"""Lifecycle owner for Telegram remote control."""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shamsu.integrations.telegram import install
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

#: How long the agent thread will wait for one live-card send. This blocks the
#: turn, so it is deliberately short: long enough for a slow phone network,
#: short enough that an unreachable Telegram cannot hold a run hostage. The
#: card treats a timeout as "retry later, keeping every line", and backs off
#: exponentially, so a dead transport costs one stall rather than one per flush.
SEND_TIMEOUT_SECONDS = 10.0


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
            send_message=self._send_card_from_thread,
            typing_action=self._typing_from_thread,
            mirror_factory=self._turn_mirror,
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
                "Recommended:\n"
                "  /remote_control configure <bot-token>\n"
                "This saves it once, for every project.\n\n"
                "Also read, in order:\n"
                "1. SHAMSU_TELEGRAM_BOT_TOKEN in the environment\n"
                f"2. {install.install_token_path()}\n"
                "3. .shamsu/telegram.env in this project\n"
                "4. .env in this project\n\n"
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
            # This branch is exactly "an authorized free-text task", so the
            # desktop echoes it as a terminal prompt rather than as a panel -
            # and the turn's activity lines follow it there.
            self._mirror_inbound(update, as_prompt=True)
            await self._send(self._background_ack(update))
            self._start_background_update(update, mirror_inbound=False)
            return
        await self._handle_update_and_send(update)

    async def _handle_update_and_send(self, update: TelegramUpdate, *, mirror_inbound: bool = True) -> None:
        messages = await self.controller.handle_update(update)
        if not messages:
            return
        if mirror_inbound:
            self._mirror_inbound(update)
        for message in messages:
            await self._send(message)

    def _start_background_update(self, update: TelegramUpdate, *, mirror_inbound: bool = True) -> None:
        task = asyncio.create_task(self._handle_update_and_send(update, mirror_inbound=mirror_inbound))
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

    def _background_ack(self, update: TelegramUpdate) -> OutboundMessage:
        assert update.message is not None
        return OutboundMessage(
            update.message.chat.chat_id,
            "Task received. SHAMSU is starting now.\n\nUse /status anytime while it works.",
        )

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

    def _send_card_from_thread(self, message: OutboundMessage) -> int:
        """Send or edit the live turn card, BLOCKING, and return its id.

        Blocking is the design, not an oversight. The card has to know the
        `message_id` before it can edit anything, and back-pressure here is
        what keeps two edits of the same card from racing and leaving stale
        text on the phone. It costs the agent thread one HTTP round trip at
        most once every 1.5 seconds.

        Errors are raised rather than swallowed: the card treats a refusal as
        "try again at the next flush, keeping every line", which is only
        possible if it hears about it.
        """
        loop = self._loop
        if loop is None or self.transport is None:
            return 0
        future = asyncio.run_coroutine_threadsafe(self._send_returning_id(message), loop)
        return int(future.result(timeout=SEND_TIMEOUT_SECONDS))

    async def _send_returning_id(self, message: OutboundMessage) -> int:
        if self.transport is None:
            return 0
        message_id = int(await self.transport.send(message) or 0)
        self.store.increment_metric("telegram_messages_sent")
        # Deliberately NOT mirrored to the CLI: the desktop renders the same
        # turn from the same stream, and mirroring every edit would print a
        # panel per flush.
        return message_id

    def _typing_from_thread(self, chat_id: int) -> None:
        """Fire-and-forget `sendChatAction`. A courtesy, never worth a wait."""
        loop = self._loop
        transport = self.transport
        if loop is None or transport is None:
            return

        async def _act() -> None:
            try:
                await transport.chat_action(int(chat_id), "typing")
            except Exception:  # noqa: BLE001
                pass

        def _spawn() -> None:
            # Held in `_background_tasks`: asyncio keeps only a weak reference
            # to a bare task, so one nobody holds can be collected mid-await.
            task = asyncio.create_task(_act())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        loop.call_soon_threadsafe(_spawn)

    def _turn_mirror(self) -> Any:
        """A renderer that paints a remote turn on the desktop, if one is watching."""
        factory = getattr(self.cli_mirror, "turn_renderer", None)
        if not callable(factory):
            return None
        return factory()

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

    def _mirror_inbound(self, update: TelegramUpdate, *, as_prompt: bool = False) -> None:
        if self.cli_mirror is None:
            return
        if update.message is not None:
            message = update.message
            if not self.authenticator.authorize(message.user, message.chat).ok:
                return
            echo = getattr(self.cli_mirror, "prompt_echo", None)
            if as_prompt and callable(echo) and message.text:
                echo(message.text)
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
    """The bot token, and where it came from.

    Order, and the reasoning behind it:

    1. ``$SHAMSU_TELEGRAM_BOT_TOKEN`` - unchanged. The CI/ops override has
       always won and still does.
    2. ``~/.shamsu/telegram.env`` - the install token, and where `configure`
       now writes. It comes before the workspace file deliberately: the whole
       point of G3 is that switching project cannot change which bot you are
       talking to, and a stale per-project file left over from before the
       upgrade would silently do exactly that.
    3. ``<workspace>/.shamsu/telegram.env`` - still read, so nobody's existing
       setup breaks on upgrade.
    4. ``<workspace>/.env`` - kept for compatibility.
    """
    env_token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if env_token:
        return env_token, "environment"
    workspace = Path(workspace).resolve()
    candidates = [
        (install.install_token_path(), "install"),
        (workspace / ".shamsu" / "telegram.env", ".shamsu/telegram.env"),
        (workspace / ".env", ".env"),
    ]
    for path, source in candidates:
        token = _read_token_file(path)
        if token:
            return token, source
    return "", "missing"


def configure_telegram_bot_token(
    workspace: Path, token: str, *, install_scope: bool = True
) -> Path:
    """Save the token. Install-wide by default; per-project on request.

    `install_scope=False` is the escape hatch for someone who really does want
    a different bot in one project. It is not the default, because needing to
    do this once per project is the defect being fixed.
    """
    clean = (token or "").strip().strip("'\"")
    if not _looks_like_bot_token(clean):
        raise ValueError("That does not look like a Telegram bot token.")
    content = f"{TOKEN_ENV_VAR}={clean}\n"
    if install_scope:
        return install.write_private_file(install.install_token_path(), content)
    sandbox = Sandbox(Path(workspace).resolve())
    path = sandbox.validate(Path(".shamsu") / "telegram.env")
    return install.write_private_file(path, content)


def promote_workspace_token(workspace: Path) -> Path | None:
    """Copy a pre-upgrade workspace token up to the install, once.

    Returns the install path if it promoted something, `None` otherwise, so a
    caller can say so exactly once rather than every time.

    The old file is never deleted. A downgrade, or a colleague on the previous
    version sharing the checkout, must not find the token gone - and the cost
    of leaving it is one stale file that nothing reads.
    """
    if os.environ.get(TOKEN_ENV_VAR, "").strip():
        return None
    destination = install.install_token_path()
    if _read_token_file(destination):
        return None
    existing = _read_token_file(install.workspace_token_path(workspace))
    if not existing:
        return None
    return install.write_private_file(destination, f"{TOKEN_ENV_VAR}={existing}\n")


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
