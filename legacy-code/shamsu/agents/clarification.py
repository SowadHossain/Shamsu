"""First-party clarification primitives.

SHAMSU asks the user a question only when required input is missing and it
cannot be safely inferred with read-only tools. This module holds the pure,
side-effect-free pieces of that behavior:

- `ClarificationNeed`  - "do I need to ask, and if so what?"
- a pending-question dict shape used in session state
- `resolve_answer`     - interpret a user's reply (number, label, yes/no,
                         continue/cancel, or free text) against a pending
                         question

The REPL and the chat loop consume these; keeping them here makes the policy
testable without a live model or console.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

AFFIRMATIVE = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "do it", "go", "go ahead", "proceed", "confirm"}
NEGATIVE = {"no", "n", "nope", "nah", "don't", "dont", "stop"}
CANCEL = {"cancel", "abort", "never mind", "nevermind", "forget it", "skip"}
CONTINUE = {"continue", "keep going", "carry on", "next", "resume"}


@dataclass
class ClarificationNeed:
    needed: bool
    question: str = ""
    options: list[dict[str, str]] = field(default_factory=list)
    reason: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def to_pending(self, created_from_prompt: str = "", allow_free_text: bool = True) -> dict[str, Any]:
        return build_pending_question(
            self.question,
            self.options,
            context={**self.context, "reason": self.reason} if self.reason else self.context,
            created_from_prompt=created_from_prompt,
            allow_free_text=allow_free_text,
        )


@dataclass
class ClarificationAnswer:
    resolved: bool
    kind: str  # option | affirmative | negative | cancel | continue | free_text | unresolved
    value: str = ""
    option: dict[str, str] | None = None


def build_pending_question(
    question: str,
    options: list[dict[str, str]] | None = None,
    *,
    context: dict[str, Any] | None = None,
    created_from_prompt: str = "",
    allow_free_text: bool = True,
) -> dict[str, Any]:
    """Normalize an ask-the-user request into the session-state pending shape."""
    return {
        "question": question,
        "options": [_normalize_option(item) for item in (options or [])],
        "allow_free_text": bool(allow_free_text),
        "context": context or {},
        "created_from_prompt": created_from_prompt,
        "awaiting": "user_input",
    }


def need_for_file_candidates(path: str, candidates: list[str], reason: str = "") -> ClarificationNeed:
    """Multiple files match a missing path - ask which one to use."""
    return ClarificationNeed(
        needed=True,
        question=f"I found multiple candidates for {path}. Which one should I use?",
        options=[{"label": candidate, "description": ""} for candidate in candidates],
        reason=reason or "ambiguous_file_path",
        context={"requested_path": path, "candidates": candidates},
    )


def format_question(pending: dict[str, Any]) -> str:
    """Render a pending question as user-facing text with numbered options."""
    lines = [str(pending.get("question", "")).strip() or "I need more information to continue."]
    options = pending.get("options") or []
    for index, option in enumerate(options, start=1):
        label = str(option.get("label", "")).strip()
        description = str(option.get("description", "")).strip()
        suffix = f" - {description}" if description else ""
        lines.append(f"{index}. {label}{suffix}")
    if options and pending.get("allow_free_text", True):
        lines.append("(Reply with a number, or type your own answer.)")
    return "\n".join(lines)


def classify_reply(reply: str) -> str:
    """Coarse intent of a bare reply, independent of any pending options."""
    normalized = _normalize(reply)
    if normalized in CANCEL:
        return "cancel"
    if normalized in CONTINUE:
        return "continue"
    if normalized in AFFIRMATIVE:
        return "affirmative"
    if normalized in NEGATIVE:
        return "negative"
    return "other"


def resolve_answer(pending: dict[str, Any], reply: str) -> ClarificationAnswer:
    """Interpret `reply` as an answer to `pending`.

    Resolution order: cancel -> numbered option -> exact/prefix label match ->
    yes/no/continue -> free text (when allowed). An empty or unmatched reply
    with no free-text fallback is left unresolved so the caller can re-ask.
    """
    options = pending.get("options") or []
    allow_free_text = bool(pending.get("allow_free_text", True))
    raw = reply.strip()
    normalized = _normalize(raw)

    if normalized in CANCEL:
        return ClarificationAnswer(resolved=True, kind="cancel", value=raw)

    # Numbered choice: "1", "2)", "option 2", "#3" ...
    index = _extract_choice_index(raw, len(options))
    if index is not None:
        option = options[index]
        return ClarificationAnswer(resolved=True, kind="option", value=option.get("label", ""), option=option)

    # Label match (exact, then unique prefix/substring).
    matched = _match_option_by_label(options, normalized)
    if matched is not None:
        return ClarificationAnswer(resolved=True, kind="option", value=matched.get("label", ""), option=matched)

    if normalized in CONTINUE:
        return ClarificationAnswer(resolved=True, kind="continue", value=raw)
    if normalized in AFFIRMATIVE:
        # A bare "yes" against a single-option question means "use that one".
        if len(options) == 1:
            option = options[0]
            return ClarificationAnswer(resolved=True, kind="option", value=option.get("label", ""), option=option)
        return ClarificationAnswer(resolved=True, kind="affirmative", value=raw)
    if normalized in NEGATIVE:
        return ClarificationAnswer(resolved=True, kind="negative", value=raw)

    if raw and allow_free_text:
        return ClarificationAnswer(resolved=True, kind="free_text", value=raw)
    return ClarificationAnswer(resolved=False, kind="unresolved", value=raw)


def _normalize_option(option: Any) -> dict[str, str]:
    if isinstance(option, dict):
        return {"label": str(option.get("label", "")).strip(), "description": str(option.get("description", "")).strip()}
    return {"label": str(option).strip(), "description": ""}


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split()).strip(".!?")


def _extract_choice_index(raw: str, count: int) -> int | None:
    if count <= 0:
        return None
    token = raw.strip().lower().lstrip("#").rstrip(".)").strip()
    token = token.removeprefix("option").removeprefix("choice").strip()
    if token.isdigit():
        number = int(token)
        if 1 <= number <= count:
            return number - 1
    return None


def _match_option_by_label(options: list[dict[str, str]], normalized_reply: str) -> dict[str, str] | None:
    if not normalized_reply:
        return None
    labels = [(_normalize(option.get("label", "")), option) for option in options]
    for label, option in labels:
        if label and label == normalized_reply:
            return option
    # Unique substring/prefix match, e.g. "client" -> "client/src/App.tsx".
    partial = [option for label, option in labels if label and normalized_reply in label]
    if len(partial) == 1:
        return partial[0]
    return None
