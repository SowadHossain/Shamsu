# SHAMSU v2 — Migration Status

Tracks progress of the rebuild described in
[`docs/migration/v2-full-rebuild-plan.md`](docs/migration/v2-full-rebuild-plan.md).

**Branch:** `shamsu-v2.0.0` · **Legacy baseline tag:** `shamsu-v1-legacy-baseline`

---

## Milestones

| # | Milestone | Status | Exit condition |
|---|---|---|---|
| 1 | Repository reset | 🟢 Done | V2 tests run without importing legacy agent code |
| 2 | Runtime foundation | 🟡 In progress | Simulated runs pause, resume, cancel, reject invalid transitions |
| 3 | Artifact foundation | ⚪ Not started | Artifacts regenerate correctly after source changes |
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
| 4 | Run control | ⚪ Not started |
| 5 | Artifact registry | ⚪ Not started |
| 6 | Repository artifacts | ⚪ Not started |
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

Carried forward as an open item:

- [ ] **Legacy baseline test results are unrecorded.** The v1 suite could not be
      executed on the rebuild machine (74 collection errors —
      `ModuleNotFoundError: No module named 'mcp'`; no `python3-venv`). This is
      an environment limitation, not a v1 regression. Re-run on a fully
      provisioned machine and record results in
      `legacy-code/LEGACY_README.md`. Until then there is no numeric baseline to
      compare v2 evaluation results against.

---

## PR 2 — V2 package skeleton ✅

- [x] `src/shamsu/` layout, thirteen subpackages
- [x] Interface layer: `enums`, `ids`, `cancellation`, `tools`, `artifacts`,
      `models`, `code_intelligence`, `context`
- [x] `scripts/check_import_boundary.py` + CI gate
- [x] `.github/workflows/ci.yml` — boundary, lint, format, mypy, tests
- [x] Deterministic `FakeModelClient`; no test contacts a model

Open item:

- [ ] **mypy has never actually run.** It is configured (strict, with the
      pydantic plugin) but is not installed in the rebuild environment. First
      real execution will be in CI; expect to fix annotations on that pass.

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

Not yet built (PR 4): the run controller that drives these records — process
registration, live cancellation delivery, pause/resume, feedback injection,
wall-clock enforcement.

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
