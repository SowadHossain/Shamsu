"""Telegram approval bridge for SHAMSU's existing approval policy."""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import asdict

from shamsu.integrations.telegram.callbacks import CallbackRegistry
from shamsu.integrations.telegram.formatter import TelegramFormatter
from shamsu.integrations.telegram.keyboards import approval_keyboard
from shamsu.integrations.telegram.models import OutboundMessage, PendingApproval
from shamsu.integrations.telegram.storage import TelegramStateStore
from shamsu.types import ApprovalRequest


class TelegramApprovalBroker:
    """Sync approval function backed by Telegram callbacks.

    SHAMSU's tool layer calls a synchronous approval function today. The broker
    keeps that contract while making the decision externally resolvable by the
    authorized Telegram user. Normal local approval policy still decides what
    needs approval; Telegram only supplies the user's answer.
    """

    def __init__(
        self,
        store: TelegramStateStore,
        callbacks: CallbackRegistry,
        *,
        formatter: TelegramFormatter | None = None,
        notify: Callable[[OutboundMessage], None] | None = None,
        decision_timeout_seconds: float = 900.0,
    ) -> None:
        self.store = store
        self.callbacks = callbacks
        self.formatter = formatter or TelegramFormatter()
        self.notify = notify
        self.decision_timeout_seconds = decision_timeout_seconds
        self._pending: dict[str, tuple[threading.Event, bool | None]] = {}
        self._lock = threading.Lock()

    def approval_func(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        session_id: str,
        run_id: str,
    ) -> Callable[[ApprovalRequest], bool]:
        def approve(request: ApprovalRequest) -> bool:
            approval_id = f"approval-{uuid.uuid4().hex[:16]}"
            pending = PendingApproval(
                approval_id=approval_id,
                run_id=run_id,
                session_id=session_id,
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                action_type=request.action_type,
                description=request.description,
                risk_level=request.risk_level,
                preview=request.preview or "",
                working_dir=request.working_dir or "",
                reason=request.reason or "",
            )
            event = threading.Event()
            with self._lock:
                self._pending[approval_id] = (event, None)
            if self.notify is not None:
                self.notify(
                    OutboundMessage(
                        chat_id=telegram_chat_id,
                        text=self.formatter.approval(pending),
                        reply_markup=approval_keyboard(
                            registry=self.callbacks,
                            telegram_user_id=telegram_user_id,
                            telegram_chat_id=telegram_chat_id,
                            session_id=session_id,
                            run_id=run_id,
                            approval_id=approval_id,
                        ),
                    )
                )
            self.store.audit(
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                session_id=session_id,
                run_id=run_id,
                action="approval.requested",
                result="pending",
                payload={"approval": asdict(pending)},
            )
            event.wait(self.decision_timeout_seconds)
            with self._lock:
                _event, decision = self._pending.pop(approval_id, (event, None))
            approved = bool(decision)
            self.store.audit(
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                session_id=session_id,
                run_id=run_id,
                action="approval.resolved",
                result="approved" if approved else "rejected",
                payload={"approval_id": approval_id},
            )
            return approved

        return approve

    def resolve(self, approval_id: str, approved: bool) -> bool:
        with self._lock:
            item = self._pending.get(approval_id)
            if item is None:
                return False
            event, _decision = item
            self._pending[approval_id] = (event, bool(approved))
            event.set()
        return True

    def pending_approval_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._pending)
