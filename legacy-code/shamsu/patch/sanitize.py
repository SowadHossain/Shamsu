"""Normalization boundary for model-produced unified diffs."""
from __future__ import annotations

import re

from shamsu.llm.output import strip_reasoning

_FENCED_BLOCK_RE = re.compile(r"```(?:diff|patch)?[ \t]*\n(?P<body>.*?)```", re.I | re.S)
_SYNTHETIC_FILE_CONTEXT_RE = re.compile(
    r"^ # File:\s+.+?\s+\(lines\s+\d+(?:-\d+)?\)\s*$",
    re.IGNORECASE,
)


def sanitize_model_diff(raw: str) -> str:
    """Extract and clean one unified diff without changing its real content.

    ContextBuilder labels such as ``# File: app.py (lines 1-5)`` describe a
    retrieved snippet; they are not source lines. Small models sometimes copy
    that label into a hunk as context, making an otherwise correct patch fail.
    Only the exact synthetic context-line shape is removed.
    """
    text = strip_reasoning(raw or "").strip()
    fenced = _FENCED_BLOCK_RE.search(text)
    if fenced:
        text = fenced.group("body").strip()
    lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
    starts = ("diff --git ", "--- ", "*** ", "Index: ")
    start = next((index for index, line in enumerate(lines) if line.startswith(starts)), None)
    if start is not None:
        lines = lines[start:]
    lines = [line for line in lines if not _SYNTHETIC_FILE_CONTEXT_RE.match(line)]
    cleaned = "\n".join(lines).strip()
    return cleaned + "\n" if cleaned else ""
