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

SCHEMA_VERSION = 4

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

# --------------------------------------------------------------------------
# Migration 2 -- artifact registry
# --------------------------------------------------------------------------

_MIGRATION_2 = """
-- Artifact CONTENT lives on disk under .shamsu/artifacts/ so it stays
-- human-readable, greppable, and diffable. This table is the registry: it owns
-- freshness, versioning, and provenance, which are the parts that must be
-- queryable and transactional.
CREATE TABLE artifact_records (
    artifact_id       TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    kind              TEXT NOT NULL,
    key               TEXT NOT NULL,
    content_path      TEXT NOT NULL,
    artifact_version  INTEGER NOT NULL DEFAULT 1,
    generator_version TEXT NOT NULL,
    status            TEXT NOT NULL,
    confidence        REAL NOT NULL DEFAULT 1.0,
    created_at        TEXT NOT NULL,
    refreshed_at      TEXT NOT NULL,
    UNIQUE (project_id, kind, key)
);
CREATE INDEX idx_artifacts_kind ON artifact_records(project_id, kind);
CREATE INDEX idx_artifacts_status ON artifact_records(project_id, status);

-- One row per source file an artifact claims to describe, with the hash at
-- build time. Invalidation is a join against this table, which is why the
-- path index matters: "which artifacts did this edit invalidate?" is the
-- question asked after every mutating tool call.
CREATE TABLE artifact_sources (
    artifact_id  TEXT NOT NULL REFERENCES artifact_records(artifact_id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (artifact_id, path)
);
CREATE INDEX idx_artifact_sources_path ON artifact_sources(path);

-- Recorded disagreements between an artifact and a fresh tool result. The
-- rate of these is the artifact_freshness_error_rate evaluation metric, so
-- they are kept even after the artifact is regenerated.
CREATE TABLE artifact_contradictions (
    contradiction_id TEXT PRIMARY KEY,
    artifact_id      TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    artifact_claim   TEXT NOT NULL,
    fresh_observation TEXT NOT NULL,
    source_tool      TEXT NOT NULL
);
CREATE INDEX idx_contradictions_artifact ON artifact_contradictions(artifact_id);
"""

# --------------------------------------------------------------------------
# Migration 3 -- project memory (plan section 13.1, layer 2)
# --------------------------------------------------------------------------
#
# Three tables, three different kinds of knowledge, deliberately not merged:
#
# * `project_facts` are small, checkable claims about the project. Each records
#   the paths it depends on, so a fact can be marked unverified when the code
#   underneath it changes rather than quietly outliving its evidence.
# * `architecture_decisions` are ADRs (plan section 15.13). They are narrative and
#   human-authored, they supersede rather than update, and they are never
#   invalidated by a file changing -- a decision that was made stays made.
# * `memory_records` are lessons learned from failures, keyed by error
#   signature so a recurrence can be recognised across tasks.
#
# `source_event_id` on a fact points at the tool event that produced it, with
# the same intent as the evidence table: a fact learned by observation can be
# told apart from one a model asserted, and the two must never carry the same
# weight.

_MIGRATION_3 = """
CREATE TABLE project_facts (
    fact_id           TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    kind              TEXT NOT NULL,
    subject           TEXT NOT NULL,
    statement         TEXT NOT NULL,
    origin            TEXT NOT NULL,
    source_event_id   TEXT REFERENCES tool_events(event_id) ON DELETE SET NULL,
    confidence        REAL NOT NULL,
    confirmations     INTEGER NOT NULL DEFAULT 0,
    contradictions    INTEGER NOT NULL DEFAULT 0,
    evidence_paths    TEXT NOT NULL DEFAULT '[]',
    evidence_hash     TEXT NOT NULL DEFAULT '',
    verified          INTEGER NOT NULL DEFAULT 1,
    superseded_by     TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (project_id, kind, subject)
);
CREATE INDEX idx_facts_project ON project_facts(project_id, verified);

CREATE TABLE architecture_decisions (
    decision_id     TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    context         TEXT NOT NULL DEFAULT '',
    decision        TEXT NOT NULL,
    alternatives    TEXT NOT NULL DEFAULT '[]',
    consequences    TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL,
    related_paths   TEXT NOT NULL DEFAULT '[]',
    related_tasks   TEXT NOT NULL DEFAULT '[]',
    supersedes      TEXT REFERENCES architecture_decisions(decision_id),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_decisions_project ON architecture_decisions(project_id, status);

CREATE TABLE memory_records (
    memory_id       TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    task_id         TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
    kind            TEXT NOT NULL,
    signature       TEXT NOT NULL DEFAULT '',
    statement       TEXT NOT NULL,
    resolution      TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL,
    occurrences     INTEGER NOT NULL DEFAULT 1,
    related_paths   TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_memory_signature ON memory_records(project_id, signature);
"""

#: Step dependencies, so a failed step can take only its dependents down with
#: it. Added as a nullable-with-default column rather than a table: a step's
#: dependencies are part of the step, they are written once when the plan is
#: materialised, and nothing ever queries them in the other direction.
#:
#: `DEFAULT '[]'` is what makes this safe on a database written by an older
#: build — every existing step reads back as independent, which is exactly the
#: behaviour those rows had when they were written.
_MIGRATION_4 = """
ALTER TABLE plan_steps ADD COLUMN depends_on TEXT NOT NULL DEFAULT '[]';
"""

MIGRATIONS: Sequence[str] = (_MIGRATION_1, _MIGRATION_2, _MIGRATION_3, _MIGRATION_4)


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
    * ``check_same_thread=False`` because cancellation must work from a signal
      handler or UI thread and cancelling writes run status. `StateStore`
      serialises every access on a lock; do not share this connection without
      equivalent protection.
    """
    connection = sqlite3.connect(path, isolation_level="DEFERRED", check_same_thread=False)
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
