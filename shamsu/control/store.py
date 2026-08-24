"""The control plane: one small database three processes can agree through.

The CLI, the web portal and the Telegram bot are separate processes. Everything
they need to share turns out to be the same shape:

- **A lease** - who is running this thread right now.
- **A queue** - what should run next on it.
- **An approval** - a question exactly one surface may answer.

All three are "durable record, many readers, exactly one winner on write", so
they live in one file and use one trick: `BEGIN IMMEDIATE` plus a conditional
`UPDATE` whose `rowcount` decides who won. Splitting them into three mechanisms
would have meant getting the same race right three times.

Why this exists at all: `runtime/run_control.py` keeps runs in a module-level
dict, so a second process sees an idle machine while the first is thirty
minutes into a build. That is fine while one REPL is the only surface, and
wrong the moment a browser can start a turn.

**Files stay the system of record.** This database holds coordination, not
content: no transcripts, no activity, nothing that could not be rebuilt by
deleting it and starting again. `messages.jsonl` and `activity.jsonl` remain
the things you can `cat`.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from shamsu.runtime.home import shamsu_home

SCHEMA_VERSION = 1

#: A lease this old with no heartbeat is treated as abandoned. Long enough to
#: survive a slow turn's pauses, short enough that a killed process does not
#: lock a thread for the rest of the day.
LEASE_STALE_SECONDS = 90.0

#: How often a live owner should call `renew_lease`. Comfortably inside the
#: staleness window, so one missed beat is not a lost lease.
LEASE_HEARTBEAT_SECONDS = 20.0

#: Unanswered approvals fail closed. Matching the Telegram broker's existing
#: default rather than inventing a second number.
APPROVAL_TIMEOUT_SECONDS = 900.0

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
CANCELLED = "cancelled"

ALLOW = "allow"
DENY = "deny"

#: The install-wide run slot, keyed as a lease so the machine gate and the
#: per-thread gate are one mechanism. Deliberately not path-shaped: `_key`
#: resolves real workspaces, and an earlier sentinel of `"*"` resolved against
#: the CURRENT DIRECTORY - so two processes started in different folders
#: computed different keys and both believed they held the only run slot. The
#: gate silently did nothing, and only across processes.
MACHINE_LEASE_KEY = "*machine*"


def control_db_path() -> Path:
    return shamsu_home() / "control.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool:
    """Is that process still running?

    A lease is only abandoned if BOTH the pid is gone and the heartbeat is
    stale - either test alone is wrong. A pid can be recycled by an unrelated
    program, and a fresh heartbeat cannot distinguish a busy owner from one
    that crashed a moment ago.
    """
    if pid <= 0:
        return False
    try:
        import psutil

        return psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001 - psutil missing or refusing; fall through
        pass
    if os.name == "nt":  # pragma: no cover - exercised on Windows only
        return True
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


@dataclass(frozen=True)
class Lease:
    workspace: str
    session_id: str
    owner_pid: int
    owner_surface: str
    heartbeat: str

    @property
    def is_mine(self) -> bool:
        return self.owner_pid == os.getpid()


@dataclass(frozen=True)
class QueuedPrompt:
    queue_id: int
    workspace: str
    session_id: str
    source: str
    text: str
    status: str
    created_at: str


@dataclass(frozen=True)
class Approval:
    approval_id: str
    workspace: str
    session_id: str
    run_id: str
    action_type: str
    description: str
    risk_level: str
    preview: str
    created_at: str
    expires_at: str
    decision: str
    decided_by: str


class ControlStore:
    """Shared coordination state. Safe to construct in any process, any thread."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else control_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # -- plumbing --------------------------------------------------------

    @contextlib.contextmanager
    def _connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            if write:
                # IMMEDIATE takes the write lock up front. Without it two
                # processes can both read "unclaimed", both decide to claim,
                # and the second gets SQLITE_BUSY at COMMIT - by which point it
                # has already told its caller it won.
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if write:
                conn.execute("COMMIT")
        except Exception:
            if write:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leases (
                    workspace TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    owner_pid INTEGER NOT NULL,
                    owner_surface TEXT NOT NULL,
                    heartbeat TEXT NOT NULL,
                    PRIMARY KEY (workspace, session_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS queue (
                    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    claimed_by INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS queue_pending "
                "ON queue (workspace, session_id, status, queue_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    action_type TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    risk_level TEXT NOT NULL DEFAULT '',
                    preview TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT '',
                    decided_by TEXT NOT NULL DEFAULT '',
                    decided_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta_kv "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta_kv (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- leases ----------------------------------------------------------

    def lease_holder(self, workspace: Path | str, session_id: str) -> Lease | None:
        """Who owns this thread, if anyone still really does."""
        key = _key(workspace)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM leases WHERE workspace = ? AND session_id = ?",
                (key, session_id),
            ).fetchone()
        if row is None:
            return None
        lease = _lease(row)
        if _lease_is_live(lease):
            return lease
        return None

    def acquire_lease(
        self, workspace: Path | str, session_id: str, surface: str = "cli"
    ) -> bool:
        """Claim this thread. True if it is now ours.

        Re-entrant for the same process: a REPL that already owns a thread and
        asks again gets True rather than deadlocking against itself.
        """
        key = _key(workspace)
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM leases WHERE workspace = ? AND session_id = ?",
                (key, session_id),
            ).fetchone()
            if row is not None:
                lease = _lease(row)
                if lease.owner_pid != os.getpid() and _lease_is_live(lease):
                    return False
            conn.execute(
                """
                INSERT INTO leases (workspace, session_id, owner_pid, owner_surface, heartbeat)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workspace, session_id) DO UPDATE SET
                    owner_pid=excluded.owner_pid,
                    owner_surface=excluded.owner_surface,
                    heartbeat=excluded.heartbeat
                """,
                (key, session_id, os.getpid(), surface, _now()),
            )
        return True

    def renew_lease(self, workspace: Path | str, session_id: str) -> bool:
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                "UPDATE leases SET heartbeat = ? "
                "WHERE workspace = ? AND session_id = ? AND owner_pid = ?",
                (_now(), _key(workspace), session_id, os.getpid()),
            )
            return cursor.rowcount > 0

    def release_lease(self, workspace: Path | str, session_id: str) -> bool:
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                "DELETE FROM leases WHERE workspace = ? AND session_id = ? AND owner_pid = ?",
                (_key(workspace), session_id, os.getpid()),
            )
            return cursor.rowcount > 0

    def active_leases(self) -> list[Lease]:
        """Every thread being run right now, anywhere on this machine.

        The install-wide "one run at a time" gate reads this. Serialising per
        thread is not enough on its own: two turns in two different projects
        would pass that test and still contend for one GPU.
        """
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM leases").fetchall()
        return [lease for lease in map(_lease, rows) if _lease_is_live(lease)]

    def clear_stale_leases(self) -> int:
        removed = 0
        with self._connect(write=True) as conn:
            for row in conn.execute("SELECT * FROM leases").fetchall():
                lease = _lease(row)
                if not _lease_is_live(lease):
                    conn.execute(
                        "DELETE FROM leases WHERE workspace = ? AND session_id = ?",
                        (lease.workspace, lease.session_id),
                    )
                    removed += 1
        return removed

    # -- queue -----------------------------------------------------------

    def enqueue(
        self, workspace: Path | str, session_id: str, text: str, source: str = "cli"
    ) -> int:
        now = _now()
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                """
                INSERT INTO queue (workspace, session_id, source, text, status,
                                   created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (_key(workspace), session_id, source, text, now, now),
            )
            return int(cursor.lastrowid or 0)

    def claim_next(self, workspace: Path | str, session_id: str) -> QueuedPrompt | None:
        """Take the oldest queued prompt for this thread, atomically.

        Two processes calling this at the same moment must not both get the
        same row - which is what the conditional UPDATE inside an IMMEDIATE
        transaction guarantees.
        """
        key = _key(workspace)
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM queue WHERE workspace = ? AND session_id = ? "
                "AND status = 'queued' ORDER BY queue_id LIMIT 1",
                (key, session_id),
            ).fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                "UPDATE queue SET status = 'running', claimed_by = ?, updated_at = ? "
                "WHERE queue_id = ? AND status = 'queued'",
                (os.getpid(), _now(), int(row["queue_id"])),
            )
            if cursor.rowcount == 0:
                return None
            claimed = dict(row)
            claimed["status"] = RUNNING
            return _queued(claimed)

    def finish(self, queue_id: int, status: str = DONE) -> None:
        with self._connect(write=True) as conn:
            conn.execute(
                "UPDATE queue SET status = ?, updated_at = ? WHERE queue_id = ?",
                (status, _now(), int(queue_id)),
            )

    def cancel_queued(self, queue_id: int) -> bool:
        """Drop a prompt that has not started. A running one is not cancellable here."""
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                "UPDATE queue SET status = 'cancelled', updated_at = ? "
                "WHERE queue_id = ? AND status = 'queued'",
                (_now(), int(queue_id)),
            )
            return cursor.rowcount > 0

    def pending(self, workspace: Path | str, session_id: str) -> list[QueuedPrompt]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM queue WHERE workspace = ? AND session_id = ? "
                "AND status = 'queued' ORDER BY queue_id",
                (_key(workspace), session_id),
            ).fetchall()
        return [_queued(dict(row)) for row in rows]

    def queue_depth(self, workspace: Path | str, session_id: str) -> int:
        return len(self.pending(workspace, session_id))

    # -- approvals -------------------------------------------------------

    def raise_approval(
        self,
        *,
        workspace: Path | str,
        session_id: str,
        run_id: str = "",
        action_type: str = "",
        description: str = "",
        risk_level: str = "",
        preview: str = "",
        timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS,
        approval_id: str = "",
    ) -> str:
        approval_id = approval_id or f"approval-{uuid.uuid4().hex[:16]}"
        created = datetime.now(timezone.utc)
        expires = created.timestamp() + float(timeout_seconds)
        with self._connect(write=True) as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    approval_id, workspace, session_id, run_id, action_type,
                    description, risk_level, preview, created_at, expires_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    _key(workspace),
                    session_id,
                    run_id,
                    action_type,
                    description,
                    risk_level,
                    preview,
                    created.isoformat(),
                    datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                ),
            )
        return approval_id

    def resolve_approval(self, approval_id: str, decision: str, decided_by: str) -> bool:
        """Answer it. True only for the FIRST answer.

        The phone and the browser can both be showing the same card; both
        people can tap Allow. Exactly one of those writes may count, or the
        agent could be told "approved" twice and a second surface would think
        its answer was the one that mattered.
        """
        if decision not in (ALLOW, DENY):
            raise ValueError(f"decision must be {ALLOW!r} or {DENY!r}")
        with self._connect(write=True) as conn:
            cursor = conn.execute(
                "UPDATE approvals SET decision = ?, decided_by = ?, decided_at = ? "
                "WHERE approval_id = ? AND decision = ''",
                (decision, decided_by, _now(), approval_id),
            )
            return cursor.rowcount > 0

    def approval(self, approval_id: str) -> Approval | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        return _approval(row) if row is not None else None

    def pending_approvals(
        self, workspace: Path | str | None = None, session_id: str = ""
    ) -> list[Approval]:
        """Unanswered, unexpired approvals - everywhere, or for one thread.

        Install-wide by default on purpose: a surface should be able to show
        you "something is waiting on you" without knowing which project it is
        in, which is the whole point of being able to answer from anywhere.
        """
        clauses = ["decision = ''"]
        params: list[Any] = []
        if workspace is not None:
            clauses.append("workspace = ?")
            params.append(_key(workspace))
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM approvals WHERE {' AND '.join(clauses)} ORDER BY created_at",
                tuple(params),
            ).fetchall()
        return [item for item in map(_approval, rows) if not _expired(item)]

    def wait_for_decision(
        self,
        approval_id: str,
        *,
        timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS,
        poll_seconds: float = 0.25,
        should_stop=lambda: False,
    ) -> str:
        """Block until answered, expired, or told to stop. Returns the decision.

        Polling rather than notifying: the answer can arrive in a different
        process, so there is no in-memory event to wait on, and a quarter-second
        poll against a local SQLite file costs nothing next to a model call.

        Fails CLOSED. An unanswered approval is a denial, never an allow.
        """
        deadline = time.monotonic() + float(timeout_seconds)
        while time.monotonic() < deadline and not should_stop():
            record = self.approval(approval_id)
            if record is None:
                return DENY
            if record.decision:
                return record.decision
            if _expired(record):
                break
            time.sleep(poll_seconds)
        self.resolve_approval(approval_id, DENY, "timeout")
        return DENY

    def expire_approvals(self) -> int:
        """Stamp every overdue unanswered approval as a timeout denial.

        `pending_approvals()` cannot be the source here, and that WAS the bug:
        it filters expired rows out before returning, so the loop that walked
        it could never find one to expire. Nothing else called this either, so
        an approval nobody answered kept `decision = ''` for ever - and every
        surface that reads "gone from the pending list" as "somebody answered"
        then reported a decision that no human ever made. Live 2026-08-24:
        131 of 176 rows in one install were orphans, and three of them
        announced themselves as `Approval resolved on another surface.` in a
        terminal whose user had touched nothing.

        Fails CLOSED, like every other unanswered path: a timeout is a denial.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM approvals WHERE decision = ''"
            ).fetchall()
        expired = 0
        for record in map(_approval, rows):
            if _expired(record) and self.resolve_approval(
                record.approval_id, DENY, "timeout"
            ):
                expired += 1
        return expired


# -- helpers ---------------------------------------------------------------


def _key(workspace: Path | str) -> str:
    """A workspace's stable identity. The machine slot is not a path."""
    if str(workspace) == MACHINE_LEASE_KEY:
        return MACHINE_LEASE_KEY
    return str(Path(workspace).resolve())


def _lease(row: sqlite3.Row) -> Lease:
    return Lease(
        workspace=str(row["workspace"]),
        session_id=str(row["session_id"]),
        owner_pid=int(row["owner_pid"]),
        owner_surface=str(row["owner_surface"]),
        heartbeat=str(row["heartbeat"]),
    )


def _lease_is_live(lease: Lease) -> bool:
    if not _pid_alive(lease.owner_pid):
        return False
    return _age_seconds(lease.heartbeat) <= LEASE_STALE_SECONDS


def _queued(row: dict[str, Any]) -> QueuedPrompt:
    return QueuedPrompt(
        queue_id=int(row["queue_id"]),
        workspace=str(row["workspace"]),
        session_id=str(row["session_id"]),
        source=str(row["source"]),
        text=str(row["text"]),
        status=str(row["status"]),
        created_at=str(row["created_at"]),
    )


def _approval(row: sqlite3.Row) -> Approval:
    return Approval(
        approval_id=str(row["approval_id"]),
        workspace=str(row["workspace"]),
        session_id=str(row["session_id"]),
        run_id=str(row["run_id"]),
        action_type=str(row["action_type"]),
        description=str(row["description"]),
        risk_level=str(row["risk_level"]),
        preview=str(row["preview"]),
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
        decision=str(row["decision"]),
        decided_by=str(row["decided_by"]),
    )


def _expired(record: Approval) -> bool:
    if record.decision:
        return False
    try:
        expires = datetime.fromisoformat(record.expires_at)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= expires


def _age_seconds(stamp: str) -> float:
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return float("inf")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()
