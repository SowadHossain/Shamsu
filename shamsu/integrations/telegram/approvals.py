"""Telegram approval bridge for SHAMSU's existing approval policy."""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

from shamsu.control.store import ALLOW, DENY, ControlStore
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

    The decision now lives in the shared control store rather than in this
    object, so the same question can be answered from the terminal or the
    browser and every surface agrees on who answered first. The in-process
    `threading.Event` is kept as the fast path - the run is in *this* process,
    so a button press here should not wait for a poll - but the store is the
    arbiter, and a decision made elsewhere ends the wait just as well.
    """

    def __init__(
        self,
        store: TelegramStateStore,
        callbacks: CallbackRegistry,
        *,
        formatter: TelegramFormatter | None = None,
        notify: Callable[[OutboundMessage], None] | None = None,
        decision_timeout_seconds: float = 900.0,
        control: ControlStore | None = None,
    ) -> None:
        self.store = store
        self.callbacks = callbacks
        self.formatter = formatter or TelegramFormatter()
        self.notify = notify
        self.decision_timeout_seconds = decision_timeout_seconds
        self._control = control
        self._pending: dict[str, tuple[threading.Event, bool | None]] = {}
        self._lock = threading.Lock()

    @property
    def control(self) -> ControlStore:
        """The shared arbiter. Built lazily so importing this costs no file."""
        if self._control is None:
            self._control = ControlStore()
        return self._control

    def approval_func(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        session_id: str,
        run_id: str,
        workspace: Path | None = None,
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
            # Also published to the shared store, under the SAME id, so the
            # terminal and the browser can show and answer this exact question.
            self.control.raise_approval(
                workspace=workspace or Path.cwd(),
                session_id=session_id,
                run_id=run_id,
                action_type=request.action_type,
                description=request.description,
                risk_level=request.risk_level,
                preview=request.preview or "",
                timeout_seconds=self.decision_timeout_seconds,
                approval_id=approval_id,
            )
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
            # Whoever answers first. The local event is the fast path for a
            # button pressed in this process; the store carries an answer from
            # the terminal or the browser. Waiting on only one of them would
            # make "answer from anywhere" mean "anywhere that happens to be
            # here".
            approved = self._await_decision(approval_id, event)
            with self._lock:
                self._pending.pop(approval_id, None)
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

    def _await_decision(self, approval_id: str, event: threading.Event) -> bool:
        deadline = time.monotonic() + self.decision_timeout_seconds
        while time.monotonic() < deadline:
            if event.wait(0.25):
                with self._lock:
                    _event, decision = self._pending.get(approval_id, (event, None))
                if decision is not None:
                    return bool(decision)
            record = self.control.approval(approval_id)
            if record is not None and record.decision:
                return record.decision == ALLOW
            if record is None:
                break
        # Nobody answered. Fail closed, and record it so every surface stops
        # showing a question that is no longer live.
        self.control.resolve_approval(approval_id, DENY, "timeout")
        return False

    def resolve(self, approval_id: str, approved: bool) -> bool:
        """A button was pressed here. The store still decides who won."""
        won = self.control.resolve_approval(
            approval_id, ALLOW if approved else DENY, "telegram"
        )
        with self._lock:
            item = self._pending.get(approval_id)
            if item is not None:
                event, _decision = item
                self._pending[approval_id] = (event, bool(approved))
                event.set()
        return won or item is not None

    def pending_approval_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._pending)
