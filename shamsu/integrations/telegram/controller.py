"""Deterministic Telegram command and callback controller."""
from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from shamsu.integrations.telegram.authentication import TelegramAuthenticator
from shamsu.integrations.telegram.callbacks import CallbackRegistry
from shamsu.integrations.telegram.commands import parse_command
from shamsu.integrations.telegram.files import TelegramFileStager
from shamsu.integrations.telegram.formatter import TelegramFormatter
from shamsu.integrations.telegram.keyboards import (
    home_keyboard,
    run_control_keyboard,
    session_keyboard,
)
from shamsu.integrations.telegram.models import (
    CallbackAction,
    OutboundMessage,
    TelegramCallbackQuery,
    TelegramFile,
    TelegramInboundMetadata,
    TelegramMessage,
    TelegramUpdate,
    utc_now,
)
from shamsu.integrations.telegram.pairing import PairingManager
from shamsu.integrations.telegram.sessions import SessionGateway
from shamsu.integrations.telegram.storage import TelegramStateStore
from shamsu.voice import VoiceError, VoiceService

VoiceDownloader = Callable[[TelegramFile, Path], Awaitable[Path]]


class TelegramController:
    def __init__(
        self,
        *,
        store: TelegramStateStore,
        authenticator: TelegramAuthenticator,
        pairing: PairingManager,
        callbacks: CallbackRegistry,
        gateway: SessionGateway,
        workspace: Path,
        formatter: TelegramFormatter | None = None,
        approval_resolver: Callable[[str, bool], bool] | None = None,
        voice_service: VoiceService | None = None,
        voice_downloader: VoiceDownloader | None = None,
    ) -> None:
        self.store = store
        self.authenticator = authenticator
        self.pairing = pairing
        self.callbacks = callbacks
        self.gateway = gateway
        self.formatter = formatter or TelegramFormatter()
        self.approval_resolver = approval_resolver
        self.files = TelegramFileStager(workspace)
        self.voice_service = voice_service
        self.voice_downloader = voice_downloader

    async def handle_update(self, update: TelegramUpdate) -> list[OutboundMessage]:
        self.store.increment_metric("telegram_updates_received")
        if not self.store.mark_update_processed(update.update_id):
            return []
        if update.callback_query is not None:
            return self._handle_callback(update.callback_query)
        if update.message is not None:
            return await self._handle_message(update.message)
        return []

    async def _handle_message(self, message: TelegramMessage) -> list[OutboundMessage]:
        command = parse_command(message.text)
        if command and command.name == "/start":
            return [self._handle_start(message)]
        if command is None and _looks_like_pairing_code(message.text):
            result = self.pairing.verify(message.text, user=message.user, chat=message.chat)
            self.store.audit(
                telegram_user_id=message.user.user_id,
                telegram_chat_id=message.chat.chat_id,
                telegram_message_id=message.message_id,
                action="pairing.verify",
                result="ok" if result.ok else "rejected",
                payload={"reason": result.reason},
            )
            text = "Connected to this SHAMSU installation." if result.ok else result.reason
            if result.ok:
                session_id = self.gateway.ensure_default_session()
                self.store.set_active_session(message.user.user_id, session_id)
            # One of the two things the desktop is told about: a device just
            # gained - or was refused - control of this installation. That is
            # not turn output, and you want to see it even if you were not
            # looking at your phone.
            who = message.user.display_name
            return [
                OutboundMessage(
                    message.chat.chat_id,
                    text,
                    mirror_to_cli=True,
                    cli_text=(
                        f"{who} paired with this installation."
                        if result.ok
                        else f"{who} failed to pair: {result.reason}"
                    ),
                )
            ]
        authorization = self.authenticator.authorize(message.user, message.chat)
        if not authorization.ok:
            self.store.increment_metric("telegram_updates_rejected")
            return [
                OutboundMessage(
                    message.chat.chat_id,
                    authorization.reason,
                    mirror_to_cli=True,
                    cli_text=f"{message.user.display_name} was refused: {authorization.reason}",
                )
            ]
        if command is not None:
            return await self._handle_command(message, command.name)
        if message.voice is not None:
            return [await self._handle_voice(message)]
        if message.document is not None:
            return [self._handle_file(message)]
        return [await self._handle_free_text(message)]

    def _handle_start(self, message: TelegramMessage) -> OutboundMessage:
        authorization = self.authenticator.authorize(message.user, message.chat)
        if not authorization.ok:
            return OutboundMessage(
                message.chat.chat_id,
                "Welcome to SHAMSU Remote.\n\nEnter your local pairing code to connect.",
            )
        session_id = self._active_or_default_session(message.user.user_id)
        status = self.gateway.status(session_id)
        return OutboundMessage(
            message.chat.chat_id,
            self.formatter.home(status, connected=True),
            reply_markup=home_keyboard(
                registry=self.callbacks,
                telegram_user_id=message.user.user_id,
                telegram_chat_id=message.chat.chat_id,
                session_id=session_id,
                run_id=status.run_id,
            ),
        )

    async def _handle_command(self, message: TelegramMessage, command: str) -> list[OutboundMessage]:
        self.store.increment_metric("telegram_commands_processed")
        if command == "/help":
            return [OutboundMessage(message.chat.chat_id, self.formatter.help())]
        if command in {"/sessions", "/switch"}:
            session_id = self._active_or_default_session(message.user.user_id)
            sessions = self.gateway.list_sessions()
            return [
                OutboundMessage(
                    message.chat.chat_id,
                    self.formatter.sessions(sessions, active_session_id=session_id),
                    reply_markup=session_keyboard(
                        sessions,
                        registry=self.callbacks,
                        telegram_user_id=message.user.user_id,
                        telegram_chat_id=message.chat.chat_id,
                        active_session_id=session_id,
                    ),
                )
            ]
        if command == "/new":
            summary = self.gateway.create_session()
            self.store.set_active_session(message.user.user_id, summary.session_id)
            return [OutboundMessage(message.chat.chat_id, f"Created session: {summary.display_name}")]
        if command == "/status":
            return [self._status_message(message)]
        if command == "/plan":
            session_id = self._active_or_default_session(message.user.user_id)
            title, steps = self.gateway.plan(session_id)
            return [OutboundMessage(message.chat.chat_id, self.formatter.plan(title, steps))]
        if command == "/changes":
            session_id = self._active_or_default_session(message.user.user_id)
            return [OutboundMessage(message.chat.chat_id, self.formatter.changes(self.gateway.changes(session_id)))]
        if command == "/tests":
            session_id = self._active_or_default_session(message.user.user_id)
            return [OutboundMessage(message.chat.chat_id, self.formatter.tests(self.gateway.tests(session_id)))]
        if command == "/logs":
            session_id = self._active_or_default_session(message.user.user_id)
            return [OutboundMessage(message.chat.chat_id, self.formatter.logs(self.gateway.logs(session_id)))]
        if command == "/settings":
            return [OutboundMessage(message.chat.chat_id, self.formatter.settings())]
        if command == "/pause":
            session_id = self._active_or_default_session(message.user.user_id)
            ok = self.gateway.pause_current_run(session_id)
            self.store.audit(
                telegram_user_id=message.user.user_id,
                telegram_chat_id=message.chat.chat_id,
                telegram_message_id=message.message_id,
                session_id=session_id,
                action="run.pause",
                result="ok" if ok else "no_active_run",
            )
            return [OutboundMessage(message.chat.chat_id, "SHAMSU will pause at the next safe boundary." if ok else "No active run to pause.")]
        if command == "/resume":
            session_id = self._active_or_default_session(message.user.user_id)
            ok = self.gateway.resume_current_run(session_id)
            return [OutboundMessage(message.chat.chat_id, "Resumed." if ok else "No paused run to resume.")]
        if command == "/cancel":
            session_id = self._active_or_default_session(message.user.user_id)
            status = self.gateway.status(session_id)
            if not status.run_id:
                return [OutboundMessage(message.chat.chat_id, "No active run to cancel.")]
            return [
                OutboundMessage(
                    message.chat.chat_id,
                    f"Cancel current task?\n\n{status.task or status.run_id}",
                    reply_markup=run_control_keyboard(
                        registry=self.callbacks,
                        telegram_user_id=message.user.user_id,
                        telegram_chat_id=message.chat.chat_id,
                        session_id=session_id,
                        run_id=status.run_id,
                        include_resume=False,
                    ),
                )
            ]
        return [OutboundMessage(message.chat.chat_id, "Unknown command. Use /help for the compact command list.")]

    def _handle_callback(self, callback: TelegramCallbackQuery) -> list[OutboundMessage]:
        self.store.increment_metric("telegram_callbacks_processed")
        authorization = self.authenticator.authorize(callback.user, callback.message.chat)
        if not authorization.ok:
            self.store.increment_metric("telegram_updates_rejected")
            return [OutboundMessage(callback.message.chat.chat_id, authorization.reason)]
        validation = self.callbacks.validate(callback)
        if not validation.ok or validation.action is None:
            return [OutboundMessage(callback.message.chat.chat_id, validation.reason)]
        action = validation.action
        session_id = str(validation.payload.get("session_id") or "")
        run_id = str(validation.payload.get("run_id") or "")
        if action == CallbackAction.SWITCH_SESSION:
            self.gateway.switch_session(session_id)
            self.store.set_active_session(callback.user.user_id, session_id)
            self.store.audit(
                telegram_user_id=callback.user.user_id,
                telegram_chat_id=callback.message.chat.chat_id,
                telegram_message_id=callback.message.message_id,
                session_id=session_id,
                action="session.switch",
                result="ok",
            )
            status = self.gateway.status(session_id)
            return [
                OutboundMessage(
                    callback.message.chat.chat_id,
                    self.formatter.home(status, connected=True),
                    edit_message_id=callback.message.message_id,
                    reply_markup=home_keyboard(
                        registry=self.callbacks,
                        telegram_user_id=callback.user.user_id,
                        telegram_chat_id=callback.message.chat.chat_id,
                        session_id=session_id,
                        run_id=status.run_id,
                    ),
                )
            ]
        if action == CallbackAction.OPEN_SESSIONS:
            active_session_id = self._active_or_default_session(callback.user.user_id)
            sessions = self.gateway.list_sessions()
            return [
                OutboundMessage(
                    callback.message.chat.chat_id,
                    self.formatter.sessions(sessions, active_session_id=active_session_id),
                    edit_message_id=callback.message.message_id,
                    reply_markup=session_keyboard(
                        sessions,
                        registry=self.callbacks,
                        telegram_user_id=callback.user.user_id,
                        telegram_chat_id=callback.message.chat.chat_id,
                        active_session_id=active_session_id,
                    ),
                )
            ]
        if action == CallbackAction.OPEN_STATUS:
            return [self._status_message(callback.message)]
        if action == CallbackAction.OPEN_PLAN:
            title, steps = self.gateway.plan(session_id)
            return [OutboundMessage(callback.message.chat.chat_id, self.formatter.plan(title, steps))]
        if action == CallbackAction.OPEN_CHANGES:
            return [
                OutboundMessage(
                    callback.message.chat.chat_id,
                    self.formatter.changes(self.gateway.changes(session_id)),
                )
            ]
        if action == CallbackAction.CANCEL_RUN:
            ok = self.gateway.cancel_current_run(session_id)
            self.store.increment_metric("telegram_cancellations")
            self.store.audit(
                telegram_user_id=callback.user.user_id,
                telegram_chat_id=callback.message.chat.chat_id,
                telegram_message_id=callback.message.message_id,
                session_id=session_id,
                run_id=run_id,
                action="run.cancel",
                result="ok" if ok else "no_active_run",
            )
            return [OutboundMessage(callback.message.chat.chat_id, "Task cancelled." if ok else "No active run to cancel.")]
        if action == CallbackAction.PAUSE_RUN:
            ok = self.gateway.pause_current_run(session_id)
            return [OutboundMessage(callback.message.chat.chat_id, "Paused." if ok else "No active run to pause.")]
        if action == CallbackAction.RESUME_RUN:
            ok = self.gateway.resume_current_run(session_id)
            return [OutboundMessage(callback.message.chat.chat_id, "Resumed." if ok else "No active run to resume.")]
        if action in {CallbackAction.APPROVE, CallbackAction.REJECT}:
            approval_id = str(validation.payload.get("approval_id") or "")
            resolved = False
            if self.approval_resolver is not None:
                resolved = self.approval_resolver(approval_id, action == CallbackAction.APPROVE)
            self.store.audit(
                telegram_user_id=callback.user.user_id,
                telegram_chat_id=callback.message.chat.chat_id,
                telegram_message_id=callback.message.message_id,
                session_id=session_id,
                run_id=run_id,
                action=f"approval.{action.value}",
                result="ok" if resolved else "already_resolved",
                payload={"approval_id": approval_id},
            )
            text = "Approved. SHAMSU will continue." if action == CallbackAction.APPROVE else "Rejected. SHAMSU will stop that action."
            return [OutboundMessage(callback.message.chat.chat_id, text)]
        return [OutboundMessage(callback.message.chat.chat_id, "Action handled.")]

    async def _handle_free_text(self, message: TelegramMessage) -> OutboundMessage:
        return await self._route_text_message(message, message.text, action="message.route")

    async def _handle_voice(self, message: TelegramMessage) -> OutboundMessage:
        assert message.voice is not None
        if self.voice_service is None or self.voice_downloader is None:
            return OutboundMessage(
                message.chat.chat_id,
                "Voice input is not enabled. Install the voice extra and restart SHAMSU.",
            )
        try:
            with tempfile.TemporaryDirectory(prefix="shamsu-telegram-voice-") as directory:
                suffix = Path(message.voice.file_name or "voice.oga").suffix or ".oga"
                downloaded = Path(directory) / f"voice{suffix}"
                await self.voice_downloader(message.voice, downloaded)
                transcript = await asyncio.to_thread(self.voice_service.transcribe_file, downloaded)
        except VoiceError as exc:
            self.store.audit(
                telegram_user_id=message.user.user_id,
                telegram_chat_id=message.chat.chat_id,
                telegram_message_id=message.message_id,
                action="voice.transcribe",
                result="failed",
                payload={"reason": str(exc)[:300]},
            )
            return OutboundMessage(message.chat.chat_id, str(exc))
        text = transcript.text.strip()
        return await self._route_text_message(
            message,
            text,
            action="voice.route",
            prefix=f'Heard: "{text}"',
        )

    async def _route_text_message(
        self,
        message: TelegramMessage,
        text: str,
        *,
        action: str,
        prefix: str = "",
    ) -> OutboundMessage:
        session_id = self._active_or_default_session(message.user.user_id)
        result = await self.gateway.route_user_message(
            text,
            metadata=TelegramInboundMetadata(
                source="telegram",
                telegram_user_id=message.user.user_id,
                telegram_chat_id=message.chat.chat_id,
                telegram_message_id=message.message_id,
                session_id=session_id,
                timestamp=utc_now(),
            ),
        )
        self.store.audit(
            telegram_user_id=message.user.user_id,
            telegram_chat_id=message.chat.chat_id,
            telegram_message_id=message.message_id,
            session_id=session_id,
            run_id=result.run_id,
            action=action,
            result=result.status or "ok",
            payload={"text_preview": text[:300]},
        )
        pages = self.formatter.paginate(result.text)
        response = pages[0].text
        if prefix:
            response = f"{prefix}\n\n{response}"
        return OutboundMessage(message.chat.chat_id, response)

    def _handle_file(self, message: TelegramMessage) -> OutboundMessage:
        assert message.document is not None
        staged = self.files.stage_metadata_only(message.document)
        session_id = self._active_or_default_session(message.user.user_id)
        self.store.audit(
            telegram_user_id=message.user.user_id,
            telegram_chat_id=message.chat.chat_id,
            telegram_message_id=message.message_id,
            session_id=session_id,
            action="file.stage",
            result="ok" if staged.ok else "rejected",
            payload={"filename": staged.filename, "reason": staged.reason},
        )
        if not staged.ok:
            return OutboundMessage(message.chat.chat_id, staged.reason)
        return OutboundMessage(
            message.chat.chat_id,
            f"Received\n\n{staged.filename}\n\nAttached to: {self.gateway.switch_session(session_id).display_name}\n\nWhat should SHAMSU do with it?",
        )

    def _status_message(self, message: TelegramMessage) -> OutboundMessage:
        session_id = self._active_or_default_session(message.user.user_id)
        status = self.gateway.status(session_id)
        markup = None
        if status.run_id:
            markup = run_control_keyboard(
                registry=self.callbacks,
                telegram_user_id=message.user.user_id,
                telegram_chat_id=message.chat.chat_id,
                session_id=session_id,
                run_id=status.run_id,
            )
        return OutboundMessage(message.chat.chat_id, self.formatter.status(status), reply_markup=markup)

    def _active_or_default_session(self, telegram_user_id: int) -> str:
        session_id = self.store.active_session_for(telegram_user_id)
        if session_id:
            return session_id
        session_id = self.gateway.ensure_default_session()
        self.store.set_active_session(telegram_user_id, session_id)
        return session_id


def _looks_like_pairing_code(text: str) -> bool:
    stripped = (text or "").strip()
    return len(stripped) == 6 and stripped.isdigit()
