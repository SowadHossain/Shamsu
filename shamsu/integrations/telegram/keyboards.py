"""Inline keyboard builders for Telegram remote control."""
from __future__ import annotations

from shamsu.integrations.telegram.callbacks import CallbackRegistry
from shamsu.integrations.telegram.models import (
    CallbackAction,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    TelegramSessionSummary,
)


def home_keyboard(
    *,
    registry: CallbackRegistry,
    telegram_user_id: int,
    telegram_chat_id: int,
    session_id: str,
    run_id: str = "",
) -> InlineKeyboardMarkup:
    sessions_id = registry.create(
        CallbackAction.OPEN_SESSIONS,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        session_id=session_id,
        run_id=run_id,
    )
    status_id = registry.create(
        CallbackAction.OPEN_STATUS,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        session_id=session_id,
        run_id=run_id,
    )
    plan_id = registry.create(
        CallbackAction.OPEN_PLAN,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        session_id=session_id,
        run_id=run_id,
    )
    changes_id = registry.create(
        CallbackAction.OPEN_CHANGES,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        session_id=session_id,
        run_id=run_id,
    )
    pause_id = registry.create(
        CallbackAction.PAUSE_RUN,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        session_id=session_id,
        run_id=run_id,
    )
    cancel_id = registry.create(
        CallbackAction.CANCEL_RUN,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        session_id=session_id,
        run_id=run_id,
    )
    return InlineKeyboardMarkup(
        (
            (
                InlineKeyboardButton("Sessions", sessions_id),
                InlineKeyboardButton("Status", status_id),
            ),
            (
                InlineKeyboardButton("Plan", plan_id),
                InlineKeyboardButton("Changes", changes_id),
            ),
            (
                InlineKeyboardButton("Pause", pause_id),
                InlineKeyboardButton("Cancel", cancel_id),
            ),
        )
    )


def session_keyboard(
    sessions: list[TelegramSessionSummary],
    *,
    registry: CallbackRegistry,
    telegram_user_id: int,
    telegram_chat_id: int,
    active_session_id: str = "",
) -> InlineKeyboardMarkup:
    rows: list[tuple[InlineKeyboardButton, ...]] = []
    for session in sessions[:20]:
        action_id = registry.create(
            CallbackAction.SWITCH_SESSION,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            session_id=session.session_id,
            payload={"session_id": session.session_id},
        )
        suffix = " *" if session.session_id == active_session_id else ""
        rows.append((InlineKeyboardButton(f"{session.display_name}{suffix}", action_id),))
    rows.append((InlineKeyboardButton("+ New Session", "static:new"),))
    return InlineKeyboardMarkup(tuple(rows))


def run_control_keyboard(
    *,
    registry: CallbackRegistry,
    telegram_user_id: int,
    telegram_chat_id: int,
    session_id: str,
    run_id: str,
    include_resume: bool = True,
) -> InlineKeyboardMarkup:
    cancel_id = registry.create(
        CallbackAction.CANCEL_RUN,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        session_id=session_id,
        run_id=run_id,
    )
    pause_id = registry.create(
        CallbackAction.PAUSE_RUN,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        session_id=session_id,
        run_id=run_id,
    )
    rows = [(InlineKeyboardButton("Pause", pause_id), InlineKeyboardButton("Cancel", cancel_id))]
    if include_resume:
        resume_id = registry.create(
            CallbackAction.RESUME_RUN,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            session_id=session_id,
            run_id=run_id,
        )
        rows.append((InlineKeyboardButton("Resume", resume_id),))
    return InlineKeyboardMarkup(tuple(rows))


def approval_keyboard(
    *,
    registry: CallbackRegistry,
    telegram_user_id: int,
    telegram_chat_id: int,
    session_id: str,
    run_id: str,
    approval_id: str,
) -> InlineKeyboardMarkup:
    approve_id = registry.create(
        CallbackAction.APPROVE,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        session_id=session_id,
        run_id=run_id,
        approval_id=approval_id,
    )
    reject_id = registry.create(
        CallbackAction.REJECT,
        telegram_user_id=telegram_user_id,
        telegram_chat_id=telegram_chat_id,
        session_id=session_id,
        run_id=run_id,
        approval_id=approval_id,
    )
    return InlineKeyboardMarkup(
        ((InlineKeyboardButton("Approve", approve_id), InlineKeyboardButton("Reject", reject_id)),)
    )
