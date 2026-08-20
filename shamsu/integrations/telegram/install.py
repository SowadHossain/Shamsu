"""Install-scoped paths for Telegram remote control.

"Install scope" is the distinction this module exists to name. Almost
everything SHAMSU writes belongs to a *workspace* - the index, the sessions,
the ledger - and that is right, because it describes that project. The bot
token and the pairing do not: they describe **this machine's bot and the phone
allowed to talk to it**, which is the same fact whichever project you happen to
have open.

Keeping them workspace-scoped meant switching project silently un-paired the
phone and asked for the token again, which is exactly what G3 says must stop.

`~/.shamsu/` is the established home for install-wide state here already -
`tools/` for the MCP binaries, `skills/`, `runtime/`, `mcp.json`. This is one
more tenant, not a new convention.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

# Re-exported: `shamsu_home` outgrew this module the moment anything other than
# the bot token needed install-wide state. It lives in `runtime/home.py` now;
# these names stay so existing callers and tests keep working.
from shamsu.runtime.home import HOME_ENV_VAR, shamsu_home

TOKEN_FILE = "telegram.env"

__all__ = [
    "HOME_ENV_VAR",
    "shamsu_home",
    "TOKEN_FILE",
    "install_token_path",
    "install_state_db_path",
    "workspace_token_path",
    "legacy_state_db_path",
    "write_private_file",
]


def install_token_path() -> Path:
    return shamsu_home() / TOKEN_FILE


def install_state_db_path() -> Path:
    return shamsu_home() / "telegram" / "telegram-state.db"


def workspace_token_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".shamsu" / TOKEN_FILE


def legacy_state_db_path(workspace: Path) -> Path:
    """Where the state DB lived before it became install-scoped."""
    return Path(workspace).resolve() / ".shamsu" / "telegram" / "telegram-state.db"


def write_private_file(path: Path, text: str) -> Path:
    """Write a secret, readable only by this user, as far as the OS allows.

    POSIX gets a real `0600`. Windows has no mode bits that mean anything here,
    so this falls back to the default ACL of a file created inside the user
    profile - which is already user-scoped - rather than pretending otherwise.
    The permissions are set BEFORE the content is written, so there is no
    window in which the token exists in a world-readable file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Best effort: a filesystem that cannot express this must not stop the
        # user configuring their bot.
        pass
    path.write_text(text, encoding="utf-8")
    return path
