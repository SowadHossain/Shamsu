"""Install-wide preferences, shared by every surface.

Small on purpose. This is not a config system - it holds the handful of
choices that should follow you between the terminal, the browser and the
phone, because setting a context window in one place and finding the other two
unchanged is the same complaint that made the bot token install-scoped.

Precedence is deliberate and matches the token's: **an environment variable
always wins.** An operator who exported `SHAMSU_CHAT_MAX_CTX` for a reason must
not have it silently overridden by something clicked in a browser last week.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from shamsu.runtime.home import shamsu_home

SETTINGS_FILE = "settings.json"

#: Only these keys are honoured. An unknown key in the file is ignored rather
#: than stored, so a typo cannot quietly become a setting nothing reads.
#:
#: The three ceilings below joined the four originals because they were the
#: numbers people actually needed to change and the only way to change them was
#: an environment variable and a restart - or, for `max_rounds`, editing the
#: source. Deliberately ceilings and not SHARES: `tool_result_budget` and the
#: skeleton ratio stay as fractions of the live window, because a share follows
#: a window that moves and an absolute number does not. Making those settable
#: would re-open the defect fixed on 2026-08-30, where budgets sized against
#: the install-wide maximum let one tool result take 98% of an 8k session.
KNOWN_KEYS = frozenset(
    {
        "chat_max_ctx",
        "model",
        "verbosity",
        "telegram_workspace",
        "max_rounds",
        "turn_budget_s",
        "approval_timeout_s",
    }
)

#: Ceilings that are plain positive numbers: the key, its floor, and what it
#: falls back to. Table-driven so `/set`, the completer and the readers below
#: cannot disagree about what is legal.
NUMERIC_LIMITS: dict[str, tuple[float, float, str]] = {
    # key: (minimum, default, one-line description for `/set`)
    "max_rounds": (1, 24, "tool-call steps one turn may take"),
    "turn_budget_s": (0, 1200, "seconds one turn may run (0 = no limit)"),
    "approval_timeout_s": (10, 900, "seconds an approval card waits for an answer"),
}

#: The three levels `body_kinds()` already implements. Anything else in the
#: file is treated as unset rather than passed through, because a typo here
#: would silently pick the quietest rendering.
VERBOSITY_LEVELS = ("quiet", "normal", "verbose")
DEFAULT_VERBOSITY = "normal"


def settings_path() -> Path:
    return shamsu_home() / SETTINGS_FILE


def load_settings() -> dict[str, Any]:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A hand-edited file costs the settings, never the process.
        return {}
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if key in KNOWN_KEYS}


def update_settings(**changes: Any) -> dict[str, Any]:
    """Merge and persist. Returns the settings as they now stand."""
    unknown = set(changes) - KNOWN_KEYS
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
    current = load_settings()
    for key, value in changes.items():
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
    path = settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return current


def chat_max_ctx() -> int | None:
    """The saved context-window ceiling, if one was chosen. None means default.

    Floored at 4096 because anything smaller cannot hold the system prompt and
    a tool schema, so accepting it would produce a model that fails on its
    first call rather than a smaller window.
    """
    value = load_settings().get("chat_max_ctx")
    try:
        window = int(value)
    except (TypeError, ValueError):
        return None
    return window if window >= 4096 else None


def chat_model() -> str:
    """The install-wide model choice, or ``""`` for tier-based selection.

    Read live rather than cached at import, so a model picked in the browser
    reaches a REPL that was already running. See ``model_for_role()`` for where
    this sits in the precedence chain - it is deliberately WEAKER than a
    workspace pin.
    """
    value = load_settings().get("model")
    return str(value).strip() if isinstance(value, str) else ""


def verbosity() -> str:
    """How much of a turn every renderer shows: quiet, normal or verbose.

    One value for all three surfaces on purpose. Verbosity describes how much
    you want to watch, not which screen you happen to be at, and having the
    terminal and the phone disagree about it was never a feature.
    """
    value = load_settings().get("verbosity")
    level = str(value).strip().lower() if isinstance(value, str) else ""
    return level if level in VERBOSITY_LEVELS else DEFAULT_VERBOSITY


def telegram_workspace() -> Path | None:
    """Which project the Telegram bot drives, if one was chosen.

    None means "decide at start-up" - the portal's own workspace, else the most
    recently active one. A path that no longer exists is treated as unset,
    because refusing to start the bot over a folder someone deleted last month
    would be a worse failure than falling back.
    """
    value = load_settings().get("telegram_workspace")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        candidate = Path(value).expanduser()
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_dir() else None


def numeric_limit(key: str) -> float:
    """One of `NUMERIC_LIMITS`, from env > settings.json > default.

    The same precedence `budget.chat_ctx_ceiling` uses, for the same reason: an
    operator who exported a variable did so deliberately and must not have it
    overridden by something clicked in a browser last week.

    The environment variable is derived from the key rather than listed, so
    adding a limit to the table is the whole change - a second list of names to
    keep in step is a second list to forget.
    """
    floor, default, _ = NUMERIC_LIMITS[key]
    raw = os.environ.get(f"SHAMSU_{key.upper()}", "").strip()
    for candidate in (raw, load_settings().get(key)):
        try:
            value = float(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        # A value BELOW the floor is a mistake, not a smaller choice, and
        # honouring it would produce a harness that fails rather than one that
        # works in less room. `turn_budget_s` floors at 0 because 0 is its
        # documented "no limit" escape hatch.
        if value >= floor:
            return value
    return float(default)


def max_rounds() -> int:
    """How many tool-call steps one turn may take."""
    return int(numeric_limit("max_rounds"))


def turn_budget_s() -> float:
    """Seconds one turn may run. 0 means no limit."""
    return numeric_limit("turn_budget_s")


def approval_timeout_s() -> float:
    """Seconds an approval card waits before it is treated as unanswered."""
    return numeric_limit("approval_timeout_s")
