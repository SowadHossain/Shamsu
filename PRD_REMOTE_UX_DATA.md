# PRD Addendum — Storage, Catalog and Control Plane

**Companion to `PRD_REMOTE_UX.md`. Read that first; this replaces its §10 and reorders its §11.**

Status: DRAFT — for review. No code has been changed.
Author: Claude (planning pass, 2026-08-18)

---

## 1. The question, and the honest answer

> "we will potentially need a central database to hold the chats and everything,
> as we'll be running stuff from the web portal"

Yes — but not the way the phrasing implies, and the reason is not the chats.

I checked the assumption I had made in `PRD_REMOTE_UX.md` (that today's
file-per-workspace layout would carry a web portal). It does not, for **two
measured reasons and one structural one**. The chats themselves are the part
that is *least* in trouble.

The recommendation is a **two-tier split**: keep the workspace files as the
system of record, add a central SQLite **catalog + control plane** as a derived,
rebuildable read model. Not "move everything into a database."

---

## 2. What I measured

### 2.1 The session index is rewritten in full on every logged event

`SessionLogger.log()` ([manager.py:611-640](shamsu/session/manager.py#L611-L640))
ends with:

```python
self.manager._write_metadata(self.metadata)
self.manager._upsert_index(self.metadata)
```

and `_upsert_index` ([manager.py:401-414](shamsu/session/manager.py#L401-L414)):

```python
with FileLock(str(self.index_lock_path), timeout=30):
    index = self._read_index()          # parse the WHOLE index
    ...
    self._write_index(index)            # serialize and os.replace the WHOLE index
```

Measured on a real workspace in this repo — `test-shamsu/openbazaar-build/`:

| Metric | Value |
|---|---|
| Sessions in one workspace | **127** |
| `sessions/index.json` size | **122 KB** |
| Events logged across those sessions | 7,921 |
| Events in the largest single session | **225** |
| `messages.jsonl` total | 2.0 MB |

So that one session performed **225 full read-modify-write cycles of a 122 KB
JSON file, each under a workspace-global `FileLock`** — roughly 27 MB of JSON
serialization purely for bookkeeping, and 225 serialization points.

Today this is survivable because there is effectively **one writer**. The web
portal makes it three (CLI + Telegram + web), plus the `activity.jsonl` stream
proposed in `PRD_REMOTE_UX.md` §4.3 as a fourth, high-frequency one. The cost
also grows with session count: the 128th session pays for all 127 before it.

**This is a latent bug the portal will expose, not a theoretical one.**

### 2.2 Cross-workspace search has no index at all

`search_sessions()` ([manager.py:339-353](shamsu/session/manager.py#L339-L353))
carries its own honest disclaimer:

```python
"""Local, JSONL-scan search across titles, summaries, transcript/chat
messages, and local memory. No FTS/index — a simple scan is enough for
the workspace-local scale."""
```

That comment is **correct and explicitly scoped**: *workspace-local*. The web
portal's primary screen is the opposite — every workspace, every thread, one
search box. At 2 MB of transcript per workspace, a cross-workspace search is a
full scan of every JSONL file in every registered project, per keystroke.

The existing scan is not wrong. The portal simply invalidates its stated premise.

### 2.3 The structural one: run state is in-process memory

This is the finding that actually decides the design.

[run_control.py:78-80](shamsu/runtime/run_control.py#L78-L80):

```python
_RUNS: dict[str, ControlledRun] = {}
_RUN_HISTORY: dict[str, ControlledRun] = {}
_CURRENT_RUN: contextvars.ContextVar[ControlledRun | None] = ...
```

`active_runs_for_session()`, `cancel_run()`, `pause_run()`, `resume_run()` and
`add_feedback()` all read that module-level dict.

**Consequence: a web portal in a separate process cannot see or control any
run.** It would show "idle" while the CLI is thirty minutes into a build, and
its Stop button would silently do nothing. Same for a second REPL.

My original PRD sidestepped this by putting the web server *in a daemon thread
inside the REPL process* (§8.2). That works, but it buys the sidestep at a high
price:

- No portal unless a REPL is open.
- Two REPLs → two portals, each blind to the other's runs.
- No headless `shamsu serve` is ever possible.

"We'll be running stuff from the web portal" is precisely the requirement that
makes the sidestep unacceptable. **Run control has to become shared state.**

---

## 3. Design: files are truth, the catalog is a projection

Three tiers, explicitly separated. The split is the whole idea.

### Tier 1 — System of record (per workspace, files, unchanged)

| Path | Role |
|---|---|
| `<ws>/.shamsu/sessions/<id>/messages.jsonl` | the lossless transcript |
| `<ws>/.shamsu/sessions/<id>/events.jsonl` | the event log |
| `<ws>/.shamsu/sessions/<id>/activity.jsonl` | the turn stream (PRD §4.3) |
| `<ws>/.shamsu/runs/<run_id>/` | action-ledger evidence |

Append-only. Greppable. Survives everything. **Never queried by the portal.**

This tier is non-negotiable, for a reason with scar tissue behind it: the
transcript being clipped on disk, and the incident where a `.jsonl` reformatted
by an editor caused 655 of 657 lines to fail to parse and a session to hydrate
one message *silently*. `messages.jsonl` stays the thing you can `cat`, and the
lossless-on-write rule at [manager.py:713-717](shamsu/session/manager.py#L713-L717)
stays exactly as written.

### Tier 2 — Central catalog (`~/.shamsu/shamsu.db`, SQLite WAL)

A **derived, disposable read model**. Everything in it can be rebuilt from
Tier 1 by `shamsu reindex`. Delete it and nothing is lost but time.

This is what the portal queries, and the only thing that can answer
"all workspaces, all threads, sorted by recency, filtered by this text".

### Tier 3 — Control plane (same DB, different tables)

Shared, cross-process run ownership and command queues. Replaces the *visibility*
role of `_RUNS` without replacing the in-memory object the owning process uses.

---

## 4. Why SQLite, and not a real database

**Recommendation: SQLite in WAL mode. Not Postgres, not MySQL, not a service.**

- The project's prime constraint is local-first with no service dependencies
  (`CLAUDE.md`, invariant 5). A database you have to *start* breaks SHAMSU for
  its actual user: someone on a laptop with 8 GB of VRAM and no docker running.
- The codebase already has a proven WAL idiom in two places —
  [telegram/storage.py:38-45](shamsu/integrations/telegram/storage.py#L38-L45)
  and [runtime/task_state.py:281-288](shamsu/runtime/task_state.py#L281-L288).
  This is a third instance of an established pattern, not a new dependency.
- SQLite WAL gives exactly what is missing: **concurrent readers with one
  writer, no full-file rewrite, no global lock held across a parse**. That is a
  direct fix for §2.1.
- FTS5 is stdlib and already used for the code index
  (`.shamsu/index.db`), so cross-workspace search is a solved problem with zero
  new dependencies.

**Where this could go wrong, and the hedge:** if SHAMSU ever becomes multi-user
or hosted, SQLite's single-writer model becomes the ceiling. So the catalog goes
behind a `CatalogStore` protocol in `shamsu/interfaces.py` style, with
`SqliteCatalogStore` as the only implementation. Schema uses no SQLite-specific
types outside the FTS table. Swapping in Postgres later is then a new class, not
a rewrite. **I am not proposing we build that now** — multi-user is an explicit
non-goal — only that we do not build a wall in front of it.

---

## 5. Schema

`~/.shamsu/shamsu.db`, `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`.

```sql
-- ---------- catalog (derived; rebuildable) ----------
CREATE TABLE workspaces (
  workspace_id TEXT PRIMARY KEY,      -- stable hash of the resolved path
  path         TEXT NOT NULL UNIQUE,
  label        TEXT NOT NULL DEFAULT '',
  added_at     TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  present      INTEGER NOT NULL DEFAULT 1   -- 0 = path gone; kept, not deleted
);

CREATE TABLE sessions (
  session_id     TEXT PRIMARY KEY,
  workspace_id   TEXT NOT NULL REFERENCES workspaces(workspace_id),
  title          TEXT NOT NULL DEFAULT '',
  status         TEXT NOT NULL DEFAULT 'active',
  parent_session_id TEXT NOT NULL DEFAULT '',   -- enables "fork" (PRD §9.8)
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  message_count  INTEGER NOT NULL DEFAULT 0,
  last_prompt    TEXT NOT NULL DEFAULT '',
  source         TEXT NOT NULL DEFAULT 'cli'    -- cli | telegram | web
);
CREATE INDEX ix_sessions_ws_updated ON sessions(workspace_id, updated_at DESC);
CREATE INDEX ix_sessions_updated    ON sessions(updated_at DESC);

CREATE TABLE turns (
  turn_id     TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL REFERENCES sessions(session_id),
  seq         INTEGER NOT NULL,
  source      TEXT NOT NULL,              -- cli | telegram | web
  prompt      TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL,              -- running | done | failed | cancelled
  started_at  TEXT NOT NULL,
  ended_at    TEXT NOT NULL DEFAULT '',
  model       TEXT NOT NULL DEFAULT '',
  files_changed INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX ux_turns_session_seq ON turns(session_id, seq);

CREATE TABLE messages (
  message_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL REFERENCES sessions(session_id),
  turn_id     TEXT NOT NULL DEFAULT '',
  line_no     INTEGER NOT NULL,          -- 1-based line in messages.jsonl
  role        TEXT NOT NULL,
  ts          TEXT NOT NULL,
  content     TEXT NOT NULL              -- redacted copy; file remains truth
);
CREATE UNIQUE INDEX ux_messages_session_line ON messages(session_id, line_no);

CREATE VIRTUAL TABLE messages_fts USING fts5(
  content, session_id UNINDEXED, message_id UNINDEXED,
  content='messages', content_rowid='message_id'
);
```

On storing `content` twice: the alternative is a `(offset, length)` pointer into
`messages.jsonl`, which keeps the catalog tiny. **Rejected.** Offsets are exactly
the thing that broke when an editor reformatted a `.jsonl` in place — the
incident that cost fifteen minutes and hydrated one message silently. A
duplicated, redacted copy costs ~2 MB per workspace (measured), which is
nothing, and it cannot desynchronise into a *wrong* answer — only a stale one,
which `reindex` fixes. Cheap beats clever here.

```sql
-- ---------- control plane (live; authoritative for "who owns this run") ----------
CREATE TABLE runs (
  run_id       TEXT PRIMARY KEY,
  session_id   TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  status       TEXT NOT NULL,            -- queued|running|paused|done|failed|cancelled|orphaned
  owner_pid    INTEGER NOT NULL DEFAULT 0,
  owner_host   TEXT NOT NULL DEFAULT '',
  owner_surface TEXT NOT NULL DEFAULT '',-- cli | telegram | web | serve
  started_at   TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,            -- bumped every 5s by the owner
  ended_at     TEXT NOT NULL DEFAULT '',
  last_message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_runs_session_status ON runs(session_id, status);

CREATE TABLE run_commands (
  command_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id      TEXT NOT NULL,
  kind        TEXT NOT NULL,             -- cancel | pause | resume | feedback
  payload     TEXT NOT NULL DEFAULT '',
  issued_by   TEXT NOT NULL,             -- surface that issued it
  created_at  TEXT NOT NULL,
  consumed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX ix_run_commands_pending ON run_commands(run_id, consumed_at);

CREATE TABLE approvals (
  approval_id TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL,
  description TEXT NOT NULL,
  risk_level  TEXT NOT NULL DEFAULT '',
  preview     TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL,             -- pending | allowed | denied | expired
  created_at  TEXT NOT NULL,
  decided_at  TEXT NOT NULL DEFAULT '',
  decided_by  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
```

---

## 6. The write path, and its ordering rule

**Rule: durable file first, catalog second. Never the reverse.**

```
append messages.jsonl / activity.jsonl      (fsync-ordered, truth)
        ↓
UPSERT into shamsu.db                        (projection)
```

If a process dies between the two, the catalog is **stale**, never **wrong** —
it can only be missing the tail. The reverse ordering could show the portal a
message that is not durable, which is the failure mode invariant 4 ("honest
failure over fabrication") exists to prevent.

**Reconciliation.** Each session row carries `indexed_line_count`. On open, on
`reindex`, and on a background sweep every 60 s, compare it with the actual line
count of `messages.jsonl`; if the file is longer, ingest the tail. This makes
crash recovery automatic and makes an externally edited transcript
self-healing — the exact scenario that previously failed silently.

**`shamsu reindex [--workspace W | --all]`** drops and rebuilds the catalog from
Tier 1. Because the catalog is derived, this is always safe. It is also the
migration path (§8) and the corruption remedy.

---

## 7. Fixing §2.1 and §2.3 with the same mechanism

### 7.1 The per-event index rewrite

`_upsert_index()` stops being the hot path. `SessionLogger.log()` writes its
JSONL lines (unchanged) and does one SQLite UPSERT. `index.json` is **kept and
still written**, but demoted to a debounced snapshot (at session create, close,
rename, and at most once every 10 s) so existing readers and any external
tooling keep working.

Effect on the measured case: 225 events × (122 KB read + 122 KB write + global
lock) becomes 225 × (one indexed UPSERT) + ~a handful of snapshot writes.

### 7.2 Cross-process run control — and a bug this deletes

`ControlledRun` stays in memory in the process that owns the run; the **catalog
becomes the registry**:

- Starting a run: `INSERT INTO runs(..., owner_pid, heartbeat_at)`.
- Owning loop bumps `heartbeat_at` every 5 s and, in the same tick, `SELECT`s
  pending `run_commands` for its run and applies them **on its own loop**.
- Any surface issuing `/stop`, `pause`, or feedback just `INSERT`s a row.
- `active_runs_for_session()` becomes a query, so every process sees the truth.
- A run whose `heartbeat_at` is >30 s stale is marked `orphaned` by any reader.
  That is a real improvement over the current 300 s staleness window in
  `claimed_by_other_live_process()` ([manager.py:481](shamsu/session/manager.py#L481)),
  and it means a crashed REPL no longer leaves a session looking busy forever.

**This deletes the known defect in `TELEGRAM_BOT.md` §3.8** — `cancel_run` being
called from the poll thread into `asyncio` primitives owned by a different loop,
which is why `/pause` and `/cancel` are unreliable today. With a command table
there is no cross-thread signalling at all: one process writes a row, the other
polls it on the loop that owns the run. That is a strictly better fix than the
`call_soon_threadsafe` patch I proposed as P7 in the main PRD, and it removes
that phase.

Approvals work identically, which is what makes "approve on the phone, watch it
resume in the browser" (`PRD_REMOTE_UX.md` §8.7 criterion 4) actually true
across processes rather than only within one.

### 7.3 What this unlocks

With run state shared, `shamsu serve` becomes viable: a single headless process
owning the web portal **and** the Telegram poller, able to start runs with no
REPL open. That was listed as an out-of-scope follow-up in `PRD_REMOTE_UX.md`
§7.3; the control plane is the thing that makes it a small addition rather than
a redesign. Still not proposed for v1 — but the `FileLock` poller election and
this table are the same idea, and they should not be designed twice.

---

## 8. Migration

No data is at risk, because Tier 1 is untouched and Tier 2 is derived.

1. Ship the catalog write path **alongside** the existing `index.json` writes
   (dual-write). Nothing reads the catalog yet.
2. `shamsu reindex --all` walks the workspace registry and backfills. On the
   measured workspace: 127 sessions, 7,921 events, 2 MB of transcript — seconds,
   not minutes.
3. Switch readers over: portal first (new surface, no risk), then
   `/sessions list`, then `search_sessions`.
4. Keep `index.json` written as a debounced snapshot indefinitely. It is cheap
   insurance and keeps the "you can read the state with `cat`" property.

Rollback at any step is deleting `~/.shamsu/shamsu.db`.

---

## 9. The isolation trade-off — stated plainly

`TELEGRAM_BOT.md` §4.1 documents a real, currently-enforced property: two
workspaces cannot see each other's sessions, and that isolation is enforced by
`Sandbox`, not by convention.

**A central catalog weakens that**, and it does so deliberately, because "all
workspaces viewable from one portal" is the requirement. What changes and what
does not:

| Property | Before | After |
|---|---|---|
| Session *metadata and transcript text* readable across workspaces | No | **Yes** — via the catalog |
| *File* access across workspaces | No | **No** — `Sandbox` is unchanged and still per-active-workspace |
| A run in workspace A writing to workspace B | No | **No** — run rows carry `workspace_id`; the executing loop is still sandboxed |
| Workspace appears in the catalog at all | n/a | **Only if registered** (`PRD_REMOTE_UX.md` §7.2), which is opt-in |

So the boundary that moves is *readability of your own chat history across your
own projects*, on a machine you already control. The boundary that does **not**
move is the one that matters: what the agent can write.

Two mitigations worth building anyway:

- `~/.shamsu/shamsu.db` gets `0600` (best-effort ACL on Windows), same treatment
  as the token file in `PRD_REMOTE_UX.md` §6.
- A per-workspace `catalog: false` opt-out in the registry, for a project you
  want kept out of the shared index entirely. Cheap to honour at write time.

---

## 10. Revised delivery plan

This supersedes `PRD_REMOTE_UX.md` §11. Changes: a new **PD** phase, P4/P5 now
depend on it, and old P7 is deleted (absorbed by §7.2).

| Phase | Content | Est. |
|---|---|---|
| **P0** | `TurnEvent` + `TurnStream` + `activity.jsonl`; `SimpleChatLoop.emit`; CLI renderer on it | 1 d |
| **P1** | Telegram live turn card. **Delivers G1+G2** — still the smallest slice that answers the original complaint | 1.5 d |
| **P2** | Install-bound token + state migration. **Delivers G3** | 0.5 d |
| **PD-a** | Catalog schema, `SqliteCatalogStore`, dual-write, `shamsu reindex`, reconciliation sweep | 1.5 d |
| **PD-b** | Control plane: `runs` + `run_commands` + `approvals`; `run_control` reads the DB; heartbeat + orphan detection; §3.8 defect closed | 1.5 d |
| **P3** | Workspace registry, poller lock, gateway map, `/projects` `/use` `/where` | 1.5 d |
| **P4** | `webui` server, SSE, JSON API, static shell — now reading the catalog, so cross-workspace listing and search are one query each | 1.5 d |
| **P5** | Web live turns, prompt input, approvals, diffs. **Delivers G5** | 1.5 d |
| **P6** | Polish (`PRD_REMOTE_UX.md` §9 items 1–5), verbosity, `/settings` | 1 d |

**Ordering that matters:**

- P0→P1 is unchanged and still independently shippable. **If you only have two
  days, this is still the answer** — none of the database work is needed for
  Telegram parity.
- **PD-b is a hard prerequisite for P4/P5** if the portal is to run in its own
  process. If we accept the in-REPL-thread portal from `PRD_REMOTE_UX.md` §8.2
  as a v1 limitation, PD-b can slip after P5 — but then the portal cannot
  outlive the REPL, and I would not recommend shipping it that way given the
  stated goal of *running* work from the portal.
- PD-a is worth doing on its own merits even if the portal is cancelled, purely
  to fix §2.1.

---

## 11. Test plan additions

- **Crash consistency.** Kill the process between the JSONL append and the
  catalog UPSERT; assert `reindex`/reconciliation recovers exactly the missing
  tail, and that no message is duplicated.
- **Concurrent writers.** Three processes appending to three sessions in one
  workspace for 30 s; assert no lost updates, no `database is locked` escapes,
  and catalog counts equal file line counts.
- **Reindex is a fixpoint.** `reindex` twice → byte-identical catalog contents.
- **Reindex is lossless.** Delete the DB, rebuild, assert every session, message
  and turn is recovered from Tier 1.
- **Externally reformatted transcript.** Pretty-print a `messages.jsonl` (the
  real 2026-08-18 incident); assert reconciliation detects the mismatch and
  re-ingests rather than silently under-reporting.
- **Orphan detection.** Start a run, `SIGKILL` the owner; assert the run flips to
  `orphaned` within 30 s and the portal stops showing it as running.
- **Cross-process control.** Process A owns a run; process B inserts `cancel`;
  assert A stops within one poll interval. This is the executable form of the
  §3.8 fix.
- **Isolation.** With workspaces A and B both in the catalog, assert a tool call
  in an A-run naming a B path is still rejected by `Sandbox`.
- **Regression.** The measured workspace (127 sessions, 122 KB index) must list
  in the portal in <100 ms.

---

## 12. Decisions, and what would change them

1. **SQLite, not a server database.** Changes only if SHAMSU goes multi-user or
   hosted — both explicit non-goals. The `CatalogStore` protocol is the hedge.
2. **Files stay the system of record; the catalog is derived.** This is the
   decision I would defend hardest. It preserves the lossless-transcript
   property, makes corruption a non-event, and means every schema mistake we
   make is recoverable by `reindex`.
3. **Content duplicated into the catalog rather than referenced by offset.**
   ~2 MB per workspace to remove an entire class of desync bug with prior art in
   this repo.
4. **Control plane in the same DB as the catalog.** They have different lifetimes
   (one disposable, one live), which argues for two files. One file wins on
   atomicity: a run and its session move together in a single transaction. If
   `reindex` ever needs to nuke the catalog, it truncates catalog tables only.
5. **The isolation weakening is accepted and scoped** (§9): metadata and
   transcripts become cross-readable; file-write authority does not.

---

## 13. Open questions

1. **Does the portal need to outlive the REPL?** This is the one that decides
   whether PD-b is a prerequisite or a follow-up. My reading of "we'll be
   running stuff from the web portal" is **yes** — recommend PD-b before P4.
2. **Retention.** Catalog rows for sessions in workspaces that no longer exist:
   keep (`present=0`) or prune? Recommend keep — the transcripts may still be on
   a drive that is merely unmounted, and pruning is unrecoverable.
3. **Should `activity.jsonl` events land in the catalog too?** They are the
   highest-volume writes by far. Recommend **no** — keep them as files, with
   only a per-turn summary in `turns`. Revisit only if the portal needs
   cross-session activity search, which nothing has asked for.
4. **One catalog per install, or one per user profile on a shared machine?**
   `~/.shamsu/` is already per-user, so this resolves itself — flagging it only
   because a shared build machine would need `SHAMSU_HOME` honoured, which is
   worth checking is respected consistently.
