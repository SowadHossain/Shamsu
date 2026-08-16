"""Opaque inline-button callback registry and validation."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from shamsu.integrations.telegram.models import (
    CallbackAction,
    CallbackValidationResult,
    TelegramCallbackQuery,
)
from shamsu.integrations.telegram.storage import TelegramStateStore


class CallbackRegistry:
    def __init__(self, store: TelegramStateStore, *, ttl_seconds: int = 600) -> None:
        self.store = store
        self.ttl_seconds = ttl_seconds

    def create(
        self,
        action: CallbackAction,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        session_id: str = "",
        run_id: str = "",
        approval_id: str = "",
        payload: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        action_id = f"tgcb_{secrets.token_urlsafe(18)}"
        expires_at = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds or self.ttl_seconds)
        ).isoformat()
        self.store.create_callback_action(
            action_id=action_id,
            action=action,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            session_id=session_id,
            run_id=run_id,
            approval_id=approval_id,
            payload=payload or {},
            expires_at=expires_at,
        )
        return action_id

    def validate(
        self,
        callback: TelegramCallbackQuery,
        *,
        expected_run_id: str = "",
        consume: bool = True,
    ) -> CallbackValidationResult:
        record = self.store.load_callback_action(callback.data)
        if record is None:
            return CallbackValidationResult(False, reason="Unknown or expired action.")
        if int(record["telegram_user_id"]) != int(callback.user.user_id):
            return CallbackValidationResult(False, reason="This action belongs to another user.")
        if int(record["telegram_chat_id"]) != int(callback.message.chat.chat_id):
            return CallbackValidationResult(False, reason="This action belongs to another chat.")
        if expected_run_id and str(record.get("run_id") or "") != expected_run_id:
            return CallbackValidationResult(False, reason="This action belongs to another run.")
        if str(record.get("consumed_at") or ""):
            return CallbackValidationResult(False, reason="This action was already used.")
        try:
            expires_at = datetime.fromisoformat(str(record["expires_at"]))
        except ValueError:
            return CallbackValidationResult(False, reason="Invalid action.")
        if expires_at < datetime.now(timezone.utc):
            return CallbackValidationResult(False, reason="This action expired.")
        if consume and not self.store.consume_callback_action(callback.data):
            return CallbackValidationResult(False, reason="This action was already used.")
        return CallbackValidationResult(
            True,
            action=CallbackAction(str(record["action"])),
            payload={
                **(record.get("payload") or {}),
                "session_id": str(record.get("session_id") or ""),
                "run_id": str(record.get("run_id") or ""),
                "approval_id": str(record.get("approval_id") or ""),
            },
        )

