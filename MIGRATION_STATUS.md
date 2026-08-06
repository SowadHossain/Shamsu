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
| 4 | Read-only agent | 🟢 Done | Grounded plans produced without modifying files |
| 5 | Controlled editing | 🟢 Done | Simple changes completed with verified evidence |
| 6 | Structured planning | 🟢 Done | Bounded multi-file tasks completed step-by-step |
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
| 7 | Tool contracts and policy | 🟢 Done |
| 8 | Read-only agent | 🟢 Done |
| 9 | Controlled authoring | 🟢 Done |
| 10 | Planning contracts | 🟢 Done |
| 11 | Completion gate | 🟢 Done |
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

- [x] **Legacy baseline established and triaged** — 2362 collected,
      **2349 passed, 7 failed, 6 skipped, 0 collection errors**. Full
      breakdown in `legacy-code/LEGACY_README.md`.

      The original blocker (74 collection errors from a missing `mcp`, no
      `python3-venv`) was solved with `pip install --target` + `PYTHONPATH`,
      which sidesteps PEP 668 without touching the system environment.

      A first run reported 17 failures. Ten were artefacts of how the suite
      was being run: this machine has **no `python`, only `python3`**, so every
      test shelling out to `python -c` silently produced no output. Notably
      that included `test_command_output_secrets_are_redacted` — **secret
      redaction works correctly.**

      Of the 7 real failures: **5 environmental** (3 need a local model, 1
      needs `python3-venv`, 1 needs playwright), **1 stale test** whose named
      safety property still holds, and **1 genuine v1 bug** — `_FILE_TOKEN_RE`
      cannot match a POSIX absolute path, making the contract layer's
      workspace-relative normalization dead code on Linux and macOS. Written
      up in `LEGACY_COMPONENTS.md` so v2 does not inherit it.

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

## PR 7 — Tool contracts and policy ✅

- [x] `PathSandbox` — resolve before deciding, follow symlinks, accept
      absolute paths that land inside the workspace
- [x] Typed `Tool` base; the model-facing JSON schema is *derived* from the
      input model, so what the model is shown and what it is held to cannot
      drift
- [x] `ToolGateway` — phase allowlist, approval, argument validation,
      one-mutation-per-decision, timeout racing cancellation, output capping
- [x] 29-test adversarial path-escape suite

Default approval policy is `deny_all`. An unconfigured gateway that approves
everything is decoration, not policy.

### Bug found during this PR

`NullCancellationToken.wait_cancelled()` raised `NotImplementedError`, on the
reasoning that hanging forever is worse than a clear error. That was wrong: the
method exists to be *raced* against real work, and a raising implementation
completes instantly — which the gateway read as "cancelled", turning every
tool timeout into a spurious cancellation. It now never resolves, and the
gateway asks the token directly rather than inferring from watcher completion.

## PR 8 — Read-only agent ✅

- [x] `project.inspect`, `code.search`, `file.read` — logical tools, not
      syscall wrappers
- [x] `ContextCompiler` — priority budgeting, hot context never dropped,
      stale artifacts labelled
- [x] Output contracts (`InvestigationStep`, `ImplementationPlan`, …)
- [x] `ReadOnlyAgent` — bounded investigate loop, then plan
- [x] `is_grounded()` — a plan citing files the agent never read is rejected

**Milestone 4's exit condition is met:** the agent produces grounded
implementation plans without modifying files. Verified against this repository
and by a test that hashes the whole tree before and after a full investigation.

Read-only is enforced by *policy*, not by the agent behaving: the phase is
INSPECT throughout and the gateway only exposes tools declaring INSPECT.

## PR 9 — Controlled authoring ✅

- [x] `file.patch` — anchored replacement, reversible, ambiguity refused
- [x] `git.inspect` / `git.checkpoint` — one logical call each, fixed argv
- [x] `test.run` — allowlisted command *keys*, never a shell string
- [x] `verification/digest` — test-output digesting and stable error signatures
- [x] `verification/evidence` — `EvidenceRecorder` and the completion gate
- [x] Rollback: `PatchUndo` per edit, `git reset` for multi-file changes

**Milestone 5's exit condition is met:** a broken function is patched, the
tests are run, the diff is inspected, a checkpoint is committed, and the gate
only opens once all four pieces of evidence exist as rows keyed to real tool
executions.

### Design points worth keeping

- **Anchored edits, not line numbers.** Line numbers drift; an anchor that no
  longer matches simply fails, which is the honest outcome. An anchor matching
  more than once is refused rather than resolved by taking the first — "it
  edited the wrong one" is much worse than "it asked again".
- **A no-op patch reports failure.** Otherwise it would register
  `FILE_CHANGED` evidence for changing nothing.
- **`test.run` takes a command key, not a command line.** There is no string
  for a model to smuggle `; rm -rf /` into, because there is no string.
- **Whole-file overwrite needs an explicit acknowledgement.** v1 defaulted to
  it and lost work.
- **Error signatures ignore temp paths, durations, and line numbers**, so two
  attempts at the same failure sign identically and `RepairTracker` can tell
  grinding from progress.

---

## PR 10 — Planning contracts ✅

- [x] `agent/planning` — proposal → `plans` / `plan_steps` rows
- [x] Evidence vocabulary — prose requirements mapped onto `EvidenceKind`
- [x] Evidence floor — a change step's minimum, set by the runtime
- [x] Acceptance criteria — preserved, including phrases that mapped to nothing
- [x] Step gate — `required ⊆ verified`, per step, against evidence rows
- [x] Re-planning — versioned, superseding, bounded at 2 per task
- [x] `render_plan_summary` / `render_step` — the compact plan view (plan §21)

**Milestone 6's exit condition is met:** a two-step plan across two files runs
to completion step by step, with each step's gate opening only on evidence rows
scoped to that step.

### Design points worth keeping

- **A model may raise its own bar, never lower it.** `required_evidence` is the
  union of what the plan asked for and what the runtime demands. A change step
  always requires `FILE_CHANGED` and `GIT_DIFF_REVIEWED`, whatever the plan
  says.
- **The only discount on evidence costs the ability to write.** Declaring a
  step `investigate` removes its floor *and* every mutating tool from its
  allowlist. `change` is the default, so an omitted field lands on the stricter
  side.
- **Free-text evidence is mapped, not adopted.** "targeted authentication tests
  pass" becomes `TESTS_PASSED` by a runtime vocabulary. A phrase matching
  nothing is not guessed at — it survives as an acceptance criterion, where it
  is readable prose rather than a requirement nothing can satisfy.
- **A plan cannot pre-approve its own step.** `PlanStepProposal` has no
  approval field; `approval_required` is derived from risk by the runtime.
- **Paths are checked at plan time.** A step declaring it will edit
  `../../etc/passwd` is refused before any row is written, not three decisions
  later with the budget spent.
- **Re-planning supersedes; it never edits.** Completed work is not copied
  forward, because evidence rows key to the step that earned them and a copy
  under a new id would orphan the proof. Finished step *titles* cross the
  boundary so the next plan can be told what not to redo.
- **There is one path to `StepOutcome.PASS`, and it reads the evidence table.**
  `fail_step` refuses to write a passing outcome.

---

## PR 11 — Completion gate ✅

- [x] `CompletionClaim` — the shape a model uses to *propose* completion
- [x] `validate_claim` — named claims checked against rows; unknown names refused
- [x] Step gate — `required ⊆ verified`, scoped to the step that earned it
- [x] Final gate — every step passed, at its own scope, or the task is not done
- [x] `build_report` — the final report, derived from `tool_events` and `evidence`
- [x] `next_after_completion_gate` — where a refusal sends the run
- [x] `tests/adversarial/test_evidence_forgery.py` — the claim under attack

**Plan §20.7 is enforced structurally:** the model cannot set completion
directly. It proposes; the runtime decides from rows. No tool declares
`Phase.COMPLETE`, so the complete phase has an empty tool surface — nothing can
run there at all.

### Design points worth keeping

- **An unknown claim is refused, not defaulted.** `requirements_for` returns an
  empty set for an unrecognised name, and an empty requirement set is trivially
  satisfied — so `tests_pas` would otherwise sail through the check that exists
  to stop it.
- **The final gate is not the task-level union of evidence.** A four-step plan
  whose first step patched a file, ran tests, and reviewed a diff satisfies a
  union check outright, and the other three steps complete having done nothing.
  Each step is judged at its own scope instead.
- **Rows outrank the recorded outcome.** `StepOutcome.PASS` is a cached
  decision; the evidence table is the fact. A step marked passed whose evidence
  is missing does not complete the task.
- **`evidence_cited` is never consulted.** It exists so a refusal can be
  explained, not so a claim can be supported.
- **The report is derived, including its file list.** Changed files are read
  off successful `file.patch` executions, because a list the agent maintains is
  a list the agent can be wrong about. Failed calls are counted and reported.
- **A report for a nonexistent task raises.** A plausible-looking report for a
  run that never happened is the worst possible output.

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
