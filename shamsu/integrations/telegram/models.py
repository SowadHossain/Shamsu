"""Typed Telegram remote-control contracts.

These types are intentionally transport-shaped, not agent-shaped: Telegram is a
UI for the existing SHAMSU runtime, so none of these classes own task execution.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RemoteControlStatus(str, Enum):
    DISABLED = "disabled"
    STARTING = "starting"
    WAITING_FOR_PAIR = "waiting_for_pair"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"


class PermissionLevel(str, Enum):
    OWNER = "owner"
    OPERATOR = "operator"
    VIEWER = "viewer"


class CallbackAction(str, Enum):
    SWITCH_SESSION = "switch_session"
    CANCEL_RUN = "cancel_run"
    PAUSE_RUN = "pause_run"
    RESUME_RUN = "resume_run"
    APPROVE = "approve"
    REJECT = "reject"
    OPEN_STATUS = "open_status"
    OPEN_SESSIONS = "open_sessions"
    OPEN_PLAN = "open_plan"
    OPEN_CHANGES = "open_changes"
    PAGE = "page"


@dataclass(frozen=True)
class TelegramUser:
    user_id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""

    @property
    def display_name(self) -> str:
        name = " ".join(part for part in (self.first_name, self.last_name) if part).strip()
        return name or (f"@{self.username}" if self.username else str(self.user_id))


@dataclass(frozen=True)
class TelegramChat:
    chat_id: int
    chat_type: str = "private"
    title: str = ""

    @property
    def is_private(self) -> bool:
        return self.chat_type == "private"


@dataclass(frozen=True)
class TelegramFile:
    file_id: str
    file_name: str
    mime_type: str = ""
    file_size: int = 0


@dataclass(frozen=True)
class TelegramMessage:
    message_id: int
    user: TelegramUser
    chat: TelegramChat
    text: str = ""
    date: str = field(default_factory=utc_now)
    document: TelegramFile | None = None


@dataclass(frozen=True)
class TelegramCallbackQuery:
    callback_query_id: str
    user: TelegramUser
    message: TelegramMessage
    data: str


@dataclass(frozen=True)
class TelegramUpdate:
    update_id: int
    message: TelegramMessage | None = None
    callback_query: TelegramCallbackQuery | None = None


@dataclass(frozen=True)
class TelegramInboundMetadata:
    source: str
    telegram_user_id: int
    telegram_chat_id: int
    telegram_message_id: int
    session_id: str
    timestamp: str


@dataclass(frozen=True)
class InlineKeyboardButton:
    text: str
    callback_data: str


@dataclass(frozen=True)
class InlineKeyboardMarkup:
    rows: tuple[tuple[InlineKeyboardButton, ...], ...] = ()

    def to_telegram(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": button.text, "callback_data": button.callback_data} for button in row]
                for row in self.rows
            ]
        }


@dataclass(frozen=True)
class OutboundMessage:
    chat_id: int
    text: str
    reply_markup: InlineKeyboardMarkup | None = None
    edit_message_id: int | None = None
    document_path: Path | None = None
    #: `"HTML"` or `"MarkdownV2"`. Empty means plain text, which is what every
    #: existing caller sends and what stays safest for arbitrary agent output -
    #: an unescaped `<` in plain mode is a character, in HTML mode it is a
    #: parse error that Telegram rejects the whole message for.
    parse_mode: str = ""


@dataclass(frozen=True)
class TelegramSessionSummary:
    session_id: str
    display_name: str
    workspace: str
    status: str = "idle"
    current_task: str = ""
    current_run: str = ""
    active_branch: str = ""
    last_activity: str = ""


@dataclass(frozen=True)
class TelegramRuntimeStatus:
    session: TelegramSessionSummary | None
    status: str = "idle"
    task: str = ""
    phase: str = ""
    plan_completed: int = 0
    plan_total: int = 0
    current_step: str = ""
    actions: int = 0
    max_actions: int = 4
    last_action: str = ""
    updated_at: str = ""
    run_id: str = ""


@dataclass(frozen=True)
class TelegramSettings:
    task_progress: str = "important"
    task_completion: bool = True
    approval_requests: bool = True
    failures: bool = True
    tool_by_tool_updates: bool = False


@dataclass(frozen=True)
class PendingApproval:
    approval_id: str
    run_id: str
    session_id: str
    telegram_user_id: int
    telegram_chat_id: int
    action_type: str
    description: str
    risk_level: str
    preview: str = ""
    working_dir: str = ""
    reason: str = ""
    created_at: str = field(default_factory=utc_now)
    resolved: bool = False


@dataclass(frozen=True)
class CallbackValidationResult:
    ok: bool
    action: CallbackAction | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
