"""P2: the bot token and the pairing bind to the INSTALL, not the workspace.

The defect, in one sentence: switching project meant reconfiguring the token
AND re-pairing the phone, because both lived under `<workspace>/.shamsu/`.
That is the opposite of "bound to the install until I change it" (G3).

These tests are written against the observable promise - "configure once, then
switch project and it still works" - rather than against the file layout, so a
later move of the files does not fail them for the wrong reason.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from shamsu.integrations.telegram import install
from shamsu.integrations.telegram.models import PermissionLevel
from shamsu.integrations.telegram.service import (
    TOKEN_ENV_VAR,
    configure_telegram_bot_token,
    load_telegram_bot_token,
    promote_workspace_token,
)
from shamsu.integrations.telegram.storage import TelegramStateStore

TOKEN = "123456:AAH-fake-token-for-tests"
OTHER_TOKEN = "999999:BBB-a-different-bot-token"


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A scratch install root, so no test can read or write the real one."""
    root = tmp_path / "install-home"
    monkeypatch.setenv(install.HOME_ENV_VAR, str(root))
    return root


# --- the token ------------------------------------------------------------


def test_configure_writes_the_token_once_for_every_project(tmp_path, home, monkeypatch):
    """G3, executable: configure in project A, read it from project B."""
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()

    path = configure_telegram_bot_token(project_a, TOKEN)

    assert path == install.install_token_path()
    assert home in path.parents
    token, source = load_telegram_bot_token(project_b)
    assert token == TOKEN
    assert source == "install"


def test_the_environment_still_wins_over_the_install(tmp_path, home, monkeypatch):
    """The CI/ops override is unchanged - it was always first and stays first."""
    configure_telegram_bot_token(tmp_path, TOKEN)
    monkeypatch.setenv(TOKEN_ENV_VAR, OTHER_TOKEN)

    token, source = load_telegram_bot_token(tmp_path)
    assert token == OTHER_TOKEN
    assert source == "environment"


def test_a_workspace_token_is_still_read_when_no_install_token_exists(
    tmp_path, home, monkeypatch
):
    """Nobody's existing setup breaks on upgrade: the old file still works."""
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    workspace_file = tmp_path / ".shamsu" / "telegram.env"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text(f"{TOKEN_ENV_VAR}={TOKEN}\n", encoding="utf-8")

    token, source = load_telegram_bot_token(tmp_path)
    assert token == TOKEN
    assert source == ".shamsu/telegram.env"


def test_dot_env_remains_the_last_resort(tmp_path, home, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    (tmp_path / ".env").write_text(f"{TOKEN_ENV_VAR}={TOKEN}\n", encoding="utf-8")

    token, source = load_telegram_bot_token(tmp_path)
    assert token == TOKEN
    assert source == ".env"


def test_configure_can_still_be_pinned_to_one_project(tmp_path, home, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    path = configure_telegram_bot_token(tmp_path, TOKEN, install_scope=False)

    assert path == tmp_path / ".shamsu" / "telegram.env"
    assert not install.install_token_path().exists()


def test_an_invalid_token_is_refused_before_anything_is_written(tmp_path, home):
    with pytest.raises(ValueError):
        configure_telegram_bot_token(tmp_path, "not-a-token")
    assert not install.install_token_path().exists()


def test_the_token_file_is_not_world_readable(tmp_path, home):
    """Best effort by platform, but never silently permissive on POSIX."""
    path = configure_telegram_bot_token(tmp_path, TOKEN)
    mode = path.stat().st_mode & 0o777
    if hasattr(__import__("os"), "getuid"):  # POSIX only; Windows uses ACLs
        assert mode == 0o600
    assert path.read_text(encoding="utf-8").strip() == f"{TOKEN_ENV_VAR}={TOKEN}"


# --- the migration --------------------------------------------------------


def test_an_existing_workspace_token_is_promoted_once(tmp_path, home, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    workspace_file = tmp_path / ".shamsu" / "telegram.env"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text(f"{TOKEN_ENV_VAR}={TOKEN}\n", encoding="utf-8")

    promoted = promote_workspace_token(tmp_path)

    assert promoted == install.install_token_path()
    assert load_telegram_bot_token(tmp_path)[0] == TOKEN
    # Never delete the old file - a downgrade must not lose the token.
    assert workspace_file.exists()
    # And it only happens once.
    assert promote_workspace_token(tmp_path) is None


def test_promotion_never_overwrites_an_install_token(tmp_path, home, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    configure_telegram_bot_token(tmp_path, TOKEN)
    workspace_file = tmp_path / ".shamsu" / "telegram.env"
    workspace_file.parent.mkdir(parents=True, exist_ok=True)
    workspace_file.write_text(f"{TOKEN_ENV_VAR}={OTHER_TOKEN}\n", encoding="utf-8")

    assert promote_workspace_token(tmp_path) is None
    assert load_telegram_bot_token(tmp_path)[0] == TOKEN


def test_promotion_with_nothing_to_promote_is_a_no_op(tmp_path, home, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    assert promote_workspace_token(tmp_path) is None


# --- the state DB ---------------------------------------------------------


def _authorize(store: TelegramStateStore, user_id: int = 7) -> None:
    store.authorize_user(
        telegram_user_id=user_id,
        telegram_chat_id=99,
        installation_id="install-test",
        display_name="Sam",
        permission_level=PermissionLevel.OWNER,
    )


def test_a_pairing_survives_a_project_switch(tmp_path, home):
    """The other half of G3: re-pairing per project defeated the point."""
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()

    _authorize(TelegramStateStore(project_a))

    from_b = TelegramStateStore(project_b)
    assert [int(user["telegram_user_id"]) for user in from_b.authorized_users()] == [7]


def test_the_update_offset_is_install_global(tmp_path, home):
    """There is one bot, so there is one offset. Two would replay updates."""
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()

    TelegramStateStore(project_a).set_meta("telegram_update_offset", "4242")
    assert TelegramStateStore(project_b).get_meta("telegram_update_offset") == "4242"


def test_the_active_session_stays_per_workspace(tmp_path, home):
    """Pairing is install-wide; which THREAD you are in is not.

    A session id from project A means nothing in project B, and handing one
    over would attach the phone to a thread that does not exist there.
    """
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()

    store_a = TelegramStateStore(project_a)
    store_b = TelegramStateStore(project_b)
    store_a.set_active_session(7, "session-in-a")
    store_b.set_active_session(7, "session-in-b")

    assert store_a.active_session_for(7) == "session-in-a"
    assert store_b.active_session_for(7) == "session-in-b"


def test_an_audit_row_records_which_workspace_it_happened_in(tmp_path, home):
    project_a = tmp_path / "a"
    project_a.mkdir()
    store = TelegramStateStore(project_a)
    store.audit(action="test", result="ok", payload={})

    rows = store.list_audit()
    assert rows and rows[0]["workspace"] == str(project_a.resolve())


def test_an_explicit_db_path_still_wins(tmp_path, home):
    """The seam every existing test uses must keep working."""
    db_path = tmp_path / "explicit.db"
    store = TelegramStateStore(tmp_path, db_path=db_path)
    _authorize(store)

    assert db_path.exists()
    assert not install.install_state_db_path().exists()


def test_a_legacy_workspace_db_is_imported_once_and_left_alone(tmp_path, home):
    """Upgrading must not silently un-pair the phone."""
    workspace = tmp_path / "legacy"
    workspace.mkdir()
    legacy_path = workspace / ".shamsu" / "telegram" / "telegram-state.db"
    legacy = TelegramStateStore(workspace, db_path=legacy_path)
    _authorize(legacy, user_id=11)
    legacy.set_meta("telegram_update_offset", "77")
    legacy.set_active_session(11, "legacy-session")

    store = TelegramStateStore(workspace)

    assert [int(u["telegram_user_id"]) for u in store.authorized_users()] == [11]
    assert store.get_meta("telegram_update_offset") == "77"
    assert store.active_session_for(11) == "legacy-session"
    # The old file is never deleted, and never imported twice.
    assert legacy_path.exists()
    legacy.disconnect_user(11)
    again = TelegramStateStore(workspace)
    assert [int(u["telegram_user_id"]) for u in again.authorized_users()] == [11]


def test_an_older_install_db_is_migrated_in_place(tmp_path, home):
    """A v1 install DB gains the new columns rather than being rebuilt."""
    db_path = install.install_state_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            """
            CREATE TABLE active_sessions (
                telegram_user_id INTEGER PRIMARY KEY,
                session_id TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO active_sessions VALUES (7, 'old-session', '2026-01-01T00:00:00Z')"
        )
        conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")

    store = TelegramStateStore(tmp_path)

    assert store.get_meta("schema_version") == "2"
    # The row survives; it simply has no workspace yet, so it answers for the
    # workspace that asks first rather than being thrown away.
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute("SELECT * FROM active_sessions")]
    assert rows and rows[0]["session_id"] == "old-session"
    assert "workspace" in rows[0]


# --- the command surface --------------------------------------------------


def test_the_configure_command_defaults_to_the_install(tmp_path, home, monkeypatch):
    import shamsu.integrations.telegram.local as local

    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.setattr(local._MANAGER, "stop", lambda: None)
    monkeypatch.setattr(local._MANAGER, "start", lambda *a, **k: None)
    monkeypatch.setattr(
        local._MANAGER,
        "reload_service",
        lambda *a, **k: type("S", (), {"local_panel": lambda self, sub: type("P", (), {"message": "ok"})()})(),
    )
    console = _RecordingConsole()

    local.handle_remote_control_command(
        f"/remote_control configure {TOKEN}", tmp_path, console
    )

    assert install.install_token_path().exists()
    assert not (tmp_path / ".shamsu" / "telegram.env").exists()
    assert TOKEN not in console.text


def test_the_workspace_flag_pins_the_token_to_this_project(tmp_path, home, monkeypatch):
    import shamsu.integrations.telegram.local as local

    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    monkeypatch.setattr(local._MANAGER, "stop", lambda: None)
    monkeypatch.setattr(local._MANAGER, "start", lambda *a, **k: None)
    monkeypatch.setattr(
        local._MANAGER,
        "reload_service",
        lambda *a, **k: type("S", (), {"local_panel": lambda self, sub: type("P", (), {"message": "ok"})()})(),
    )
    console = _RecordingConsole()

    local.handle_remote_control_command(
        f"/remote_control configure {TOKEN} --workspace", tmp_path, console
    )

    assert (tmp_path / ".shamsu" / "telegram.env").exists()
    assert not install.install_token_path().exists()
    assert TOKEN not in console.text


def test_the_promotion_notice_is_printed_once(tmp_path, home, monkeypatch):
    import shamsu.integrations.telegram.local as local

    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    workspace_file = tmp_path / ".shamsu" / "telegram.env"
    workspace_file.parent.mkdir(parents=True)
    workspace_file.write_text(f"{TOKEN_ENV_VAR}={TOKEN}\n", encoding="utf-8")

    console = _RecordingConsole()
    local._announce_token_promotion(tmp_path, console)
    assert "promoted to this installation" in console.text
    assert TOKEN not in console.text

    second = _RecordingConsole()
    local._announce_token_promotion(tmp_path, second)
    assert second.text == ""


class _RecordingConsole:
    """Just enough Console to see what the user was told."""

    def __init__(self) -> None:
        self.printed: list[str] = []

    def print(self, *args, **kwargs) -> None:
        for arg in args:
            self.printed.append(str(getattr(arg, "renderable", arg)))

    @property
    def text(self) -> str:
        return "\n".join(self.printed)
