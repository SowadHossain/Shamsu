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
from pathlib import Path
from typing import Any

from shamsu.runtime.home import shamsu_home

SETTINGS_FILE = "settings.json"

#: Only these keys are honoured. An unknown key in the file is ignored rather
#: than stored, so a typo cannot quietly become a setting nothing reads.
KNOWN_KEYS = frozenset({"chat_max_ctx", "model", "verbosity", "telegram_workspace"})

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
