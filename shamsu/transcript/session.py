"""An append-only conversation whose prefix stays byte-stable.

Why this exists
---------------
``shamsu/context/compiler.py`` builds exactly two messages for every model call —
a system prompt and a freshly rendered state frame — and its output contract tells
the model "Do not rely on old chat turns as state." Two consequences follow, and
both are load-bearing failures:

1. The model never sees its own previous output. Its plan, its reasoning and the
   code it just wrote come back as harness-written paraphrases in a different
   voice. Continuing its own text is the strongest behaviour a small model has,
   and the frame deliberately removes it.
2. ``PHASE`` is the first section of that frame and flips AUTHOR/VERIFY/REPAIR
   between calls, so the prompt mutates at roughly token one. Ollama can only
   reuse a cached prefix that is byte-identical from the start, so every call
   re-prefills the whole window from scratch.

A :class:`Transcript` fixes both by construction: the system message is frozen at
build time, nothing already appended is ever rewritten, and compaction removes
whole messages rather than re-summarising the ones that stay. New turns therefore
extend a prefix the server has already cached.

The rule to preserve when editing this file: **never mutate the content of a
message that has already been sent.** Rewriting message *k* invalidates the cache
for every token from *k* onwards, which is the exact cost this class exists to
avoid.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shamsu.context.budget import count_tokens

# Turns kept verbatim at the tail when compacting. Four covers the working set a
# repair needs: the milestone instruction, the model's code, the verifier's error
# and the model's fix. Dropping below that starts cutting a repair off from the
# failure it is repairing.
DEFAULT_KEEP_TAIL = 8

# Compaction begins at this fraction of the window rather than at the window
# itself, so a long model answer cannot overflow between the check and the call.
COMPACT_AT = 0.75


@dataclass
class Message:
    """One turn. ``pinned`` messages survive compaction.

    The plan is pinned: it is the document every later "next" continues, so
    dropping it is what turns a coherent build into an amnesiac one.
    """

    role: str
    content: str
    pinned: bool = False

    def as_payload(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class Transcript:
    """An append-only message list with cache-preserving compaction."""

    def __init__(self, system: str, *, max_tokens: int = 32768) -> None:
        # Frozen deliberately. Anything that varies per call — phase, step index,
        # timestamps, counters — must live in the newest user message instead, or
        # it moves the cache boundary to the very first token.
        self._system = Message("system", system.strip(), pinned=True)
        self.max_tokens = int(max_tokens)
        self._messages: list[Message] = []
        self._dropped = 0
        self._compactions = 0

    # -- reading -------------------------------------------------------------

    @property
    def system_prompt(self) -> str:
        return self._system.content

    @property
    def dropped_messages(self) -> int:
        """How many turns compaction has removed over the session's life."""
        return self._dropped

    @property
    def compactions(self) -> int:
        return self._compactions

    def __len__(self) -> int:
        return len(self._messages) + 1

    def messages(self) -> list[dict[str, str]]:
        """The payload for ``LLMManager.chat_with_tools``."""
        return [self._system.as_payload()] + [m.as_payload() for m in self._messages]

    def token_estimate(self) -> int:
        return count_tokens(
            "\n".join([self._system.content] + [m.content for m in self._messages])
        )

    def last_of(self, role: str) -> str:
        for message in reversed(self._messages):
            if message.role == role:
                return message.content
        return ""

    # -- writing -------------------------------------------------------------

    def append(self, role: str, content: str, *, pinned: bool = False) -> Message:
        """Add a turn. The only supported way to change a transcript."""
        message = Message(role=str(role), content=str(content), pinned=bool(pinned))
        self._messages.append(message)
        return message

    def append_user(self, content: str) -> Message:
        return self.append("user", content)

    def append_assistant(self, content: str, *, pinned: bool = False) -> Message:
        return self.append("assistant", content, pinned=pinned)

    def pin_last(self) -> None:
        """Mark the newest turn as surviving compaction.

        Used for the plan: it is appended by the same call path as any other
        answer, and only the caller knows it is the document later turns
        continue.
        """
        if self._messages:
            self._messages[-1].pinned = True

    def append_tool_result(self, summary: str) -> Message:
        """Feed an observation back as a user turn.

        Deliberately ``user`` and not ``tool``: this route salvages file writes
        out of ordinary prose (see ``_salvage_raw_tool_fences``) rather than
        going through native tool calling, so there is no ``tool_call_id`` for a
        ``tool`` message to answer. Ollama accepts an orphaned tool message from
        some templates and errors on others; a user turn works on every model and
        reads to the model exactly like a human pasting an error back.
        """
        return self.append("user", summary)

    # -- compaction ----------------------------------------------------------

    def needs_compaction(self, headroom_tokens: int = 0) -> bool:
        budget = (self.max_tokens - max(0, headroom_tokens)) * COMPACT_AT
        return self.token_estimate() > budget

    def compact(self, *, keep_tail: int = DEFAULT_KEEP_TAIL) -> int:
        """Drop whole unpinned messages from the middle. Returns the count.

        Middle-dropping rather than re-summarising is the point. A rolling
        summary rewrites the head of the prompt on every turn and costs a full
        re-prefill each time — the very behaviour this route replaces. Dropping
        message *k* invalidates the cache only from *k* onward, and only on the
        turns where it actually happens, which is rare compared to every call.
        """
        if len(self._messages) <= keep_tail:
            return 0
        head_kept: list[Message] = []
        droppable: list[int] = []
        tail_start = len(self._messages) - keep_tail
        for index, message in enumerate(self._messages):
            if index >= tail_start or message.pinned:
                continue
            droppable.append(index)
        if not droppable:
            return 0
        budget = (self.max_tokens * COMPACT_AT)
        removed: set[int] = set()
        # Oldest first: the newest turns are the ones the model is continuing.
        for index in droppable:
            if count_tokens(
                "\n".join(
                    [self._system.content]
                    + [m.content for i, m in enumerate(self._messages) if i not in removed]
                )
            ) <= budget:
                break
            removed.add(index)
        if not removed:
            return 0
        head_kept = [m for i, m in enumerate(self._messages) if i not in removed]
        self._messages = head_kept
        self._dropped += len(removed)
        self._compactions += 1
        return len(removed)

    # -- diagnostics ---------------------------------------------------------

    def shared_prefix_tokens(self, previous: list[dict[str, Any]]) -> int:
        """Tokens this transcript shares with a previously sent payload.

        This is the number the route exists to keep high; ``build.py`` records it
        per call so a run can be checked for cache reuse instead of assumed to
        have it. A rebuilt state frame scores ~0 here on every call.
        """
        current = self.messages()
        shared = 0
        for old, new in zip(previous, current):
            if old.get("role") != new.get("role"):
                break
            old_content = str(old.get("content") or "")
            new_content = str(new.get("content") or "")
            if old_content != new_content:
                break
            shared += count_tokens(new_content)
        return shared
