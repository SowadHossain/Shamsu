"""Lifecycle owner for Telegram remote control."""
from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shamsu.control.store import MACHINE_LEASE_KEY
from shamsu.integrations.telegram import install
from shamsu.integrations.telegram.approvals import TelegramApprovalBroker
from shamsu.integrations.telegram.authentication import TelegramAuthenticator
from shamsu.integrations.telegram.callbacks import CallbackRegistry
from shamsu.integrations.telegram.commands import parse_command
from shamsu.integrations.telegram.controller import TelegramController
from shamsu.integrations.telegram.formatter import TelegramFormatter
from shamsu.integrations.telegram.models import (
    OutboundMessage,
    RemoteControlStatus,
    TelegramUpdate,
    utc_now,
)
from shamsu.integrations.telegram.pairing import PairingCode, PairingManager
from shamsu.integrations.telegram.sessions import LocalShamsuSessionGateway, SessionGateway
from shamsu.integrations.telegram.storage import TelegramStateStore
from shamsu.integrations.telegram.transport import TelegramBotApiTransport, TelegramTransport
from shamsu.integrations.telegram.webhook import (
    CloudflaredQuickTunnel,
    TelegramWebhookError,
    TelegramWebhookServer,
    wait_for_public_webhook,
)
from shamsu.safety.sandbox import Sandbox
from shamsu.voice import VoiceService

TOKEN_ENV_VAR = "SHAMSU_TELEGRAM_BOT_TOKEN"
TRANSPORT_ENV_VAR = "SHAMSU_TELEGRAM_TRANSPORT"
WEBHOOK_URL_ENV_VAR = "SHAMSU_TELEGRAM_WEBHOOK_URL"
WEBHOOK_HOST_ENV_VAR = "SHAMSU_TELEGRAM_WEBHOOK_HOST"
WEBHOOK_PORT_ENV_VAR = "SHAMSU_TELEGRAM_WEBHOOK_PORT"
WEBHOOK_TUNNEL_ATTEMPTS_ENV_VAR = "SHAMSU_TELEGRAM_WEBHOOK_TUNNEL_ATTEMPTS"

#: The bot's slot in the install-wide lease table. Telegram allows exactly one
#: `getUpdates` caller per token and answers the second one with 409, so two
#: SHAMSU processes polling the same bot is not a degraded mode, it is an
#: outage - and the loser's exception is swallowed by the poll loop's retry.
#:
#: Expressing "who is running the bot" as a lease rather than a status file
#: gets three answers from one row: is it running, in which process, and since
#: when. The settings page reads the same row the arbitration writes, so the
#: display cannot drift from the truth.
POLLER_SESSION = "telegram-poller"

#: Meta keys in the Telegram state DB. Durable because the process that saw the
#: failure is usually not the process being asked about it.
META_POLL_ERROR = "last_poll_error"
META_POLL_ERROR_AT = "last_poll_error_at"
META_POLLER_WORKSPACE = "poller_workspace"
META_TRANSPORT_MODE = "telegram_transport_mode"
META_WEBHOOK_URL = "telegram_webhook_url"
META_WEBHOOK_LOCAL_URL = "telegram_webhook_local_url"
META_WEBHOOK_STARTED_AT = "telegram_webhook_started_at"

#: A constant, because the title used to be `f"Telegram {display_name}"` while
#: the body carried the rest of the sentence - so a pairing produced a panel
#: titled "Telegram sus" containing "entered a pairing code.", one sentence sawn
#: in half across a border. The title names the channel; the body is a whole
#: sentence and reads on its own.
TELEGRAM_PANEL_TITLE = "Telegram"

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


@dataclass(frozen=True)
class PollerStart:
    """Why `start()` did or did not begin polling.

    A tri-state rather than a bool, for the same reason `QueuedRunner` has one:
    "did not start" hides two different situations - it was already running, or
    somebody else owns the bot - and only the second one is worth telling the
    user about.
    """

    outcome: str
    holder_pid: int = 0
    holder_surface: str = ""
    detail: str = ""
    transport: str = "polling"

    @property
    def running(self) -> bool:
        return self.outcome in {"started", "already-running"}


STARTED = "started"
ALREADY_RUNNING = "already-running"
HELD_ELSEWHERE = "held-elsewhere"
NO_TOKEN = "no-token"
FAILED = "failed"

TRANSPORT_POLLING = "polling"
TRANSPORT_WEBHOOK = "webhook"


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
        transport_mode: str | None = None,
        tunnel_factory: Callable[[str], CloudflaredQuickTunnel] | None = None,
        webhook_ready_checker: Callable[[str], None] | None = None,
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
        self.transport_mode = _normalize_transport_mode(transport_mode)
        self.tunnel_factory = tunnel_factory or (lambda local_url: CloudflaredQuickTunnel(local_url))
        self.webhook_ready_checker = webhook_ready_checker or wait_for_public_webhook
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
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._webhook_server: TelegramWebhookServer | None = None
        self._cloudflare_tunnel: CloudflaredQuickTunnel | None = None
        self._control: Any = None
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
            voice_service=VoiceService(),
            voice_downloader=self._download_telegram_file,
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

    async def start(self) -> PollerStart:
        self._loop = asyncio.get_running_loop()
        if self.transport is None:
            self.status = RemoteControlStatus.DISABLED
            return PollerStart(
                NO_TOKEN,
                detail="Telegram is not configured.",
                transport=self.transport_mode,
            )
        if self._poll_task is not None and not self._poll_task.done():
            return PollerStart(ALREADY_RUNNING, transport=self.transport_mode)
        if self._webhook_server is not None:
            return PollerStart(ALREADY_RUNNING, transport=self.transport_mode)
        if not self._claim_poller_lease():
            holder = self._poller_holder()
            self.status = RemoteControlStatus.DISABLED
            return PollerStart(
                HELD_ELSEWHERE,
                holder_pid=holder.owner_pid if holder is not None else 0,
                holder_surface=holder.owner_surface if holder is not None else "",
                detail="Another SHAMSU process is already running this Telegram bot.",
                transport=self.transport_mode,
            )
        self.store.set_meta(META_POLLER_WORKSPACE, str(self.workspace))
        self.store.set_meta(META_TRANSPORT_MODE, self.transport_mode)
        self.status = RemoteControlStatus.STARTING
        if self.transport_mode == TRANSPORT_WEBHOOK:
            try:
                await self._start_webhook()
            except Exception as exc:  # noqa: BLE001 - startup failures are user-facing
                self.status = RemoteControlStatus.ERROR
                self._record_poll_error(exc)
                await self._stop_webhook_runtime()
                try:
                    self._control_store().release_lease(MACHINE_LEASE_KEY, POLLER_SESSION)
                except Exception:  # noqa: BLE001, S110 - a bad lease DB must not hide startup error
                    pass
                return PollerStart(FAILED, detail=str(exc), transport=self.transport_mode)
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            return PollerStart(STARTED, transport=self.transport_mode)
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return PollerStart(STARTED, transport=self.transport_mode)

    def _claim_poller_lease(self) -> bool:
        try:
            return self._control_store().acquire_lease(
                MACHINE_LEASE_KEY, POLLER_SESSION, surface="telegram"
            )
        except Exception:  # noqa: BLE001 - an unusable control DB must not be
            # the reason the bot refuses to run. Losing arbitration is worse
            # than losing it entirely only when two pollers actually collide,
            # and that is the rarer failure.
            return True

    def _poller_holder(self) -> Any:
        try:
            return self._control_store().lease_holder(MACHINE_LEASE_KEY, POLLER_SESSION)
        except Exception:  # noqa: BLE001
            return None

    def _control_store(self) -> Any:
        from shamsu.control.store import ControlStore

        if self._control is None:
            self._control = ControlStore()
        return self._control

    async def _heartbeat_loop(self) -> None:
        """Keep the poller lease warm.

        Needed because long polling can idle for a full `getUpdates` timeout
        with nothing to report, and the lease goes stale after 90 seconds. A
        bot waiting quietly for a message must not look like a crashed one.
        """
        from shamsu.control.store import LEASE_HEARTBEAT_SECONDS

        while True:
            await asyncio.sleep(LEASE_HEARTBEAT_SECONDS)
            try:
                self._control_store().renew_lease(MACHINE_LEASE_KEY, POLLER_SESSION)
            except Exception:  # noqa: BLE001
                pass

    async def stop(self) -> None:
        await self._stop_webhook_runtime()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: B014 - either is fine
                pass
            self._heartbeat_task = None
        try:
            self._control_store().release_lease(MACHINE_LEASE_KEY, POLLER_SESSION)
        except Exception:  # noqa: BLE001
            pass
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

    async def _start_webhook(self) -> None:
        assert self.transport is not None
        assert self._loop is not None
        secret = secrets.token_urlsafe(24)
        host = os.environ.get(WEBHOOK_HOST_ENV_VAR, "127.0.0.1").strip() or "127.0.0.1"
        server = TelegramWebhookServer(
            host=host,
            port=_webhook_port(),
            path=f"/telegram/{secret}",
            secret_token=secret,
            loop=self._loop,
            process_update=self.process_update,
        )
        server.start()
        self._webhook_server = server
        public_root = os.environ.get(WEBHOOK_URL_ENV_VAR, "").strip().rstrip("/")
        if not public_root:
            public_root = await self._start_cloudflare_tunnel(server.local_url)
        if not public_root.startswith("https://"):
            raise TelegramWebhookError("Telegram webhooks require an https URL.")
        if self._cloudflare_tunnel is None:
            await asyncio.to_thread(self.webhook_ready_checker, public_root)
        webhook_url = f"{public_root}{server.webhook_url_path}"
        await self.transport.set_webhook(
            webhook_url,
            secret_token=secret,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )
        self.store.set_meta(META_WEBHOOK_URL, webhook_url)
        self.store.set_meta(META_WEBHOOK_LOCAL_URL, server.local_url)
        self.store.set_meta(META_WEBHOOK_STARTED_AT, utc_now())
        self.status = (
            RemoteControlStatus.CONNECTED
            if self.store.authorized_users()
            else RemoteControlStatus.WAITING_FOR_PAIR
        )

    async def _stop_webhook_runtime(self) -> None:
        if self.transport is not None and self._webhook_server is not None:
            try:
                await self.transport.delete_webhook(drop_pending_updates=False)
            except Exception:  # noqa: BLE001, S110 - shutdown should keep going
                pass
        server = self._webhook_server
        tunnel = self._cloudflare_tunnel
        self._webhook_server = None
        self._cloudflare_tunnel = None
        if server is not None:
            await asyncio.to_thread(server.stop)
        if tunnel is not None:
            await asyncio.to_thread(tunnel.stop)

    async def _start_cloudflare_tunnel(self, local_url: str) -> str:
        attempts = _webhook_tunnel_attempts()
        last_error: BaseException | None = None
        for attempt in range(attempts):
            tunnel = self.tunnel_factory(local_url)
            public_root = await asyncio.to_thread(tunnel.start)
            try:
                await asyncio.to_thread(self.webhook_ready_checker, public_root)
            except Exception as exc:  # noqa: BLE001 - retry with a new quick-tunnel hostname
                last_error = exc
                await asyncio.to_thread(tunnel.stop)
                if attempt + 1 >= attempts:
                    break
                continue
            self._cloudflare_tunnel = tunnel
            return public_root
        raise TelegramWebhookError(str(last_error) if last_error else "Cloudflare tunnel did not start.")

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
        if not text and message.voice is None:
            return False
        if _looks_like_pairing_code(text):
            return False
        if parse_command(text) is not None:
            return False
        return self.authenticator.authorize(message.user, message.chat).ok

    def _background_ack(self, update: TelegramUpdate) -> OutboundMessage:
        assert update.message is not None
        if update.message.voice is not None:
            return OutboundMessage(
                update.message.chat.chat_id,
                "Voice received. SHAMSU is transcribing it now.\n\nUse /status anytime while it works.",
            )
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
            except Exception as exc:
                self.status = RemoteControlStatus.ERROR
                self.store.increment_metric("telegram_send_failures")
                # Recorded, not just retried. This loop's favourite failure is
                # a 409 from a webhook registered against the token, which it
                # will otherwise retry silently until someone gives up on the
                # bot. The settings page reads this back.
                self._record_poll_error(exc)
                await asyncio.sleep(2)
                self.status = RemoteControlStatus.CONNECTED

    def _record_poll_error(self, exc: BaseException) -> None:
        message = f"{type(exc).__name__}: {exc}"
        token = self.token or ""
        if token:
            message = message.replace(token, "<token>")
        try:
            self.store.set_meta(META_POLL_ERROR, message[:500])
            self.store.set_meta(META_POLL_ERROR_AT, utc_now())
        except Exception:  # noqa: BLE001 - reporting a failure must not raise one
            pass

    async def _send(self, message: OutboundMessage) -> None:
        if self.transport is None:
            return
        try:
            await self.transport.send(message)
            self.store.increment_metric("telegram_messages_sent")
            self._mirror_outbound(message)
        except Exception:
            self.store.increment_metric("telegram_send_failures")

    async def _download_telegram_file(self, file: Any, destination: Path) -> Path:
        if self.transport is None:
            raise RuntimeError("Telegram transport is not running.")
        return await self.transport.download_file(file, destination)

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
        if self.status in {
            RemoteControlStatus.ERROR,
            RemoteControlStatus.STARTING,
            RemoteControlStatus.DISCONNECTED,
        }:
            return self.status
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
                f"Transport: {self.transport_mode}",
                f"Active remote users: {len(users)}",
                "",
                f"Webhook: {self.store.get_meta(META_WEBHOOK_URL, '-')}"
                if self.transport_mode == TRANSPORT_WEBHOOK
                else "Webhook: disabled",
                f"Bot token: configured ({self.token_source})" if self.token else "Bot token: missing",
            ]
        )

    def _mirror_inbound(self, update: TelegramUpdate, *, as_prompt: bool = False) -> None:
        """Show the desktop only what changes ITS world.

        A prompt starts a turn here, so it is echoed as a prompt line. A file
        lands in this workspace, so it gets a panel. Everything else - a
        `/status`, a button press, a pairing code - is the phone reading its own
        screen, and printing it here was noise about someone else's session.
        """
        if self.cli_mirror is None:
            return
        if update.message is not None:
            message = update.message
            if not self.authenticator.authorize(message.user, message.chat).ok:
                return
            echo = getattr(self.cli_mirror, "prompt_echo", None)
            if as_prompt and callable(echo) and message.text:
                echo(message.text, self._active_session_title(message.user.user_id))
                return
            if message.voice is not None:
                self.cli_mirror(
                    TELEGRAM_PANEL_TITLE,
                    f"{message.user.display_name} sent a voice message.",
                )
                return
            if message.document is not None:
                self.cli_mirror(
                    TELEGRAM_PANEL_TITLE,
                    f"{message.user.display_name} sent a file: {message.document.file_name}",
                )
            return

    def _active_session_title(self, telegram_user_id: int) -> str:
        """The thread this phone is talking to, for the prompt line.

        Best-effort by design: an unknown title costs `shamsu telegram>`
        instead of `shamsu (asteroids) telegram>`, which is a smaller loss than
        an exception between a user's prompt and their answer.
        """
        try:
            from shamsu.session.manager import SessionManager

            session_id = self.store.active_session_for(int(telegram_user_id))
            if not session_id:
                return ""
            # `resolve` hands back SessionMetadata, so the title is right here.
            return str(SessionManager(self.workspace).resolve(session_id).title or "")
        except Exception:  # noqa: BLE001
            return ""

    def _mirror_outbound(self, message: OutboundMessage) -> None:
        if self.cli_mirror is None:
            return
        # Opt-in. See `OutboundMessage.mirror_to_cli`: turn output already
        # reaches the desktop through the turn renderer, so mirroring it as
        # well printed the same turn twice, once as a transcript and once as a
        # stack of notifications about itself.
        if not message.mirror_to_cli:
            return
        # Deliberately NOT gated on the chat being authorized. The events that
        # opt in are pairing and refusal, and "a stranger just tried to drive
        # your installation" is the one the authorization check would have
        # swallowed - which is precisely the one worth printing.
        self.cli_mirror(TELEGRAM_PANEL_TITLE, message.cli_text or message.text)

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


def poller_status(workspace: Path | None = None) -> dict[str, Any]:
    """Whether the bot is running, and everything the settings page needs.

    A module function, not a method, because the process that asks is almost
    never the process that polls: the web portal and the REPL are separate
    programs, and the answer has to come from durable state rather than from an
    object one of them happens to hold.

    Every source here is already install-scoped - the lease table, the Telegram
    state DB - so this is the same answer wherever it is asked from.
    """
    from shamsu.integrations.telegram.storage import TelegramStateStore

    resolved = Path(workspace).resolve() if workspace is not None else Path.cwd()
    token, source = load_telegram_bot_token(resolved)

    holder = None
    try:
        from shamsu.control.store import ControlStore

        holder = ControlStore().lease_holder(MACHINE_LEASE_KEY, POLLER_SESSION)
    except Exception:  # noqa: BLE001 - an unreadable control DB means "unknown",
        # which is reported as not running rather than raised at the caller.
        holder = None

    store = TelegramStateStore(resolved)
    metrics = store.metrics()
    users = store.authorized_users()
    mode = store.get_meta(META_TRANSPORT_MODE, "") or _normalize_transport_mode(None)
    return {
        "configured": bool(token),
        "token_source": source if token else "missing",
        "transport": "webhook" if mode == TRANSPORT_WEBHOOK else "long polling",
        "transport_mode": mode,
        "running": holder is not None,
        "owner_pid": holder.owner_pid if holder is not None else 0,
        "owner_surface": holder.owner_surface if holder is not None else "",
        "since": holder.heartbeat if holder is not None else "",
        "is_this_process": bool(holder is not None and holder.is_mine),
        "workspace": store.get_meta(META_POLLER_WORKSPACE, ""),
        "webhook_url": store.get_meta(META_WEBHOOK_URL, ""),
        "webhook_local_url": store.get_meta(META_WEBHOOK_LOCAL_URL, ""),
        "webhook_started_at": store.get_meta(META_WEBHOOK_STARTED_AT, ""),
        "paired_count": len(users),
        "messages_sent": int(metrics.get("telegram_messages_sent", 0)),
        "send_failures": int(metrics.get("telegram_send_failures", 0)),
        "last_error": store.get_meta(META_POLL_ERROR, ""),
        "last_error_at": store.get_meta(META_POLL_ERROR_AT, ""),
    }


def _normalize_transport_mode(value: str | None) -> str:
    raw = (value or os.environ.get(TRANSPORT_ENV_VAR, "") or TRANSPORT_POLLING).strip().lower()
    if raw in {"webhook", "webhooks", "cloudflare", "cloudflare-tunnel"}:
        return TRANSPORT_WEBHOOK
    return TRANSPORT_POLLING


def _webhook_port() -> int:
    raw = os.environ.get(WEBHOOK_PORT_ENV_VAR, "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _webhook_tunnel_attempts() -> int:
    raw = os.environ.get(WEBHOOK_TUNNEL_ATTEMPTS_ENV_VAR, "").strip()
    if not raw:
        return 3
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


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


# `_safe_inbound_preview` lived here and turned an inbound message into a verb
# phrase for a panel title that was a name - "Telegram sus" / "entered a pairing
# code." Both halves are gone: the desktop no longer narrates what the phone is
# reading, so there is nothing left to preview.


def _looks_like_pairing_code(text: str) -> bool:
    return len(text) == 6 and text.isdigit()
