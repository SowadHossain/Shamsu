"""Stateful chat message history for SHAMSU's ReAct loop."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from shamsu.session.manager import SessionLogger

# Compaction: hydrate only the most recent N transcript turns. The session
# summary and long-term memory carry older context separately, so we never
# replay the entire history into the model.
HYDRATE_MAX_MESSAGES = 80

# Harness-authored status text, not model output. Replaying it teaches the
# model that a turn ends in this text and it starts producing the text instead
# of acting - observed live 2026-08-02, where a session accumulated ten
# identical "could you provide the full path" turns and then wrote nothing for
# the rest of its life. Old turns are still summarized elsewhere.
_HARNESS_STATUS_PREFIXES = (
    "i did not complete the requested workspace change",
    "i said i would take an action",
    "local ai failed safely",
    "the model call timed out",
    "shamsu is ready",
)

# How many times the same assistant answer may be replayed before the rest are
# dropped. Repetition is what the model latches onto.
_MAX_REPEATED_HYDRATED = 2


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
        # Rolling summary of turns evicted by budget-aware trimming, and the
        # absolute index in _messages up to which history has been folded in
        # (1 == nothing summarized yet; index 0 is the system prompt).
        self._rolling_summary: str = ""
        self._summarized_upto: int = 1
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

    def select_for_budget(
        self, max_tokens: int, token_counter: Callable[[str], int]
    ) -> tuple[list[ChatMessage], int]:
        """Pick the largest recent suffix of history whose content fits in
        *max_tokens*, always keeping at least the most recent message. Returns
        ``(tail, start_abs)`` where ``start_abs`` is the absolute index in
        ``_messages`` at which the kept suffix begins (1 == nothing evicted).
        The start is snapped forward to a user-message boundary when that still
        leaves a non-empty tail, so assistant/tool-call sequences stay intact.
        This is the token-aware replacement for the flat ``messages(30)`` cap."""
        history = self._messages[1:]
        if not history:
            return [], 1
        # ``max_tokens`` is the budget for the complete message list, not just
        # conversation history. Charge the always-present system prompt first.
        history_budget = max(0, max_tokens - token_counter(self.system_prompt))
        used = 0
        start_rel = len(history)
        for i in range(len(history) - 1, -1, -1):
            cost = token_counter(history[i].content)
            if start_rel != len(history) and used + cost > history_budget:
                break
            used += cost
            start_rel = i
        snapped = start_rel
        while snapped < len(history) and history[snapped].role != "user":
            snapped += 1
        if snapped < len(history):
            start_rel = snapped
        start_abs = start_rel + 1
        return self._messages[start_abs:], start_abs

    def newly_evicted(self, start_abs: int) -> list[ChatMessage]:
        """History messages being dropped this round that haven't yet been
        folded into the rolling summary: ``_messages[_summarized_upto:start_abs]``."""
        lower = max(1, self._summarized_upto)
        return self._messages[lower:start_abs]

    def update_rolling_summary(self, summary: str, start_abs: int) -> None:
        self._rolling_summary = summary
        self._summarized_upto = max(self._summarized_upto, start_abs)

    @property
    def rolling_summary(self) -> str:
        return self._rolling_summary

    def build_ollama_messages(
        self, tail: list[ChatMessage], include_summary: bool
    ) -> list[dict[str, Any]]:
        """Assemble the final message list: system prompt, an optional rolling
        summary of older turns, then the recent tail."""
        messages = [self._messages[0].to_ollama()]
        if include_summary and self._rolling_summary.strip():
            messages.append(
                {
                    "role": "system",
                    "content": f"Summary of earlier conversation:\n{self._rolling_summary}",
                }
            )
        messages.extend(message.to_ollama() for message in tail)
        return messages

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
        seen_assistant: dict[str, int] = {}
        for payload in records:
            role = str(payload.get("role", "")).strip()
            content = str(payload.get(key_content, ""))
            if role == "assistant":
                # Drop repeats of an identical answer. A model that stalled the
                # same way five times must not be shown five examples of it.
                key = " ".join(content.strip().lower().split())
                if key:
                    seen_assistant[key] = seen_assistant.get(key, 0) + 1
                    if seen_assistant[key] > _MAX_REPEATED_HYDRATED:
                        continue
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
    if not normalized:
        return True
    return not normalized.startswith(_HARNESS_STATUS_PREFIXES)
