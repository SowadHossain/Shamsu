"""SQLite schema and forward migrations.

Versioned through SQLite's ``user_version`` pragma, which is stored in the
database header and needs no bookkeeping table of its own.

Rules for changing this file:

* Migrations are append-only. Never edit a shipped migration -- a database in
  the field has already run it, so editing it makes the schema depend on when
  the database was created.
* Every migration is idempotent within its own version and runs inside the
  transaction the runner opens.
* Foreign keys are declared and enforced. An orphaned evidence row pointing at
  a tool event that does not exist would be a completion-gate bypass.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

SCHEMA_VERSION = 1

# --------------------------------------------------------------------------
# Migration 1 -- initial runtime state
# --------------------------------------------------------------------------

_MIGRATION_1 = """
CREATE TABLE projects (
    project_id       TEXT PRIMARY KEY,
    root             TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    languages        TEXT NOT NULL DEFAULT '[]',
    frameworks       TEXT NOT NULL DEFAULT '[]',
    package_managers TEXT NOT NULL DEFAULT '[]',
    database_types   TEXT NOT NULL DEFAULT '[]',
    test_commands    TEXT NOT NULL DEFAULT '[]',
    active_branch    TEXT,
    index_version    INTEGER NOT NULL DEFAULT 0,
    artifact_version INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE tasks (
    task_id              TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    request              TEXT NOT NULL,
    kind                 TEXT,
    state                TEXT NOT NULL,
    phase                TEXT NOT NULL,
    plan_id              TEXT,
    current_step_id      TEXT,
    action_count         INTEGER NOT NULL DEFAULT 0,
    repair_count         INTEGER NOT NULL DEFAULT 0,
    replan_count         INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    final_result         TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX idx_tasks_project ON tasks(project_id);

CREATE TABLE runs (
    run_id                   TEXT PRIMARY KEY,
    project_id               TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    task_id                  TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    status                   TEXT NOT NULL,
    started_at               TEXT NOT NULL,
    ended_at                 TEXT,
    wall_clock_limit_seconds REAL NOT NULL,
    cancel_reason            TEXT
);
CREATE INDEX idx_runs_task ON runs(task_id);
CREATE INDEX idx_runs_status ON runs(status);

CREATE TABLE plans (
    plan_id       TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    version       INTEGER NOT NULL,
    summary       TEXT NOT NULL,
    superseded_by TEXT,
    created_at    TEXT NOT NULL,
    UNIQUE (task_id, version)
);
CREATE INDEX idx_plans_task ON plans(task_id);

CREATE TABLE plan_steps (
    step_id             TEXT PRIMARY KEY,
    plan_id             TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
    ordinal             INTEGER NOT NULL,
    title               TEXT NOT NULL,
    inputs              TEXT NOT NULL DEFAULT '[]',
    outputs             TEXT NOT NULL DEFAULT '[]',
    constraints         TEXT NOT NULL DEFAULT '[]',
    allowed_tools       TEXT NOT NULL DEFAULT '[]',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    required_evidence   TEXT NOT NULL DEFAULT '[]',
    risk                TEXT NOT NULL,
    approval_required   INTEGER NOT NULL DEFAULT 0,
    outcome             TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    UNIQUE (plan_id, ordinal)
);
CREATE INDEX idx_steps_plan ON plan_steps(plan_id);

CREATE TABLE tool_events (
    event_id         TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    task_id          TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    step_id          TEXT,
    tool             TEXT NOT NULL,
    phase            TEXT NOT NULL,
    arguments_json   TEXT NOT NULL,
    ok               INTEGER NOT NULL,
    output           TEXT NOT NULL DEFAULT '',
    error            TEXT,
    truncated        INTEGER NOT NULL DEFAULT 0,
    original_bytes   INTEGER,
    duration_seconds REAL NOT NULL DEFAULT 0.0,
    created_at       TEXT NOT NULL
);
CREATE INDEX idx_events_task ON tool_events(task_id);
CREATE INDEX idx_events_run ON tool_events(run_id);

-- source_event_id is NOT NULL and a foreign key on purpose: evidence that
-- does not trace to an observed tool execution cannot be inserted at all.
CREATE TABLE evidence (
    evidence_id     TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    step_id         TEXT,
    kind            TEXT NOT NULL,
    source_event_id TEXT NOT NULL REFERENCES tool_events(event_id) ON DELETE CASCADE,
    detail          TEXT NOT NULL DEFAULT '',
    recorded_at     TEXT NOT NULL
);
CREATE INDEX idx_evidence_task ON evidence(task_id);
CREATE INDEX idx_evidence_step ON evidence(step_id);

CREATE TABLE approvals (
    approval_id  TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    step_id      TEXT,
    reason       TEXT NOT NULL,
    risk         TEXT NOT NULL,
    decision     TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_at   TEXT
);
CREATE INDEX idx_approvals_task ON approvals(task_id);

CREATE TABLE checkpoints (
    checkpoint_id       TEXT PRIMARY KEY,
    task_id             TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    step_id             TEXT,
    label               TEXT NOT NULL,
    git_ref             TEXT,
    state_snapshot_json TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX idx_checkpoints_task ON checkpoints(task_id);

CREATE TABLE failures (
    failure_id TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    step_id    TEXT,
    kind       TEXT NOT NULL,
    signature  TEXT NOT NULL,
    expected   TEXT NOT NULL DEFAULT '',
    actual     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    attempt    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_failures_task ON failures(task_id);
-- Repeated-failure detection queries this directly.
CREATE INDEX idx_failures_signature ON failures(task_id, signature);
"""

MIGRATIONS: Sequence[str] = (_MIGRATION_1,)


def current_version(connection: sqlite3.Connection) -> int:
    """The schema version recorded in the database header."""
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(connection: sqlite3.Connection) -> int:
    """Apply every pending migration. Returns the resulting version.

    Raises:
        RuntimeError: the database is newer than this code understands.
            Refusing is the honest response -- a newer schema may have
            semantics this build would silently violate.
    """
    version = current_version(connection)

    if version > len(MIGRATIONS):
        raise RuntimeError(
            f"database schema version {version} is newer than this build supports "
            f"({len(MIGRATIONS)}). Upgrade SHAMSU rather than downgrading the database."
        )

    for index in range(version, len(MIGRATIONS)):
        with connection:
            connection.executescript(MIGRATIONS[index])
            # executescript commits any open transaction, so set the version
            # after the DDL rather than inside the same implicit statement.
            connection.execute(f"PRAGMA user_version = {index + 1}")

    return current_version(connection)


def connect(path: str) -> sqlite3.Connection:
    """Open a connection configured for runtime state.

    * ``foreign_keys`` on -- off by default in SQLite, and the evidence
      integrity guarantee depends on it.
    * WAL journaling so a reader (status, `--watch`) never blocks the writer.
    * ``NORMAL`` synchronous: durable across process crashes, which is the
      failure mode that matters for resume. Full fsync per commit is not worth
      it for a local agent's event stream.
    """
    connection = sqlite3.connect(path, isolation_level="DEFERRED")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


__all__ = [
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "connect",
    "current_version",
    "migrate",
]
