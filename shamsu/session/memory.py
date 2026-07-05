"""Compact conversation memory built from workspace-local session logs."""
from __future__ import annotations

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


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    text: str


@dataclass(frozen=True)
class ConversationMemory:
    turns: list[ConversationTurn]

    @classmethod
    def from_session(cls, logger: SessionLogger | None, max_events: int = 80) -> "ConversationMemory":
        if logger is None:
            return cls([])
        turns: list[ConversationTurn] = []
        for event in logger.tail(max_events):
            event_type = event.get("event_type", "")
            payload = event.get("payload", {})
            if event_type == "user.prompt":
                prompt = str(payload.get("prompt", "")).strip()
                if prompt:
                    turns.append(ConversationTurn("user", prompt))
            elif event_type == "assistant.message":
                message = str(payload.get("message", "")).strip()
                if message:
                    turns.append(ConversationTurn("assistant", message))
        return cls(turns)

    def build_context(self, max_turns: int = 8) -> str:
        recent = self.turns[-max_turns:]
        if not recent:
            return "No previous conversation turns in this session."
        return "\n".join(f"{turn.role}: {turn.text}" for turn in recent)

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
