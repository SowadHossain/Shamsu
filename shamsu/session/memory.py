"""Compact conversation memory built from workspace-local session logs."""
from __future__ import annotations

import re
from dataclasses import dataclass

from shamsu.session.manager import SessionLogger


FOLLOWUP_WEB = {
    "check on the web",
    "look it up",
    "search the web",
    "use the web",
    "check online",
    "search online",
}
FOLLOWUP_BROWSER = {
    "open it",
    "check it in the browser",
    "open it in the browser",
    "use the browser",
}
FOLLOWUP_GENERIC = {
    "do it",
    "do that",
    "that one",
    "what about that",
    "what about it",
    "continue",
}

# Affirmative / negative replies that resolve a stored pending action instead of
# entering the model/tool loop as a fresh, context-free prompt.
AFFIRMATIVE_REPLIES = {
    "yes", "yep", "yeah", "yup", "sure", "ok", "okay", "k", "do it", "go ahead",
    "proceed", "continue", "confirm", "confirmed", "please do", "go for it",
    "sounds good", "affirmative", "y", "next", "next one", "keep going",
    "carry on", "go on",
}
NEGATIVE_REPLIES = {
    "no", "nope", "nah", "cancel", "stop", "don't", "dont", "do not",
    "abort", "never mind", "nevermind", "forget it", "n",
}

# Discourse markers people put in front of a one-word answer. "okay proceed" is
# the same instruction as "proceed", but every affirmative check matched the
# whole string, so a single filler word dropped the reply through to routing and
# "okay proceed" was answered as a fresh question instead of continuing the task.
_FILLER_PREFIXES = {
    "okay", "ok", "alright", "allright", "right", "well", "so", "now", "then",
    "please", "lets", "let's", "just", "and", "also", "um", "uh", "hmm",
    "cool", "great", "nice", "fine", "perfect", "good",
}


def strip_filler_prefix(text: str) -> str:
    """Drop leading discourse markers, but never reduce a reply to nothing.

    "okay" on its own is an answer; "okay proceed" is the same answer with a
    filler in front. Stripping stops as soon as one token is left.
    """
    tokens = text.split()
    while len(tokens) > 1 and tokens[0] in _FILLER_PREFIXES:
        tokens = tokens[1:]
    return " ".join(tokens)


def _normalize_reply(text: str) -> str:
    return strip_filler_prefix(re.sub(r"[^a-z0-9' ]", "", text.lower()).strip())


def is_affirmative(text: str) -> bool:
    """True for a bare yes/continue/do-it style confirmation (not a sentence)."""
    normalized = _normalize_reply(text)
    return normalized in AFFIRMATIVE_REPLIES


def is_negative(text: str) -> bool:
    """True for a bare no/cancel/stop style rejection (not a sentence)."""
    normalized = _normalize_reply(text)
    return normalized in NEGATIVE_REPLIES


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    text: str


@dataclass(frozen=True)
class ConversationMemory:
    turns: list[ConversationTurn]
    resume_summary: str = ""
    task_state: str = ""

    @classmethod
    def from_session(cls, logger: SessionLogger | None, max_events: int = 80) -> "ConversationMemory":
        if logger is None:
            return cls([])
        turns: list[ConversationTurn] = []
        for event in logger.tail(max_events):
            event_type = event.get("event_type", "")
            payload = event.get("payload", {})
            if event_type == "chat.message":
                # The ReAct loop writes user/assistant/tool as chat.message.
                # Tool turns are machine output, not natural-language context
                # for follow-up resolution, so they are skipped here.
                role = str(payload.get("role", "")).strip()
                content = str(payload.get("content", "")).strip()
                if role in {"user", "assistant"} and content:
                    turns.append(ConversationTurn(role, content))
            elif event_type == "user.prompt":
                prompt = str(payload.get("prompt", "")).strip()
                if prompt:
                    turns.append(ConversationTurn("user", prompt))
            elif event_type == "assistant.message":
                message = str(payload.get("message", "")).strip()
                if message:
                    turns.append(ConversationTurn("assistant", message))
        summary = logger.read_summary().strip()
        state = logger.read_state()
        task_state = _render_task_state(state)
        return cls(turns, summary[:2400], task_state)

    def build_context(self, max_turns: int = 8) -> str:
        recent = self.turns[-max_turns:]
        sections: list[str] = []
        if self.resume_summary:
            sections.append(f"Compact session resume summary:\n{self.resume_summary}")
        if self.task_state:
            sections.append(f"Current task state:\n{self.task_state}")
        if recent:
            sections.append(
                "Recent conversation:\n"
                + "\n".join(f"{turn.role}: {turn.text}" for turn in recent)
            )
        if not sections:
            return "No previous conversation turns in this session."
        return "\n\n".join(sections)

    def previous_user_prompt(self) -> str:
        for turn in reversed(self.turns[:-1] if self.turns and self.turns[-1].role == "user" else self.turns):
            if turn.role == "user":
                return turn.text
        return ""

    def resolve_followup(self, user_input: str) -> str:
        text = user_input.strip()
        lowered = text.lower()
        previous = self.previous_user_prompt()
        if not previous:
            return user_input
        if lowered in FOLLOWUP_WEB:
            return f"{previous} Please check on the web for this."
        if lowered in FOLLOWUP_BROWSER:
            return f"{previous} Please inspect it in the browser."
        if lowered in FOLLOWUP_GENERIC:
            return f"{previous}\nFollow-up request: {text}"
        if lowered.startswith("what about ") and len(lowered.split()) <= 5:
            return f"{previous}\nFollow-up request: {text}"
        return user_input


def _render_task_state(state: dict) -> str:
    lines: list[str] = []
    pending = state.get("pending_action")
    if isinstance(pending, dict) and pending:
        lines.append(
            f"- Pending action: {pending.get('type', 'action')} "
            f"(awaiting: {pending.get('awaiting', 'user_input')})"
        )
    route = state.get("last_route")
    if isinstance(route, dict) and route.get("route"):
        lines.append(f"- Last route: {route['route']}")
    failure = state.get("last_failure")
    if isinstance(failure, dict) and failure:
        command = str(failure.get("command") or "").strip()
        exit_code = failure.get("exit_code", "")
        lines.append(f"- Last failing command: {command or '(unknown)'} (exit {exit_code})")
    return "\n".join(lines)
