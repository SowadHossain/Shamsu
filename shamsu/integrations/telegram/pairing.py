"""Short-lived local pairing codes for Telegram remote control."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from shamsu.integrations.telegram.models import PermissionLevel, TelegramChat, TelegramUser
from shamsu.integrations.telegram.storage import TelegramStateStore


@dataclass(frozen=True)
class PairingCode:
    pairing_id: str
    code: str
    expires_at: str


@dataclass(frozen=True)
class PairingResult:
    ok: bool
    reason: str = ""
    telegram_user_id: int = 0


class PairingManager:
    def __init__(
        self,
        store: TelegramStateStore,
        *,
        installation_id: str,
        ttl_seconds: int = 300,
        max_attempts: int = 5,
    ) -> None:
        self.store = store
        self.installation_id = installation_id
        self.ttl_seconds = ttl_seconds
        self.max_attempts = max_attempts

    def create_code(self) -> PairingCode:
        code = f"{secrets.randbelow(1_000_000):06d}"
        pairing = PairingCode(
            pairing_id=f"pair-{uuid.uuid4().hex[:16]}",
            code=code,
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)).isoformat(),
        )
        self.store.create_pairing_code(
            pairing_id=pairing.pairing_id,
            code_hash=self._hash_code(code),
            installation_id=self.installation_id,
            expires_at=pairing.expires_at,
        )
        return pairing

    def verify(
        self,
        code: str,
        *,
        user: TelegramUser,
        chat: TelegramChat,
        permission_level: PermissionLevel = PermissionLevel.OWNER,
    ) -> PairingResult:
        if not chat.is_private:
            return PairingResult(False, "Group and channel chats are disabled for Telegram control.")
        record = self.store.find_pairing_by_hash(self._hash_code(code.strip()))
        if record is None:
            return PairingResult(False, "Invalid pairing code.")
        attempts = self.store.increment_pairing_attempts(str(record["pairing_id"]))
        if attempts > self.max_attempts:
            return PairingResult(False, "Too many pairing attempts. Create a new code locally.")
        if str(record.get("consumed_at") or ""):
            return PairingResult(False, "Pairing code has already been used.")
        try:
            expires_at = datetime.fromisoformat(str(record["expires_at"]))
        except ValueError:
            return PairingResult(False, "Pairing code is invalid.")
        if expires_at < datetime.now(timezone.utc):
            return PairingResult(False, "Pairing code expired.")
        self.store.consume_pairing(str(record["pairing_id"]))
        self.store.authorize_user(
            telegram_user_id=user.user_id,
            telegram_chat_id=chat.chat_id,
            installation_id=str(record["installation_id"]),
            display_name=user.display_name,
            permission_level=permission_level,
        )
        return PairingResult(True, telegram_user_id=user.user_id)

    @staticmethod
    def _hash_code(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

