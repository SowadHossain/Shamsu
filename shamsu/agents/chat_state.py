"""Stateful chat message history for SHAMSU's ReAct loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shamsu.session.manager import SessionLogger

# Compaction: hydrate only the most recent N transcript turns. The session
# summary and long-term memory carry older context separately, so we never
# replay the entire history into the model.
HYDRATE_MAX_MESSAGES = 80


@dataclass
class ChatMessage:
    role: str
    content: str
    tool_call_id: str = ""
    name: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_ollama(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        if self.role == "tool":
            if self.tool_call_id:
                message["tool_call_id"] = self.tool_call_id
            if self.name:
                message["name"] = self.name
        return message


class ChatState:
    def __init__(
        self,
        system_prompt: str,
        session_logger: SessionLogger | None = None,
        hydrate: bool = True,
    ) -> None:
        self.system_prompt = system_prompt
        self.session_logger = session_logger
        self._messages: list[ChatMessage] = [ChatMessage("system", system_prompt)]
        if hydrate:
            self._hydrate_from_session()

    def append_user(self, content: str) -> None:
        self._append(ChatMessage("user", content), persist=True)

    def append_assistant(self, content: str, tool_calls: list[dict[str, Any]] | None = None) -> None:
        self._append(ChatMessage("assistant", content, tool_calls=tool_calls or []), persist=True)

    def append_tool(self, tool_call_id: str, name: str, content: str) -> None:
        self._append(ChatMessage("tool", content, tool_call_id=tool_call_id, name=name), persist=True)

    def messages(self, max_messages: int = 30) -> list[dict[str, Any]]:
        system = self._messages[0]
        tail = self._messages[1:][-max(max_messages - 1, 1):]
        return [system.to_ollama(), *[message.to_ollama() for message in tail]]

    @property
    def all_messages(self) -> list[ChatMessage]:
        return list(self._messages)

    def _append(self, message: ChatMessage, persist: bool) -> None:
        self._messages.append(message)
        if persist and self.session_logger:
            # Two sinks: the rich `chat.message` event (kept for the trace/audit
            # timeline and backward compatibility) and the compact, redacted
            # `messages.jsonl` transcript that hydration prefers.
            self.session_logger.log(
                "chat.message",
                {
                    "role": message.role,
                    "content": message.content,
                    "tool_call_id": message.tool_call_id,
                    "name": message.name,
                    "tool_calls": message.tool_calls,
                },
                f"Chat message appended: {message.role}",
                workflow_id="chat",
            )
            self.session_logger.append_message(
                message.role,
                message.content,
                tool_call_id=message.tool_call_id,
                name=message.name,
                tool_calls=message.tool_calls,
            )

    def _hydrate_from_session(self) -> None:
        if not self.session_logger:
            return
        # Prefer the clean transcript; only fall back to scanning events.jsonl
        # for chat.message when no transcript exists (older sessions).
        if self.session_logger.messages_path.exists():
            records = self.session_logger.read_messages(HYDRATE_MAX_MESSAGES)
            self._hydrate_records(records, key_content="content")
            return
        events = [
            event.get("payload", {})
            for event in self.session_logger.tail(HYDRATE_MAX_MESSAGES)
            if event.get("event_type") == "chat.message"
        ]
        self._hydrate_records(events, key_content="content")

    def _hydrate_records(self, records: list[dict[str, Any]], key_content: str) -> None:
        for payload in records:
            role = str(payload.get("role", "")).strip()
            content = str(payload.get(key_content, ""))
            if role in {"user", "assistant", "tool"} and _should_hydrate_chat_message(role, content):
                self._append(
                    ChatMessage(
                        role=role,
                        content=content,
                        tool_call_id=str(payload.get("tool_call_id", "")),
                        name=str(payload.get("name", "")),
                        tool_calls=_list_of_dicts(payload.get("tool_calls", [])),
                    ),
                    persist=False,
                )

def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _should_hydrate_chat_message(role: str, content: str) -> bool:
    if role != "assistant":
        return True
    normalized = " ".join(content.strip().lower().split())
    blocked_status_messages = {
        "shamsu is ready.",
        "shamsu is ready",
    }
    return normalized not in blocked_status_messages
