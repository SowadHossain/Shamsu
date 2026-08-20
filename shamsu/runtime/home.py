"""Where install-wide state lives: `$SHAMSU_HOME`, else `~/.shamsu`.

Split out of `integrations/telegram/install.py`, which is where it first
appeared and is the wrong owner for it. The bot token was simply the first
thing that needed to outlive a single workspace; the control plane, the
workspace registry and anything else install-scoped need the same answer, and
none of them should import a Telegram module to get it.

The env override exists so a test can never read - or overwrite - the real
`~/.shamsu` of whoever is running the suite. That is not hypothetical: the bot
token and the phone's pairing live there.
"""
from __future__ import annotations

import os
from pathlib import Path

HOME_ENV_VAR = "SHAMSU_HOME"


def shamsu_home() -> Path:
    override = os.environ.get(HOME_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".shamsu"
