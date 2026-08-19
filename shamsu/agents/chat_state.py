"""Stateful chat message history for SHAMSU's ReAct loop."""
from __future__ import annotations

import re

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from shamsu.context.budget import PER_MESSAGE_OVERHEAD, message_tokens
from shamsu.session.manager import SessionLogger

# Compaction: hydrate only the most recent N transcript turns. The session
# summary and long-term memory carry older context separately, so we never
# replay the entire history into the model.
HYDRATE_MAX_MESSAGES = 24

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
    # Placeholder left by replace_last_assistant. Replaying it would teach the
    # model that emitting an unparseable call is a normal way to end a turn.
    "(unparseable tool call omitted",
    # Simple mode's own stops. These are the HARNESS speaking, not the model,
    # and replaying them as assistant turns teaches the model that "I stopped
    # after 24 steps" is a normal way to end one. Live 2026-08-18 a session
    # carried "The model did not respond within 600s." into every later turn.
    "the model did not respond within",
    "the model returned an empty reply",
)

# The same thing, where a leading number or path makes a prefix unsafe - "I
# tried..." is perfectly ordinary model prose, so these must match precisely.
_HARNESS_STATUS_PATTERNS = (
    re.compile(r"^i stopped after \d+ steps? without finishing"),
    re.compile(r"^i tried \d+ edits? in a row that changed nothing"),
    re.compile(r"^i have now changed .+ \d+ times in this turn"),
)

# How many times the same assistant answer may be replayed before the rest are
# dropped. Repetition is what the model latches onto.
_MAX_REPEATED_HYDRATED = 2

_INTERNAL_USER_CONTEXT_MARKERS = (
    "\n\n## SHAMSU Task Harness",
    "\n\n## Active SHAMSU Skills",
    "\n\n## Deterministic Context",
    "\n\nAdditional SHAMSU context:",
    "\n\nWorkspace root:",
    "\n\nMentioned file context:",
    "\n\nPlan from planner model:",
    "\n\nCurrent task state:",
)


@dataclass
class ChatMessage:
    role: str
    content: str
    tool_call_id: str = ""
    name: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Set once this message has had its payload shrunk for the prompt. The
    # on-disk transcript still holds the original; this flag only stops the
    # elision doing the same work again every round.
    elided: bool = False

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
        hydrate_max_messages: int = HYDRATE_MAX_MESSAGES,
        known_tools: frozenset[str] | None = None,
    ) -> None:
        self.system_prompt = system_prompt
        self.session_logger = session_logger
        # Tool names this caller can actually execute. A transcript is shared by
        # every SHAMSU that opens the workspace, and the legacy router speaks a
        # DIFFERENT vocabulary - `project.inspect`, `file.read`, `code.search`,
        # `test.run`. Live 2026-08-18 those landed in a simple-mode session, and
        # the model, seeing calls it appeared to have made itself, kept making
        # them. A model imitates its own transcript, so history written by
        # another agent is not merely untidy: it is instructions.
        self.known_tools = frozenset(known_tools) if known_tools else None
        # How far back to rehydrate. The 24 default is an 8k-era number and is a
        # HARD horizon, applied before any token budgeting: a caller that builds
        # a fresh ChatState per user message can never see further back than
        # this, however large its context window. Live 2026-08-17, simple mode
        # emits ~5 messages per turn, so 24 meant the agent remembered five
        # turns of a twenty-turn session and started guessing at the project.
        self.hydrate_max_messages = max(1, int(hydrate_max_messages))
        self._messages: list[ChatMessage] = [ChatMessage("system", system_prompt)]
        # Rolling summary of turns evicted by budget-aware trimming, and the
        # absolute index in _messages up to which history has been folded in
        # (1 == nothing summarized yet; index 0 is the system prompt).
        self._rolling_summary: str = ""
        self._summarized_upto: int = 1
        # Transcript records that hydration did NOT load. Needed to translate
        # the persisted watermark, which counts records in the whole
        # transcript, into an index in this hydrated window.
        self._hydration_offset: int = 0
        if hydrate:
            self._hydrate_from_session()
            self._restore_summary()

    def append_user(self, content: str, persisted_content: str | None = None) -> None:
        self._append(
            ChatMessage("user", content),
            persist=True,
            persisted_content=(
                _clean_user_content_for_persistence(persisted_content)
                if persisted_content is not None
                else None
            ),
        )

    def append_assistant(self, content: str, tool_calls: list[dict[str, Any]] | None = None) -> None:
        self._append(ChatMessage("assistant", content, tool_calls=tool_calls or []), persist=True)

    def replace_last_assistant(self, content: str) -> None:
        """Swap the most recent assistant turn for *content*, in memory only.

        Used to evict an unparseable tool call from the PROMPT. An identical
        prefix is what makes a deterministic retry byte-identical, and leaving a
        broken payload in context invites the model to copy it verbatim. The
        on-disk transcript keeps the original as evidence, so this deliberately
        does not persist.
        """
        for message in reversed(self._messages):
            if message.role == "assistant":
                message.content = content
                message.tool_calls = []
                return

    def set_system_prompt(self, content: str) -> None:
        """Replace the system prompt in place, keeping history intact.

        Used to re-inject per-workspace standing instructions every turn. It
        updates ``_messages[0]``, which is never evicted by the budget trimmer and
        is what ``select_for_budget`` charges against the budget, so refreshed
        instructions are always both present and accounted for.
        """
        if content == self.system_prompt:
            return
        self.system_prompt = content
        self._messages[0].content = content

    def append_tool(self, tool_call_id: str, name: str, content: str) -> None:
        self._append(ChatMessage("tool", content, tool_call_id=tool_call_id, name=name), persist=True)

    def messages(self, max_messages: int = 30) -> list[dict[str, Any]]:
        system = self._messages[0]
        tail = self._messages[1:][-max(max_messages - 1, 1):]
        return [system.to_ollama(), *[message.to_ollama() for message in tail]]

    def select_for_budget(
        self,
        max_tokens: int,
        token_counter: Callable[[str], int],
        *,
        per_message_overhead: int = PER_MESSAGE_OVERHEAD,
    ) -> tuple[list[ChatMessage], int]:
        """Pick the largest recent suffix of history that fits in *max_tokens*,
        always keeping at least the most recent message. Returns
        ``(tail, start_abs)`` where ``start_abs`` is the absolute index in
        ``_messages`` at which the kept suffix begins (1 == nothing evicted).
        The start is snapped forward to a user-message boundary when that still
        leaves a non-empty tail, so assistant/tool-call sequences stay intact.
        This is the token-aware replacement for the flat ``messages(30)`` cap.

        Each message is charged for its `tool_calls` and its chat-template
        envelope as well as its text - see `message_tokens`. Charging content
        alone is what let a prompt reach ~31,400 tokens of a 32,768 window while
        this method believed it had built one of 21,381.

        ``per_message_overhead`` is exposed so a test can isolate the selection
        logic from the envelope constant."""
        history = self._messages[1:]
        if not history:
            return [], 1
        # ``max_tokens`` is the budget for the complete message list, not just
        # conversation history. Charge the always-present system prompt first -
        # envelope included, since it is a message like any other.
        history_budget = max(
            0, max_tokens - token_counter(self.system_prompt) - per_message_overhead
        )
        used = 0
        start_rel = len(history)
        for i in range(len(history) - 1, -1, -1):
            cost = message_tokens(
                history[i], token_counter, overhead=per_message_overhead
            )
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

    def elide_old_payloads(
        self,
        keep_recent: int,
        elide: Callable[[ChatMessage], tuple[str, list[dict[str, Any]]] | None],
    ) -> int:
        """Shrink the payloads of messages older than the last *keep_recent*.

        Code enters the conversation twice - once as the `write_file` content
        inside `tool_calls`, once as the body of a `read_file` result - and both
        have served their purpose the moment the call returns. What is worth
        keeping afterwards is which file, and what changed. Measured on a real
        130-message session: 44,833 tokens verbatim against 10,476 with older
        payloads elided, which takes a session from ~13 turns before the window
        fills to ~57.

        Lossless for file payloads, because the file is still on disk and
        `read_file` fetches it back. The caller decides what is safe to elide -
        shell output is NOT recoverable and must be compacted, not dropped.

        Two invariants:

        * this rewrites messages IN MEMORY only. `messages.jsonl` keeps every
          byte, which is what makes the archive lossless and the elision safe.
        * no message is ever removed, so a `tool_call_id` can never be orphaned
          from the assistant turn that owns it.
        """
        history = self._messages[1:]
        cutoff = max(0, len(history) - max(0, keep_recent))
        changed = 0
        for message in history[:cutoff]:
            if message.elided:
                continue
            replacement = elide(message)
            if replacement is None:
                continue
            content, tool_calls = replacement
            if content == message.content and tool_calls == message.tool_calls:
                message.elided = True
                continue
            message.content = content
            message.tool_calls = tool_calls
            message.elided = True
            changed += 1
        return changed

    def newly_evicted(self, start_abs: int) -> list[ChatMessage]:
        """History messages being dropped this round that haven't yet been
        folded into the rolling summary: ``_messages[_summarized_upto:start_abs]``."""
        lower = max(1, self._summarized_upto)
        return self._messages[lower:start_abs]

    def update_rolling_summary(self, summary: str, start_abs: int) -> None:
        self._rolling_summary = summary
        self._summarized_upto = max(self._summarized_upto, start_abs)
        # Persist immediately. Held only in memory, this was rebuilt from
        # whatever hydration loaded, so turns past the horizon were evicted,
        # never summarised, and lost for good.
        if self.session_logger is not None:
            try:
                self.session_logger.save_summary(
                    self._rolling_summary, self._transcript_watermark()
                )
            except Exception:
                pass  # a failed save must never break the turn

    def _transcript_watermark(self) -> int:
        """`_summarized_upto`, expressed in whole-transcript terms.

        The in-memory index is meaningless to the next process: it counts
        positions in THIS hydrated window. Persisting it directly is what made
        the watermark untranslatable and forced the reset that re-summarised
        everything every turn. Adding back what hydration skipped makes it a
        number the next session can actually use.
        """
        return self._hydration_offset + max(0, self._summarized_upto - 1)

    def _restore_summary(self) -> None:
        if self.session_logger is None:
            return
        try:
            summary, upto = self.session_logger.load_summary()
        except Exception:
            return
        if summary.strip():
            self._rolling_summary = summary
            # `upto` counts records in the WHOLE transcript that the summary
            # already covers. Hydration loaded only the tail, so translate it
            # into this window rather than giving up and starting from 1.
            #
            # Giving up is what shipped: a fresh ChatState is built per user
            # turn, so `newly_evicted()` returned the entire history every
            # turn and each one spent a model call re-summarising the same
            # messages. Measured live 2026-08-19: session.json held 24, the
            # loop used 1, and the same 23 messages were compacted every turn
            # for a whole session.
            already_summarised = max(0, int(upto) - self._hydration_offset)
            self._summarized_upto = max(
                1, min(1 + already_summarised, len(self._messages))
            )

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

    def _append(
        self,
        message: ChatMessage,
        persist: bool,
        persisted_content: str | None = None,
    ) -> None:
        self._messages.append(message)
        if persist and self.session_logger:
            stored_content = persisted_content if persisted_content is not None else message.content
            # Two sinks: the rich `chat.message` event (kept for the trace/audit
            # timeline and backward compatibility) and the compact, redacted
            # `messages.jsonl` transcript that hydration prefers.
            self.session_logger.log(
                "chat.message",
                {
                    "role": message.role,
                    "content": stored_content,
                    "tool_call_id": message.tool_call_id,
                    "name": message.name,
                    "tool_calls": message.tool_calls,
                },
                f"Chat message appended: {message.role}",
                workflow_id="chat",
            )
            self.session_logger.append_message(
                message.role,
                stored_content,
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
            # Read all, then window it here: `read_messages` reads the whole
            # file either way, and the count of what was SKIPPED is what makes
            # the summary watermark translatable.
            every = self.session_logger.read_messages(None)
            records = every[-self.hydrate_max_messages:] if self.hydrate_max_messages else every
            self._hydration_offset = max(0, len(every) - len(records))
            self._hydrate_records(records, key_content="content")
            return
        events = [
            event.get("payload", {})
            for event in self.session_logger.tail(self.hydrate_max_messages)
            if event.get("event_type") == "chat.message"
        ]
        self._hydrate_records(events, key_content="content")

    def _hydrate_records(self, records: list[dict[str, Any]], key_content: str) -> None:
        seen_assistant: dict[str, int] = {}
        foreign_call_ids: set[str] = set()
        for payload in records:
            role = str(payload.get("role", "")).strip()
            content = str(payload.get(key_content, ""))
            if role == "user":
                content = _clean_user_content_for_persistence(content)
            tool_calls = _list_of_dicts(payload.get("tool_calls", []))
            if self.known_tools is not None:
                if role == "assistant" and tool_calls:
                    kept = [c for c in tool_calls if _call_tool_name(c) in self.known_tools]
                    if len(kept) != len(tool_calls):
                        foreign_call_ids.update(
                            str(c.get("id") or "")
                            for c in tool_calls
                            if _call_tool_name(c) not in self.known_tools
                        )
                        tool_calls = kept
                        # An assistant turn that was ONLY a foreign call carries
                        # nothing else worth replaying.
                        if not kept and not content.strip():
                            continue
                if role == "tool":
                    name = str(payload.get("name", "")).strip()
                    call_id = str(payload.get("tool_call_id", "")).strip()
                    if (name and name not in self.known_tools) or (
                        call_id and call_id in foreign_call_ids
                    ):
                        continue
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
                        tool_calls=tool_calls,
                    ),
                    persist=False,
                )

def _call_tool_name(call: dict[str, Any]) -> str:
    """The tool a persisted tool_call names, whichever shape it was stored in."""
    function = call.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip()
    return str(call.get("name") or "").strip()


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _should_hydrate_chat_message(role: str, content: str) -> bool:
    if role == "user" and not str(content or "").strip():
        return False
    if role != "assistant":
        return True
    normalized = " ".join(content.strip().lower().split())
    if not normalized:
        return True
    if normalized.startswith(_HARNESS_STATUS_PREFIXES):
        return False
    return not any(pattern.match(normalized) for pattern in _HARNESS_STATUS_PATTERNS)


def _clean_user_content_for_persistence(content: str | None) -> str:
    text = str(content or "")
    cut_at = len(text)
    for marker in _INTERNAL_USER_CONTEXT_MARKERS:
        index = text.find(marker)
        if index >= 0:
            cut_at = min(cut_at, index)
    return text[:cut_at].strip()
