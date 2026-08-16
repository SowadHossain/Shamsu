"""Authorization checks for Telegram updates."""
from __future__ import annotations

from dataclasses import dataclass

from shamsu.integrations.telegram.models import PermissionLevel, TelegramChat, TelegramUser
from shamsu.integrations.telegram.storage import TelegramStateStore


@dataclass(frozen=True)
class AuthorizationResult:
    ok: bool
    reason: str = ""
    permission_level: PermissionLevel | None = None


class TelegramAuthenticator:
    def __init__(self, store: TelegramStateStore, *, allow_group_chats: bool = False) -> None:
        self.store = store
        self.allow_group_chats = allow_group_chats

    def authorize(self, user: TelegramUser, chat: TelegramChat) -> AuthorizationResult:
        if not chat.is_private and not self.allow_group_chats:
            self.store.increment_metric("telegram_auth_failures")
            return AuthorizationResult(False, "Group and channel control is disabled.")
        record = self.store.authorization_for(user.user_id)
        if record is None:
            self.store.increment_metric("telegram_auth_failures")
            return AuthorizationResult(False, "This Telegram user is not paired with SHAMSU.")
        if int(record["telegram_chat_id"]) != int(chat.chat_id):
            self.store.increment_metric("telegram_auth_failures")
            return AuthorizationResult(False, "This chat is not authorized for the paired user.")
        return AuthorizationResult(
            True,
            permission_level=PermissionLevel(str(record["permission_level"])),
        )

