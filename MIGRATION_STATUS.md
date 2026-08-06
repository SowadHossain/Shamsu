# SHAMSU v2 — Migration Status

Tracks progress of the rebuild described in
[`docs/migration/v2-full-rebuild-plan.md`](docs/migration/v2-full-rebuild-plan.md).

**Branch:** `shamsu-v2.0.0` · **Legacy baseline tag:** `shamsu-v1-legacy-baseline`

---

## Milestones

| # | Milestone | Status | Exit condition |
|---|---|---|---|
| 1 | Repository reset | 🟢 Done | V2 tests run without importing legacy agent code |
| 2 | Runtime foundation | ⚪ Not started | Simulated runs pause, resume, cancel, reject invalid transitions |
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
| 2 | V2 package skeleton | ⚪ Not started |
| 3 | State and persistence | ⚪ Not started |
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
