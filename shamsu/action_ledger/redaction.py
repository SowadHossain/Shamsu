"""Redaction utilities for ActionLedger.

Reuses shamsu.safety.commands.redact (the project's single source of truth
for secret-pattern matching) rather than inventing a second regex list, and
shamsu.patch.safety.is_secret_file to recognize filenames whose *contents*
must never be logged (.env, id_rsa, credentials.json, ...).
"""
from __future__ import annotations

from typing import Any

from shamsu.patch.safety import is_secret_file
from shamsu.safety.commands import redact


def redact_text(value: str) -> str:
    return redact(value)


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(key): redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value


def is_secret_path(path: str) -> bool:
    try:
        return is_secret_file(str(path))
    except Exception:
        return False
