# SHAMSU v2 — Migration Status

Tracks progress of the rebuild described in
[`docs/migration/v2-full-rebuild-plan.md`](docs/migration/v2-full-rebuild-plan.md).

**Branch:** `shamsu-v2.0.0` · **Legacy baseline tag:** `shamsu-v1-legacy-baseline`

---

## Milestones

| # | Milestone | Status | Exit condition |
|---|---|---|---|
| 1 | Repository reset | 🟢 Done | V2 tests run without importing legacy agent code |
| 2 | Runtime foundation | 🟢 Done | Simulated runs pause, resume, cancel, reject invalid transitions |
| 3 | Artifact foundation | 🟢 Done | Artifacts regenerate correctly after source changes |
| 4 | Read-only agent | ⚪ Not started | Grounded plans produced without modifying files |
| 5 | Controlled editing | ⚪ Not started | Simple changes completed with verified evidence |
| 6 | Structured planning | ⚪ Not started | Bounded multi-file tasks completed step-by-step |
| 7 | Repair | ⚪ Not started | Simple failures fixed without uncontrolled edits |
| 8 | Code intelligence | ⚪ Not started | Retrieval evals show accurate code selection |
| 9 | Project memory | ⚪ Not started | Memory improves success without more stale-context errors |
| 10 | Packages and documentation | ⚪ Not started | — |
| 11 | Docker | ⚪ Not started | — |
| 12 | Databases | ⚪ Not started | — |
| 13 | PRD workflows | ⚪ Not started | — |
| 14 | Advanced projects | ⚪ Not started | — |
| 15 | Tiny OS support | ⚪ Not started | — |

Legend: 🟢 done · 🟡 in progress · ⚪ not started · 🔴 blocked

---

## Pull request sequence

| PR | Title | Status |
|---|---|---|
| 1 | Archive legacy code | 🟢 Done |
| 2 | V2 package skeleton | 🟢 Done |
| 3 | State and persistence | 🟢 Done |
| 4 | Run control | 🟢 Done |
| 5 | Artifact registry | 🟢 Done |
| 6 | Repository artifacts | 🟢 Done |
| 7 | Tool contracts and policy | ⚪ Not started |
| 8 | Read-only agent | ⚪ Not started |
| 9 | Controlled authoring | ⚪ Not started |
| 10 | Planning contracts | ⚪ Not started |
| 11 | Completion gate | ⚪ Not started |
| 12 | Repair | ⚪ Not started |
| 13 | Structural code intelligence | ⚪ Not started |
| 14 | Lightweight project memory | ⚪ Not started |
| 15 | Legacy utility migration | ⚪ Not started |

---

## PR 1 — Archive legacy code ✅

Completed:

- [x] Branch `shamsu-v2.0.0` created from `mayday-lastresort` @ `b64780e`
- [x] Annotated tag `shamsu-v1-legacy-baseline` created
- [x] v1 implementation moved to `legacy-code/` (522 paths, one commit, `bb84e2f`)
- [x] `legacy-code/LEGACY_README.md` written
- [x] `MIGRATION_STATUS.md` (this file) added
- [x] CI separated — `legacy-ci.yml` scoped to `legacy-code/**`, non-gating

- [x] **Legacy baseline established** — 2362 collected, **2339 passed, 17
      failed, 6 skipped, 0 collection errors**. Recorded in
      `legacy-code/LEGACY_README.md`.

      The earlier blocker (74 collection errors from a missing `mcp`, and no
      `python3-venv` to build an isolated environment) was solved with
      `pip install --target` + `PYTHONPATH`, which sidesteps PEP 668 without
      touching the system environment.

      **Caveat that limits what this number means:** `ollama` is not installed
      on this machine, and v1 is a local-first agent. Eight of the 17 failures
      show model-dependency directly in their output. **17 is an upper bound on
      v1's defect count, not a measurement of it.** Re-run on a GPU machine to
      separate genuine regressions from environmental ones.

---

## PR 2 — V2 package skeleton ✅

- [x] `src/shamsu/` layout, thirteen subpackages
- [x] Interface layer: `enums`, `ids`, `cancellation`, `tools`, `artifacts`,
      `models`, `code_intelligence`, `context`
- [x] `scripts/check_import_boundary.py` + CI gate
- [x] `.github/workflows/ci.yml` — boundary, lint, format, mypy, tests
- [x] Deterministic `FakeModelClient`; no test contacts a model

- [x] **mypy strict passes.** First real run happened at commit `7ef4664`
      (mypy 2.3.0, installed via `pip install --target`). Seven errors, all
      genuine; fixed. `src/` now carries **zero** `type: ignore` comments —
      the twelve that existed were hiding weak typing (`object` parameters on
      database rows), not working around checker limitations.

## PR 3 — State and persistence ✅

- [x] Typed records — project, run, task, plan, plan step, tool event,
      evidence, approval, checkpoint, failure. All frozen.
- [x] SQLite schema with enforced foreign keys, WAL, and `user_version`
      migrations that refuse a database from a newer build
- [x] `StateStore` with atomic plan+steps insertion and checkpoint resume
- [x] Transition table as data, with reachability and totality tests
- [x] `advance_task` validates every state change; illegal moves write nothing

Two guarantees now hold at the storage layer:

1. **Transitions cannot be bypassed** — `advance_task` consults the table and
   raises `InvalidTransition` without writing.
2. **Evidence cannot be forged** — `evidence.source_event_id` is a non-null
   foreign key to `tool_events`, so evidence cannot exist without an observed
   tool execution. There is no path from a model assertion to a row.

## PR 4 — Run control ✅

- [x] `RunToken` — cancellation observable by polling from any thread and by
      `await` on the event loop, so a long call can be *raced* against
      cancellation rather than only checked between calls
- [x] `RunController` — registration, status, cancel, pause/resume, feedback
      injection, wall-clock enforcement, event log
- [x] `ExecutionLimits` — plan §11's bounds in one frozen object
- [x] `RunEvent` / `EventKind` — the observable run timeline

**Milestone 2's exit condition is met:** simulated runs pause, resume, cancel,
and reject invalid transitions, all under test.

### The v1 defect is closed

v1's `runtime/run_control.py` implemented the entire control plane and the live
loop never imported it — `cancel_run` could be called and nothing happened. The
fix is structural rather than a better implementation: the controller owns the
token, and the token is a **required parameter** on every blocking call. A
component cannot forget to observe cancellation, because it cannot block
without being handed the thing that reports it.

`CANCELLING` and `CANCELLED` are separate statuses. The first means the request
was delivered; the second means the run acknowledged it. Collapsing them would
let status claim a stop that has not happened.

### Bug found and fixed during this PR

`cancel()` advertised thread safety but wrote run status through a SQLite
connection, which is thread-bound by default. Cancelling from a signal handler
raised `ProgrammingError` *after* setting the token, leaving the token
cancelled and the database still claiming `RUNNING`.

Fixed by making `StateStore` genuinely thread-safe (`check_same_thread=False`
plus a re-entrant lock on every method touching the connection), and by having
`wait_if_paused` race the resume gate against the token instead of `cancel()`
reaching into an asyncio primitive from another thread. Regression tests were
confirmed to fail without the fix.

## PR 5 — Artifact registry ✅

- [x] Content hashing (not timestamps — `touch` moves mtime, not content)
- [x] Git-aware scanning: `ls-files --cached --others --exclude-standard`
- [x] `ArtifactRegistry` — content on disk, freshness in SQLite
- [x] Invalidation by source hash, by path, by generator version, by contradiction
- [x] `usable()` gate so INVALIDATED/MISSING/FAILED cannot reach the model
- [x] Schema migration 2

Using git's view rather than a hand-maintained ignore list is the difference
between scanning 894 files of this repository and scanning the 64 that are
actually v2 — the archived v1 tree and the vendored SmallCTL checkout are both
correctly excluded.

## PR 6 — Repository artifacts ✅

- [x] Deterministic Python extraction via stdlib `ast` (no new dependencies)
- [x] Repository manifest (§15.1), repository map (§15.2), module cards
      (§15.3), symbol cards (§15.4)
- [x] `ArtifactRefresher` — scan → recompute → retire → regenerate → report
- [x] Reverse import edges (real, computed from parsed imports)

**Milestone 3's exit condition is met.** On this repository a full pass builds
220 artifacts from 70 files; a second pass does no work; editing a file
regenerates exactly its dependents; deleting one retires its card.

### Honesty over completeness

Callers, callees, and measured coverage need the reference graph from Milestone
8. Cards say **"Not yet computed"** rather than leaving those sections blank —
a blank "Callers" heading reads as *nothing calls this*, which is a structural
claim nothing has earned. Related tests are labelled as matched by filename
convention, not measured coverage.

### Bugs found and fixed during these PRs

1. **Repository-wide artifacts did not track add/delete.** The manifest reports
   a file count and directory list, but declared only `pyproject.toml` as a
   source — so deleting a module left it FRESH and wrong. Fixed with a
   synthetic `<repository:file-list>` source whose hash covers the set of
   indexed paths.
2. **`src/` was stripped unconditionally when deriving module paths.** Correct
   for a src-layout project, wrong when `src/__init__.py` exists — and it
   silently broke every import edge in that case. The context now detects which
   packaging roots actually apply.
3. **A retired card was invalidated but not reported as retired**, because
   hash-based recomputation had already invalidated it. The report now
   describes what happened to the artifact, not which code path got there first.

---

## Standing constraints

These hold for every subsequent PR:

1. No production import from `legacy-code/` — enforced in CI.
2. New development only under `src/shamsu/`.
3. SQLite is authoritative for runtime state.
4. Graphiti is **not** on the critical path; reconsideration is gated by plan §33.
5. Completion requires verified evidence (`required_evidence ⊆ verified_evidence`).
6. Long-running autonomy stays disabled until evaluations justify it.

## Legacy component migrations

Tracked separately in [`LEGACY_COMPONENTS.md`](LEGACY_COMPONENTS.md).
