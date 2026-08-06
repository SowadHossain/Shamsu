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
| 7 | Repair | 🟢 Done | Simple failures fixed without uncontrolled edits |
| 8 | Code intelligence | 🟢 Done | Retrieval evals show accurate code selection |
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
| 12 | Repair | 🟢 Done |
| 13 | Structural code intelligence | 🟢 Done |
| 14 | Lightweight project memory | 🟢 Done |
| 15 | Legacy utility migration | 🟢 Done |

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

## PR 12 — Repair ✅

- [x] `verification/failure` — failure classification and the failure capsule
- [x] `agent/repair` — `RepairScope`, `RepairController`, bounded attempts
- [x] `WriteScope` + `Tool.write_targets` + `ToolGateway.restricted_to`
- [x] Same-failure stopping, resumable across a fresh controller
- [x] `store.failures_for` — per-step repair history

**Milestone 7's exit condition is met:** a broken `add()` is repaired from a
real pytest failure, with `unrelated.py` and the test file both refused by the
gateway, and a repair that changes nothing stops instead of spending its budget.

### Design points worth keeping

- **The write scope is enforced by the gateway, not the repair controller.** A
  restriction that lives in the caller is one a different caller does not have.
  `Tool.write_targets` puts the knowledge of what a call writes in the tool,
  because a gateway that scanned arguments for something called `path` would be
  wrong the first time a tool named it differently — and wrong silently.
- **Test files are protected by default.** Editing the failing test is
  indistinguishable from deleting the evidence, and it is the most attractive
  wrong move available. `allow_test_edits` is the caller's decision, never the
  model's.
- **Traceback frames alone cannot scope a repair.** When a test fails on an
  assertion the buggy function *returned normally*, so no frame names it — the
  only file in the traceback is the test. The scope is therefore frames ∪ files
  the step changed ∪ files the step declared, all recorded before the failure.
- **A refused write does not spend the mutation budget.** Otherwise one
  out-of-scope attempt costs the decision its only edit.
- **Same-failure detection is rebuilt from persisted failures each call.** An
  in-memory counter would hand a stuck step its whole budget again after a
  resume.
- **Classification is patterns over real output, never a model.** The
  fall-through is documented rather than clever: unmatched `test.run` output is
  a test failure, anything else is a tool failure.

### Bug found and fixed during this PR

`test.run` could report a *fixed* bug as still broken. CPython validates cached
bytecode against the source's (mtime in whole seconds, size), and an agent
patching a file frequently changes neither — `return a - b` → `return a + b` is
the same size, and a repair lands within a second of the run that motivated it.
Python then executed the previous bytecode. The agent would repair a bug that
no longer existed until same-failure detection blocked a task that was already
done. Each run now gets a fresh `PYTHONPYCACHEPREFIX`, which also keeps
`__pycache__` out of the workspace so checkpoint diffs show only real changes.
Both regression tests were confirmed to fail without the fix.

---

## PR 13 — Structural code intelligence ✅

- [x] `code_intelligence/index` — `PythonCodeIndex`, satisfying the `CodeIndex` protocol
- [x] Symbol index, reference graph, callers, callees
- [x] Related tests — import edge, package import + used name, then convention
- [x] `impact()` — bounded traversal that reports its own truncation
- [x] `code_intelligence/retrieval` — the ordered pipeline, semantic last
- [x] `python_source` — call and reference edges, attributed to the innermost symbol
- [x] `tests/evals/test_retrieval_accuracy.py` — scored against this repository

**Milestone 8's exit condition is met:** the retrieval evaluation scores
**precision@1 = 81%** over 16 queries spanning identifiers, qualified names,
paths, and literals, with a 75% regression threshold asserted in CI. Every
ground-truth related-test pair resolves.

### Deviation from the plan: no tree-sitter

The plan says "Add Tree-sitter". This ships on stdlib `ast` instead, and that
is a considered choice rather than a shortcut:

- For Python, `ast` is the language's own parser — exact where tree-sitter is
  approximate.
- It costs no dependency and no bundled grammar, and v2 adds dependencies only
  when a milestone needs them.
- `CodeIndex` is the seam a tree-sitter backend arrives through when other
  languages need one. Nothing here has to change for that.

Tree-sitter is deferred, not rejected. It becomes necessary the first time
SHAMSU must index a repository it cannot parse.

### Design points worth keeping

- **The retrieval order is the product.** Any backend can be swapped; running
  them in the wrong order cannot be fixed by improving any of them.
- **Identifier queries are routed to the symbol stage ahead of text.** Plan §18
  puts text at stage 2, which is right for a literal. For a bare identifier,
  text search returns the definition *and* every import, call site, and mention,
  and first-non-empty-wins then hands back the noisy set. Both behaviours are
  asserted.
- **Name-based, and it says so.** Python binding is not statically decidable,
  so references and callers over-approximate. That is the safe direction for
  scoping a change, but `provenance` and `truncated` exist so no caller mistakes
  it for proof.
- **`is_ready()` re-scans rather than trusting a marker.** v1 gated on a marker
  file that could disagree with the index, so the agent silently answered from a
  stale one. A stale index is reported on every retrieval result.
- **A broken semantic backend degrades to no hits.** A fallback that can fail
  the task is not a fallback.
- **Related tests use three rules, strongest first.** The middle one matters:
  `from shamsu.verification import digest_test_output` never names `digest.py`,
  and a rule matching only full module paths would call that file untested.
- **`related_files_for` is available but deliberately not wired into repair.**
  Widening a write scope is a safety change and should land on the strength of
  evaluations, not on the strength of being possible.

### Bug found and fixed during this PR

`ast.walk` yields a `Call` *and* the `Name` inside its `func`, so every call was
recorded twice — once as a call, once as a plain use. Caught by a test asserting
`is_call` on every reference to a called name. One occurrence is now one
reference; otherwise `is_call` means nothing and every consumer has to
deduplicate.

---

## PR 14 — Lightweight project memory ✅

- [x] Schema migration 3 — `project_facts`, `architecture_decisions`, `memory_records`
- [x] `memory/records` — typed facts, ADRs, and failure lessons
- [x] `memory/store` — learn, confirm, contradict, revalidate, recall
- [x] Confidence derived from origin, moved only by evidence
- [x] Staleness from content hashes; context invalidation via `revalidate`
- [x] Failure lessons keyed by error signature, wired into `RepairController`

**Milestone 9's exit condition** has two halves and the second is the hard one:
memory must improve task success **without increasing stale-context errors**.
Both are covered — a prior task's fix reaches the next task's failure capsule,
and no fact whose evidence changed is ever stated as current.

### Design points worth keeping

- **Confidence is derived, never declared.** It starts from *how* the fact was
  learned and moves only on evidence. `learn()` has no `confidence` parameter,
  and a test asserts that it does not.
- **An OBSERVED fact needs its tool event.** Without the event id, "observed" is
  an assertion wearing a better label, so it raises. Same rule as evidence, and
  the foreign key enforces it in the schema too.
- **A weaker origin may confirm but may not overwrite.** A model asserting
  something cannot replace what a tool observed, however confidently phrased.
  The disagreement is still recorded and still costs confidence.
- **Statement comparison is exact, not fuzzy.** A similarity threshold would
  eventually treat "uses pytest" and "does not use pytest" as agreement, and the
  contradiction path exists precisely to catch that.
- **Facts go stale; decisions do not.** A file changing invalidates a fact
  learned from it. It must never invalidate an ADR — a decision that was made
  stays made, even when the code it produced has since been rewritten. That is
  why these are separate tables rather than one with a `kind` column.
- **A stale fact is kept, marked, and outranked.** Deleting would lose a claim
  that is probably still true; leaving it unmarked is the entire stale-context
  failure mode. `recall` sorts verified facts ahead of stale ones so a trusted
  current fact is never evicted by a stale one that once scored higher.
- **A deleted file invalidates.** A missing path hashes as `<missing>` rather
  than being skipped — skipping would make deletion the one change memory never
  notices.
- **Only a resolution crosses tasks.** "This failed before" without a fix is
  noise in a repair frame; the capsule already says the failure is happening.

---

## PR 15 — Legacy utility migration ✅

- [x] `models/normalization` — the selected parser, rewritten as a deletion
- [x] `security/secrets` — redaction, migrated verbatim
- [x] `security/commands` — command risk, rewritten with a stricter default
- [x] `telemetry/metrics` — plan §31 metrics, computed from rows
- [x] [`LEGACY_COMPONENTS.md`](LEGACY_COMPONENTS.md) — every §8.3 candidate resolved

Four components crossed the boundary. Seven more on the candidate list were
written fresh, and the ledger says which is which — "we rewrote it" and "we
never looked" are different claims and both are now on the record. Nothing on
the §8.3 list is still open.

### Design points worth keeping

- **The parser migration is mostly a deletion.** v1's `output.py` is 1,159
  lines with six salvage strategies and greedy quote repair. What crossed is
  ~200 lines and one hard-won behaviour: the balanced-brace scanner restarts
  past an unterminated brace, because a single scan once lost a valid tool call
  behind a truncated Python fence.
- **Normalisation removes wrapping and never edits content.** Stripping a
  `<think>` span or a fence has exactly one correct result. Repairing an
  unescaped quote is a guess, and a wrong guess produces a *parseable* wrong
  answer — worse than a parse failure, because the failure is visible and a
  silently wrong `file.patch` argument is not.
- **Redaction was copied verbatim, and that is the right call.** The patterns
  are the residue of real leaks and v1's test passes against them. A rewrite
  would swap evidence for fresh guesses about what a secret looks like.
- **Unknown commands are HIGH, not MEDIUM.** v1 defaulted unknown to the same
  level as `pip install`, so nothing above could tell them apart.
- **Metrics are queries, not counters.** v1 incremented at the site that
  believed it had succeeded, so `false_success_rate` measured whether the loop
  had noticed its own mistake — zero exactly when things are worst. Every metric
  here is a query over `tasks`, `evidence`, `tool_events`, and `failures`, and
  `_evidence_holds` re-derives the gate result rather than reading a stored
  verdict.
- **`test_a_bypassed_gate_is_caught` writes a completed task with no evidence
  straight into the database.** `CompletionGate` cannot produce that state, and
  a metric that could never report it would be measuring the runtime's opinion
  of itself.

### Defect found and fixed during migration

v1's blocked-command pattern `r"sudo"` was unanchored, so `python sudoku.py`
classified as BLOCKED. Safe in direction, but a rule that fires on nonsense is
a rule someone eventually relaxes. Now `\bsudo\b`, with a test.

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
