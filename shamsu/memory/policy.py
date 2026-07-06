"""Policy for what SHAMSU may store in Graphiti long-term memory."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from shamsu.memory.types import MemoryKind
from shamsu.safety.commands import redact as base_redact

SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|password|passwd|secret|private[_-]?key)\s*[:=]\s*\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)^\s*[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD)\s*=", re.MULTILINE),
)

EXPLICIT_MARKERS = ("remember", "always", "from now on", "going forward")
TRANSIENT_MARKERS = (
    "temporary error",
    "one-off error",
    "random error",
    "just failed once",
    "flake",
    "transient",
)
SOURCE_MARKERS = ("```", "def ", "class ", "import ", "from ", "function ", "const ", "let ")
MAX_MEMORY_CHARS = 1200


@dataclass(frozen=True)
class PolicyDecision:
    should_store: bool
    kind: MemoryKind = "task_summary"
    text: str = ""
    reason: str = ""


class MemoryPolicy:
    """Conservative durable-memory policy; never stores raw logs or code blobs."""

    def decide(self, text: str, kind: MemoryKind | None = None, metadata: dict[str, Any] | None = None) -> PolicyDecision:
        cleaned = self.redact(text).strip()
        lowered = cleaned.lower()
        if not cleaned:
            return PolicyDecision(False, kind or "task_summary", "", "empty")
        if self.contains_secret(text):
            return PolicyDecision(False, kind or "safety_rule", "", "secret-like content")
        if "chain-of-thought" in lowered or "raw reasoning" in lowered:
            return PolicyDecision(False, kind or "safety_rule", "", "raw reasoning")
        if self.looks_like_full_source(cleaned):
            return PolicyDecision(False, kind or "architecture_note", "", "source blob")
        if any(marker in lowered for marker in TRANSIENT_MARKERS):
            return PolicyDecision(False, kind or "bug_lesson", "", "transient")
        inferred = kind or self.infer_kind(cleaned, metadata)
        durable = kind is not None or any(marker in lowered for marker in EXPLICIT_MARKERS) or self._looks_durable(lowered, inferred)
        if not durable:
            return PolicyDecision(False, inferred, "", "not durable")
        return PolicyDecision(True, inferred, cleaned[:MAX_MEMORY_CHARS], "durable")

    def infer_kind(self, text: str, metadata: dict[str, Any] | None = None) -> MemoryKind:
        lowered = text.lower()
        if any(word in lowered for word in ("prefer", "preference", "i want", "user wants")):
            return "user_preference"
        if any(word in lowered for word in ("decision", "decided", "architecture", "design choice")):
            return "project_decision"
        if any(word in lowered for word in ("always", "never", "from now on", "going forward", "rule")):
            return "workflow_rule"
        if any(word in lowered for word in ("bug", "fix", "lesson", "regression")):
            return "bug_lesson"
        if any(word in lowered for word in ("architecture", "module", "backend", "frontend", "adapter")):
            return "architecture_note"
        if any(word in lowered for word in ("secret", "token", "private", "safety")):
            return "safety_rule"
        return "task_summary"

    def redact(self, text: str) -> str:
        redacted = base_redact(text)
        for pattern in SECRET_PATTERNS:
            redacted = pattern.sub("[REDACTED]", redacted)
        return redacted

    def contains_secret(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in SECRET_PATTERNS)

    def looks_like_full_source(self, text: str) -> bool:
        lines = text.splitlines()
        if len(lines) > 40:
            source_hits = sum(1 for line in lines if any(marker in line for marker in SOURCE_MARKERS))
            return source_hits >= 6
        return len(text) > 4000 and any(marker in text for marker in SOURCE_MARKERS)

    def _looks_durable(self, lowered: str, kind: MemoryKind) -> bool:
        if kind in {"project_decision", "workflow_rule", "architecture_note", "safety_rule"}:
            return any(word in lowered for word in ("decided", "use ", "must", "required", "rule", "never", "always"))
        if kind == "bug_lesson":
            return any(word in lowered for word in ("lesson", "avoid", "next time", "regression", "fix was"))
        return "summary" in lowered and "task" in lowered
