"""SQLite persistence for Telegram UI state.

This store contains only Telegram integration state: pairings, processed update
IDs, callback indirection, active session mappings, notification settings, and
audit records. It deliberately does not duplicate SHAMSU runtime task state.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from shamsu.integrations.telegram.models import (
    CallbackAction,
    PermissionLevel,
    TelegramSettings,
    utc_now,
)
from shamsu.integrations.telegram import install
from shamsu.safety.commands import redact

#: 2 moved the store from the workspace to the install and gave the two
#: genuinely workspace-shaped tables a `workspace` column.
SCHEMA_VERSION = 2


class TelegramStateStore:
    """Install-scoped Telegram state, with a workspace column where it matters.

    The pairing, the authorizations, the callback tokens and the `getUpdates`
    offset describe **the bot**, of which there is one per install - so keeping
    them per workspace meant switching project logged the phone out and replayed
    updates. They are now install-global.

    What genuinely is per workspace stays per workspace: which session the phone
    is attached to, and the audit trail. A session id from another project names
    a thread that does not exist here.
    """

    def __init__(self, workspace: Path, db_path: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        # An explicit path still wins: it is the seam every test drives, and
        # the escape hatch for anyone who wants isolated state.
        self.db_path = Path(db_path) if db_path is not None else install.install_state_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        if db_path is None:
            self._import_legacy_workspace_db()

    @property
    def workspace_key(self) -> str:
        return str(self.workspace)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    pairing_id TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL UNIQUE,
                    installation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS authorized_users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    telegram_chat_id INTEGER NOT NULL,
                    installation_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    permission_level TEXT NOT NULL,
                    paired_at TEXT NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS active_sessions (
                    telegram_user_id INTEGER NOT NULL,
                    workspace TEXT NOT NULL DEFAULT '',
                    session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (telegram_user_id, workspace)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_updates (
                    update_id INTEGER PRIMARY KEY,
                    processed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS callback_actions (
                    action_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    telegram_user_id INTEGER NOT NULL,
                    telegram_chat_id INTEGER NOT NULL,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    approval_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    telegram_user_id INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL,
                    workspace TEXT NOT NULL DEFAULT '',
                    telegram_user_id INTEGER NOT NULL,
                    telegram_chat_id INTEGER NOT NULL,
                    telegram_message_id INTEGER NOT NULL,
                    shamsu_session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    result TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS metrics (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                )
                """
            )
            self._add_missing_columns(conn)
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @staticmethod
    def _add_missing_columns(conn: sqlite3.Connection) -> None:
        """Bring a v1 database up to v2 in place.

        `CREATE TABLE IF NOT EXISTS` is a no-op against a table that already
        exists, so a database written by the previous version keeps its old
        shape forever unless the columns are added explicitly. Adding them -
        rather than recreating the tables - is what keeps an existing pairing
        and an existing audit trail through the upgrade.
        """
        for table, column, definition in (
            ("active_sessions", "workspace", "TEXT NOT NULL DEFAULT ''"),
            ("audits", "workspace", "TEXT NOT NULL DEFAULT ''"),
        ):
            existing = {
                str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")
            }
            if existing and column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _import_legacy_workspace_db(self) -> None:
        """Adopt a pre-upgrade workspace database once, then never again.

        Without this, upgrading logs the phone out: the pairing lived in
        `<workspace>/.shamsu/telegram/telegram-state.db` and nothing would ever
        look there again. The old file is left exactly where it is - a
        downgrade must still find it - and a marker in `meta` makes the import
        idempotent, so a user who deliberately disconnects does not get
        silently re-paired on the next start.
        """
        legacy_path = install.legacy_state_db_path(self.workspace)
        if legacy_path == self.db_path or not legacy_path.exists():
            return
        marker = f"imported_workspace_db:{self.workspace_key}"
        if self.get_meta(marker):
            return
        try:
            self._copy_from_legacy(legacy_path)
        except sqlite3.Error:
            # A corrupt or half-written legacy file must not stop the bot from
            # starting; the user can always re-pair.
            pass
        self.set_meta(marker, utc_now())

    def _copy_from_legacy(self, legacy_path: Path) -> None:
        source = sqlite3.connect(legacy_path, timeout=30)
        source.row_factory = sqlite3.Row
        try:
            tables = {
                str(row["name"])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            with self._connect() as conn:
                if "authorized_users" in tables:
                    for row in source.execute("SELECT * FROM authorized_users"):
                        self._insert_row(conn, "authorized_users", dict(row))
                if "settings" in tables:
                    for row in source.execute("SELECT * FROM settings"):
                        self._insert_row(conn, "settings", dict(row))
                if "active_sessions" in tables:
                    for row in source.execute("SELECT * FROM active_sessions"):
                        record = dict(row)
                        # The legacy file WAS the workspace, so its rows belong
                        # to this one even though they never said so.
                        record["workspace"] = self.workspace_key
                        self._insert_row(conn, "active_sessions", record)
                if "meta" in tables:
                    for row in source.execute("SELECT * FROM meta"):
                        record = dict(row)
                        if str(record.get("key")) == "schema_version":
                            continue
                        conn.execute(
                            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                            (record.get("key"), record.get("value")),
                        )
        finally:
            source.close()

    @staticmethod
    def _insert_row(conn: sqlite3.Connection, table: str, record: dict[str, Any]) -> None:
        """Copy one row, keeping only columns this schema still has.

        `INSERT OR IGNORE`, because the install database is the authority: a
        second workspace importing its own legacy file must not overwrite a
        pairing the first one already established.
        """
        known = {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
        usable = {key: value for key, value in record.items() if key in known}
        if not usable:
            return
        columns = ", ".join(usable)
        placeholders = ", ".join("?" for _ in usable)
        conn.execute(
            f"INSERT OR IGNORE INTO {table} ({columns}) VALUES ({placeholders})",
            tuple(usable.values()),
        )

    def create_pairing_code(
        self,
        *,
        pairing_id: str,
        code_hash: str,
        installation_id: str,
        expires_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pairing_codes (
                    pairing_id, code_hash, installation_id, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (pairing_id, code_hash, installation_id, utc_now(), expires_at),
            )

    def find_pairing_by_hash(self, code_hash: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pairing_codes WHERE code_hash = ?",
                (code_hash,),
            ).fetchone()
        return dict(row) if row is not None else None

    def increment_pairing_attempts(self, pairing_id: str) -> int:
        with self._connect() as conn:
            conn.execute(
                "UPDATE pairing_codes SET attempts = attempts + 1 WHERE pairing_id = ?",
                (pairing_id,),
            )
            row = conn.execute(
                "SELECT attempts FROM pairing_codes WHERE pairing_id = ?",
                (pairing_id,),
            ).fetchone()
        return int(row["attempts"]) if row is not None else 0

    def consume_pairing(self, pairing_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE pairing_codes SET consumed_at = ? WHERE pairing_id = ? AND consumed_at = ''",
                (utc_now(), pairing_id),
            )

    def authorize_user(
        self,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        installation_id: str,
        display_name: str,
        permission_level: PermissionLevel = PermissionLevel.OWNER,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO authorized_users (
                    telegram_user_id, telegram_chat_id, installation_id, display_name,
                    permission_level, paired_at, status
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    telegram_chat_id=excluded.telegram_chat_id,
                    installation_id=excluded.installation_id,
                    display_name=excluded.display_name,
                    permission_level=excluded.permission_level,
                    status='active'
                """,
                (
                    int(telegram_user_id),
                    int(telegram_chat_id),
                    installation_id,
                    display_name,
                    permission_level.value,
                    utc_now(),
                ),
            )

    def authorization_for(self, telegram_user_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM authorized_users WHERE telegram_user_id = ? AND status = 'active'",
                (int(telegram_user_id),),
            ).fetchone()
        return dict(row) if row is not None else None

    def disconnect_user(self, telegram_user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE authorized_users SET status = 'disconnected' WHERE telegram_user_id = ?",
                (int(telegram_user_id),),
            )

    def authorized_users(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM authorized_users WHERE status = 'active' ORDER BY paired_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_update_processed(self, update_id: int) -> bool:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO processed_updates (update_id, processed_at) VALUES (?, ?)",
                    (int(update_id), utc_now()),
                )
            return True
        except sqlite3.IntegrityError:
            self.increment_metric("telegram_duplicate_updates")
            return False

    def set_active_session(self, telegram_user_id: int, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO active_sessions (telegram_user_id, workspace, session_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id, workspace) DO UPDATE SET
                    session_id=excluded.session_id,
                    updated_at=excluded.updated_at
                """,
                (int(telegram_user_id), self.workspace_key, session_id, utc_now()),
            )

    def active_session_for(self, telegram_user_id: int) -> str:
        """Which thread this user is in, HERE.

        The fallback to a row with no workspace is the upgrade path: a v1
        database recorded one active session and never said where, and dropping
        it would put the phone back at "no active session" for no good reason.
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT session_id FROM active_sessions
                WHERE telegram_user_id = ? AND workspace IN (?, '')
                ORDER BY workspace DESC
                LIMIT 1
                """,
                (int(telegram_user_id), self.workspace_key),
            ).fetchone()
        return str(row["session_id"]) if row is not None else ""

    def create_callback_action(
        self,
        *,
        action_id: str,
        action: CallbackAction,
        telegram_user_id: int,
        telegram_chat_id: int,
        session_id: str = "",
        run_id: str = "",
        approval_id: str = "",
        payload: dict[str, Any] | None = None,
        expires_at: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO callback_actions (
                    action_id, action, telegram_user_id, telegram_chat_id, session_id,
                    run_id, approval_id, payload, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    action.value,
                    int(telegram_user_id),
                    int(telegram_chat_id),
                    session_id,
                    run_id,
                    approval_id,
                    json.dumps(payload or {}, ensure_ascii=True, sort_keys=True),
                    utc_now(),
                    expires_at,
                ),
            )

    def load_callback_action(self, action_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM callback_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["payload"] = json.loads(str(data.get("payload") or "{}"))
        except json.JSONDecodeError:
            data["payload"] = {}
        return data

    def consume_callback_action(self, action_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT consumed_at FROM callback_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if row is None or row["consumed_at"]:
                return False
            conn.execute(
                "UPDATE callback_actions SET consumed_at = ? WHERE action_id = ?",
                (utc_now(), action_id),
            )
        return True

    def get_settings(self, telegram_user_id: int) -> TelegramSettings:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM settings WHERE telegram_user_id = ?",
                (int(telegram_user_id),),
            ).fetchone()
        if row is None:
            return TelegramSettings()
        try:
            data = json.loads(str(row["payload"]))
        except json.JSONDecodeError:
            return TelegramSettings()
        return TelegramSettings(**{**asdict(TelegramSettings()), **data})

    def save_settings(self, telegram_user_id: int, settings: TelegramSettings) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (telegram_user_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (
                    int(telegram_user_id),
                    json.dumps(asdict(settings), ensure_ascii=True, sort_keys=True),
                    utc_now(),
                ),
            )

    def audit(
        self,
        *,
        telegram_user_id: int = 0,
        telegram_chat_id: int = 0,
        telegram_message_id: int = 0,
        session_id: str = "",
        run_id: str = "",
        action: str,
        result: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        clean_payload = _redact_payload(payload or {})
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audits (
                    timestamp, source, workspace, telegram_user_id, telegram_chat_id,
                    telegram_message_id, shamsu_session_id, run_id, action,
                    result, payload
                )
                VALUES (?, 'telegram', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    self.workspace_key,
                    int(telegram_user_id),
                    int(telegram_chat_id),
                    int(telegram_message_id),
                    session_id,
                    run_id,
                    action,
                    result,
                    json.dumps(clean_payload, ensure_ascii=True, sort_keys=True),
                ),
            )

    def list_audit(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM audits ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def increment_metric(self, name: str, amount: int = 1) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO metrics (name, value) VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET value = value + excluded.value
                """,
                (name, int(amount)),
            )

    def metrics(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT name, value FROM metrics ORDER BY name").fetchall()
        return {str(row["name"]): int(row["value"]) for row in rows}

    def get_meta(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, default=str), object_hook=_redact_object)


def _redact_object(obj: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in obj.items():
        lowered = str(key).lower()
        if any(marker in lowered for marker in ("token", "secret", "password", "api_key")):
            clean[key] = "[REDACTED]"
        elif isinstance(value, str):
            clean[key] = redact(value)
        else:
            clean[key] = value
    return clean
