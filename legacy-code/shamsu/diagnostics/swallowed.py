"""Ledger for deliberately-swallowed exceptions (gap G3).

SHAMSU wraps its bookkeeping - audit logging, session-state writes, route
recording, memory saves - in `except Exception: pass` so a broken side channel
never breaks a user-facing answer. Right instinct, but applied silently ~40
times over in repl.py alone: when the audit log breaks on day 1, nobody finds
out until the day they need the trail.

This keeps the silence for users while making it diagnosable: best-effort
handlers call `record(where, exc)` instead of bare `pass`, and `/context show`
prints the tally. Process-global on purpose - the failures worth noticing are
the ones that repeat, and per-object counters would reset with every handler.

`record` itself must never raise; it is called from inside except blocks.
"""
from __future__ import annotations

import threading
from collections import Counter

_LOCK = threading.Lock()
_COUNTS: Counter[str] = Counter()
_LAST_ERROR: dict[str, str] = {}


def record(where: str, exc: BaseException) -> None:
    """Note that `where` swallowed `exc`. Safe to call from any except block."""
    try:
        with _LOCK:
            _COUNTS[where] += 1
            _LAST_ERROR[where] = f"{type(exc).__name__}: {exc}"[:200]
    except Exception:
        pass


def snapshot() -> list[tuple[str, int, str]]:
    """(where, count, last_error) rows, most-swallowed first."""
    with _LOCK:
        return sorted(
            ((where, count, _LAST_ERROR.get(where, "")) for where, count in _COUNTS.items()),
            key=lambda row: row[1],
            reverse=True,
        )


def total() -> int:
    with _LOCK:
        return sum(_COUNTS.values())


def reset() -> None:
    """Test hook."""
    with _LOCK:
        _COUNTS.clear()
        _LAST_ERROR.clear()
