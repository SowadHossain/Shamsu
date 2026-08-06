"""Secret redaction.

Migrated from `legacy-code/shamsu/safety/commands.py:SECRET_PATTERNS` and
`redact`, plus the recursive form from `safety/audit.py:_redact_data`.

This is the one v1 component migrated **verbatim rather than rewritten**. The
patterns are the accumulated result of a year of real leaks, the v1 test
`test_command_output_secrets_are_redacted` passes against them, and a
rewrite would be a fresh set of untested guesses about what a secret looks
like. Improving them is a task with an evaluation behind it, not a side effect
of moving the file.

Where it is needed: tool output goes into prompts, into `tool_events`, and into
the final report. Every one of those is a place an AWS key can end up on disk
or in a model's context, and the gateway's output cap does not read what it is
truncating.

**Redaction is one-way and lossy on purpose.** The whole match is replaced,
including the key name, so `password = hunter2` becomes `[REDACTED]` rather
than `password = [REDACTED]`. That loses the useful signal "a password is
configured here". It is kept anyway: the patterns were written and tested
against whole-match replacement, and narrowing them to capture only the value
would mean rewriting every one of them against no evidence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

PLACEHOLDER = "[REDACTED]"

#: Migrated verbatim from v1. Ordered: the quoted forms consume their key name
#: first, so the unquoted catch-all at the end only ever sees what they left.
_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"AKIA[0-9A-Z]{16}",
        r"sk-[a-zA-Z0-9]{32,}",
        r"ghp_[a-zA-Z0-9]{36}",
        r"-----BEGIN.*PRIVATE KEY[^-]*-----[\s\S]+?-----END.*PRIVATE KEY[^-]*-----",
        r"-----BEGIN.*PRIVATE KEY",
        r"password\s*=\s*['\"][^'\"]+",
        r'"password"\s*:\s*"[^"]+"',
        r"api_key\s*=\s*['\"][^'\"]+",
        r'"api_key"\s*:\s*"[^"]+"',
        r"secret\s*=\s*['\"][^'\"]+",
        r'"secret"\s*:\s*"[^"]+"',
        r"token\s*=\s*['\"][^'\"]+",
        r'"token"\s*:\s*"[^"]+"',
        r"SECRET_KEY\s*=\s*['\"][^'\"]+",
        r'"SECRET_KEY"\s*:\s*"[^"]+"',
        r"[Aa]uthorization\s*:\s*(Bearer|Basic|Token)\s+\S+",
        r'"[Aa]uthorization"\s*:\s*"[^"]+"',
        r"postgresql://[^@]*:[^@]*@",
        r"mysql://[^@]*:[^@]*@",
        r"mongodb(\+srv)?://[^@]*:[^@]*@",
        # Unquoted assignments (`export API_KEY=abc`, `--token=abc`). This is
        # the shape a secret actually takes in a shell command, and the quoted
        # patterns above miss all of them.
        r"(api[_-]?key|secret[_-]?key|access[_-]?token|refresh[_-]?token"
        r"|client[_-]?secret|password|passwd|secret|token)\s*[=:]\s*[^\s'\";,)]{4,}",
    )
)


def redact(text: str) -> str:
    """Replace anything matching a secret pattern.

    Over-redaction is the intended failure mode. A redacted line the agent has
    to ask about costs one turn; a leaked credential in a run log costs a
    rotation.
    """
    for pattern in _PATTERNS:
        text = pattern.sub(PLACEHOLDER, text)
    return text


def contains_secret(text: str) -> bool:
    """Whether `text` would be changed by `redact`.

    For deciding *whether* to persist something, as opposed to persisting a
    redacted version of it.
    """
    return any(pattern.search(text) for pattern in _PATTERNS)


def redact_structure(value: Any) -> Any:
    """Redact recursively through dicts, lists, and tuples.

    Tool arguments are persisted as JSON on `tool_events`, and a secret passed
    as an *argument* never appears in the output the string redactor sees.
    Mapping keys are left alone: a key named `password` is not itself a secret,
    and redacting keys would make the structure unreadable.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {key: redact_structure(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [redact_structure(item) for item in value]
    return value


__all__ = ["PLACEHOLDER", "contains_secret", "redact", "redact_structure"]
