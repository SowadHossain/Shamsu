"""Notification filtering and outbound helpers."""
from __future__ import annotations

from shamsu.integrations.telegram.models import OutboundMessage, TelegramSettings


class NotificationFilter:
    def should_send(self, event_type: str, settings: TelegramSettings) -> bool:
        if event_type == "task_progress":
            return settings.task_progress != "off"
        if event_type == "task_completed":
            return settings.task_completion
        if event_type == "approval_required":
            return settings.approval_requests
        if event_type == "failure":
            return settings.failures
        if event_type == "tool":
            return settings.tool_by_tool_updates
        return True


class NotificationSink:
    def __init__(self) -> None:
        self.messages: list[OutboundMessage] = []

    def record(self, message: OutboundMessage) -> None:
        self.messages.append(message)

