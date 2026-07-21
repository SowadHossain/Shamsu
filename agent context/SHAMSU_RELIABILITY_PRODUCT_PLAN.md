# SHAMSU Reliability and Product Excellence Plan

Status: Implementation in progress.
Date: 2026-07-19
Target branch: `develop`, through reviewed feature/fix branches

Progress through 2026-07-20:

2026-07-21 dogfood remediation update:

- Read-only command safety, snapshot visibility, and no-write code-memory bootstrap are implemented and live-tested.
- Ambiguous edit recovery, failed-mutation retries, and malformed JSON apostrophe preservation are implemented and live-tested.
- Composite verification fallback, evidence-first output, stdout reporting, and recovered outcome semantics are implemented and live-tested.
- Dedicated scoped PRD builds now execute exact acceptance commands and deterministic Python function/CLI conformance checks with bounded validation-guided repair.
- Web requests now use search-shaped provider queries and preserve exact release/version semantics during evidence synthesis.
- Abstract/code memory refreshes after applied mutations; fresh generation parity was confirmed in dogfood runs.
- Current quality gate: 1510 passed, 2 skipped; compile and diff checks pass.
- Remaining priority: reduce generic composite latency and model-call count while retaining the new evidence gates.

- Recommended PR 2, the noninteractive full-request harness, is implemented.
- Work Package 2's canonical v2 run/logging contract is implemented and its
  exit criteria are covered by deterministic tests.
- Work Package 3's execution-truthfulness changes are implemented and passed
  deterministic and live `qa_probe.py` acceptance testing.
- Work Package 4's composite routing and clarification changes are implemented
  and passed deterministic and live compound-request acceptance testing.
- Work Package 5's shared workspace policy, abstract freshness, index isolation,
  and degraded retrieval changes are implemented and live-tested.
- Work Package 6's local-first memory queue, evidence-aware durable-memory
  policy, bounded shutdown flush, and compact session resume are implemented
  and live-tested.
- Work Package 7's PDF normalization, normalized full-stack contract,
  generation gates, TaskFlow Django generation, and ownership acceptance tests
  are implemented and live-tested.
- Work Package 8's web provider chain, external-request privacy boundary,
  capability status, browser evidence, and citation-source logging are
  implemented and live-tested.
- Current quality gate: 1400 passed, 2 skipped; Ruff passes.
- Work Package 9's semantic approvals, complete decision records, dry-run
  contract, and cancellation rollback are implemented and live-tested.
- Work Package 10's outcome classification, linked diagnostic artifacts,
  actionable-failure reuse, retention checks, and workspace-state doctor are
  implemented and live-tested.
- Work Package 11's CLI arguments, approval UI/policy, request lifecycle, and
  run-inspection commands are modularized behind compatibility exports.
- The next implementation target is Work Package 12, product polish and the
  release gate.

## 1. Purpose

This document is the implementation plan for turning SHAMSU from a collection
of strong local-agent subsystems into a dependable Claude Code/Codex-style
coding agent.

The target product must be able to:

- Answer questions about a local workspace accurately.
- Create projects from natural-language requests and PRDs.
- Read, create, edit, move, and delete project files safely.
- Diagnose and fix bugs, then verify the fix.
- Run approved commands and tests.
- Search the web and cite retrieved sources.
- Inspect local applications in a browser.
- Preserve useful conversation and project context across turns.
- Report exactly what it did, why it did it, and whether it succeeded.
- Run locally on low-resource machines without cloud AI APIs.

The central product requirement is trustworthiness:

> SHAMSU must never claim success unless the recorded tool results, filesystem
> state, and verification results support that claim.

## 2. Audited Baseline

The baseline was checked on 2026-07-19.

- Current branch: `fix/routing-planning-cot`.
- The branch is 20 commits ahead of `develop`.
- The working tree was clean before this planning document was added.
- Unit/integration suite: 1283 passed, 1 skipped, 1 collection warning.
- Ruff: all checks passed.
- Default-tier live eval: 11/12 at three samples per case.
- Light-tier live eval: 9/12 at three samples per case.
- Real REPL testing was performed in `F:\Work\PROJECTS\shamsu\test-shamsu`.
- The live QA findings are recorded in `test-shamsu/SHAMSU_QA_LOG.md`.

The written project state is stale in places:

- `agent context/PROGRESS.md` reports 481 tests from 2026-07-05.
- The root README still contains older capability and test-count statements.
- The reliability branch contains major work that is not yet in `develop`.

## 3. What Is Already Implemented

SHAMSU is not starting from zero. The following foundations exist and should
be preserved.

### 3.1 Runtime and installation

- Repo-local Python virtual environment.
- Windows and Bash install/run/uninstall scripts.
- User-local launcher support.
- Local-only Ollama runtime integration.
- Lazy model downloads and model-tier selection.
- Runtime doctor and guided repair.
- Low-resource model cookbook and single-model mode.

### 3.2 Workspace understanding

- Workspace sandbox and path validation.
- Recursive file discovery and ignore rules.
- SQLite FTS indexing and Python symbol extraction.
- Incremental indexing.
- BM25-style reranking and semantic retrieval fallback.
- Codebase-Memory MCP integration.
- Workspace file listing, reading, PRD discovery, and `@file` mentions.

### 3.3 Agent workflows

- Natural-language routing through one ordered route table.
- Stateful ReAct chat loop.
- Planner and plan mode.
- QA, code-edit, bug-fix, audit, test-generation, and documentation workflows.
- Clarification questions and pending-question resume.
- Cross-route conversation continuity.
- Move, delete, write, edit, read, search, command, web, and fetch tools.
- Optional autonomy and repetition protection.
- Selective model council for sensitive bug-fix work.

### 3.4 Safety and recovery

- Workspace-bound command execution.
- Command risk classification and blocked-command policy.
- Approval manager and remembered low-risk permissions.
- Patch validation and preview.
- Mutation transactions, backups, rollback, and `/undo`.
- Dirty-worktree warnings.
- Secret redaction helpers.

### 3.5 Verification and generation

- Lightweight and optional heavy verification.
- One bounded automatic repair after failed verification.
- Markdown/TXT/PDF PRD input.
- Deterministic PRD parsing and project classification.
- Django project planning and generation.
- Generated-project setup, migrations, tests, repair loop, and summaries.
- Registry templates and Definition-of-Done checks.
- Game scaffold and local dev-server preview path.

### 3.6 Observability and evaluation

- Workspace sessions and resume support.
- Session event logs and clean chat transcripts.
- ActionLedger run directories.
- Session audit logs, safety audit logs, diagnostics, mutation journals, and
  memory event logs.
- Deterministic routing tests.
- Live local-model eval harness with isolated workspaces and multi-sample
  scoring.
- 1283 passing automated tests.

## 4. Confirmed Product Defects

The following defects were observed in the real REPL, not inferred only from
source code.

### P0 - Trust and execution correctness

1. A bug-fix patch can fail after approval without recording the real apply
   exception.
2. A failed bug-fix run can be written to the ActionLedger as `success`.
3. A successful edit can end with a contradictory clarification message.
4. A final answer can be accepted without checking it against tool results,
   mutations, or verification.
5. The current live eval harness does not exercise the complete top-level REPL
   request path, so it misses routing, approval, ActionLedger, and lifecycle
   failures.

### P1 - Core capability correctness

6. The supplied 42-page TaskFlow PDF is parsed as text but planned as a generic
   two-file static project.
7. Multi-action prompts such as "edit, then show what changed" are captured by
   one first-match route instead of executed as a sequence.
8. `summarize @prd.pdf` displays mention context instead of summarizing it.
9. Codebase-Memory can index `.shamsu` mutation backups and manifests.
10. Abstract index freshness metadata can disagree with the external graph.
11. Post-answer long-term memory work can delay the next prompt or process exit.

### P2 - UX, maintainability, and operational clarity

12. Approval menu numbering changes meaning depending on whether a remembered
    permission option is offered.
13. Web status reports SearXNG health but not fallback-search availability.
14. Expected non-Git conditions can create misleading diagnostic error packets.
15. Prompt, context, model, tool, mutation, verification, and output records are
    spread across several stores without one correlation identifier.
16. Raw model reasoning is persisted by default in some session paths.
17. `shamsu/cli/repl.py` is large and contains routing, lifecycle, rendering,
    command handlers, memory finalization, and workflow dispatch in one module.
18. README, progress, and benchmark documentation do not describe one current
    release state.

## 5. Engineering Principles

Every implementation slice must follow these rules.

1. Measure before tuning prompts.
2. Prefer deterministic state and tool results over model self-report.
3. Keep one canonical run outcome.
4. Treat model output as untrusted input at every boundary.
5. Use append-only events for history and derived files for summaries.
6. Preserve workspace safety and approval boundaries.
7. Make degraded local behavior useful when optional services are unavailable.
8. Do not make Graphiti, Codebase-Memory, SearXNG, or Taskmaster a single point
   of failure for basic coding-agent work.
9. Keep low-resource constraints visible in performance tests.
10. Add a regression scenario before fixing each observed bug.
11. Do not merge prompt changes without equal-sample eval comparisons.
12. Keep raw chain-of-thought out of the default product log.

## 6. Target Request Lifecycle

Every natural-language prompt should follow one request lifecycle.

1. Receive and redact the prompt for persistence.
2. Create `session_id`, `turn_id`, and `run_id` correlation identifiers.
3. Resolve pending questions and explicit `@file` mentions.
4. Detect one or more requested operations.
5. Build a task graph or ordered step list.
6. Retrieve only the context required for the current step.
7. Record a short rationale and evidence for the selected action.
8. Ask for clarification when an irreversible user choice is missing.
9. Execute tools only through registered, sandboxed tool interfaces.
10. Record approvals and results.
11. Record mutations and rollback information.
12. Verify changed behavior when a safe verifier exists.
13. Derive the run outcome from evidence.
14. Produce a final response constrained by that outcome.
15. Return control to the user.
16. Perform bounded memory/index maintenance in the background.

## 7. Work Package 0 - Baseline Consolidation

### Objective

Move the existing reliability work onto a stable integration baseline before
adding more changes.

### Tasks

- Review the 20 commits on `fix/routing-planning-cot` as one reliability stack.
- Rebase or merge through a fresh branch based on current `develop`.
- Resolve documentation and branch-history conflicts deliberately.
- Preserve `BENCHMARK.md` and `BENCHMARK-light.md` as historical baselines.
- Record the current 1283-test baseline.
- Fix or suppress the Pytest collection warning for `TestRunResult` without
  changing its runtime contract.
- Update the progress tracker after integration.

### Exit criteria

- Reliability work is present on the agreed integration branch.
- Unit tests and lint remain green.
- Default and light eval baselines are reproducible.
- There is one documented commit from which later comparisons begin.

## 8. Work Package 1 - End-to-End Harness

### Objective

Test the same path users run, including orchestration, routing, approvals,
logging, tools, finalization, and post-run state.

### 8.1 Noninteractive runner

Add a first-class machine interface such as:

```text
shamsu run --workspace <path> --prompt <text> --output json
```

The runner must call the real request lifecycle rather than a simplified test
copy. It must support deterministic approval input and return:

- Run/session/turn identifiers.
- Selected operations and routes.
- Final outcome and response.
- Tool calls and command exits.
- Changed files and transaction IDs.
- Verification state.
- Timing and timeout phase.
- Paths to run artifacts.

### 8.2 Scenario format

Define scenarios in a structured format with:

- Name and tags.
- Initial file fixture.
- Optional Git state.
- Prompt or prompt sequence.
- Approval responses.
- Model mode: fake, recorded, or live.
- Expected route/task steps.
- Required and forbidden tools.
- Expected file contents or diff.
- Expected command and exit code.
- Expected outcome.
- Expected final-answer facts.
- Maximum user-visible completion time.
- Artifact-integrity assertions.

### 8.3 Test layers

Layer A - deterministic integration:

- Fake or recorded model responses.
- Exact routing, lifecycle, logging, approval, patch, and status assertions.
- Runs in normal CI.

Layer B - model-contract regression:

- Captured malformed diffs, fenced commands, fake tool XML, JSON tool calls,
  native tool calls, empty answers, and reasoning-model output.
- Tests the normalization and salvage boundaries without Ollama randomness.

Layer C - live Ollama evaluation:

- Runs against each supported model tier.
- Uses three or more samples for release baselines.
- Scores filesystem and command evidence, not only final prose.

Layer D - interactive PTY smoke:

- Tests prompt-toolkit input, approvals, cancellation, multiline input, and
  Windows behavior with a real terminal interface.

### 8.4 Initial regression scenarios

- Workspace location and file listing.
- Repository fact QA.
- File creation.
- Targeted edit.
- Edit followed by verification and diff.
- Syntax bug fix.
- Patch apply failure.
- Command success and command failure.
- Approval deny and remembered approval.
- Cancellation and timeout.
- `summarize @prd.pdf`.
- TaskFlow PRD planning.
- Web search with SearXNG.
- Web search through fallback provider.
- Browser inspection of a local app.
- Non-Git workspace.
- Abstract index refresh after write.
- Session follow-up and resumed session.
- Long-running memory write that cannot block the next prompt.

### Exit criteria

- Every confirmed QA defect has a failing scenario before its fix.
- The harness identifies false success automatically.
- A scenario can be replayed from its fixture and recorded model responses.
- CI runs deterministic scenarios without Ollama.

## 9. Work Package 2 - Canonical Run and Logging Contract

### Objective

Make one prompt fully reconstructable from one run directory.

### 9.1 Canonical ownership

- ActionLedger becomes the canonical per-prompt execution record.
- Session storage owns conversation history and references run IDs.
- Mutation storage owns rollback data and references run/turn IDs.
- Diagnostics stores large error packets referenced by events.
- Audit views are derived from or written through the canonical event stream.
- Memory stores durable facts and references the run that produced them.

### 9.2 Correlation identifiers

Every relevant record must contain:

- `schema_version`
- `session_id`
- `turn_id`
- `run_id`
- `operation_id`
- `parent_operation_id`, where applicable
- `timestamp`
- `sequence`

### 9.3 Outcome state machine

Supported terminal outcomes:

- `success`: requested work completed and required verification passed.
- `success_unverified`: mutation completed but no safe verifier was available.
- `partial`: some requested operations completed and others did not.
- `failed`: requested operation failed.
- `denied`: required approval was denied.
- `needs_input`: work paused for a user decision.
- `cancelled`: user or runtime cancelled the run.
- `timed_out`: a bounded phase exceeded its deadline.

The final assistant message must not set the outcome. The workflow result and
recorded evidence set it.

### 9.4 Decision records

Do not use raw chain-of-thought as the primary debugging contract. Record:

- Goal.
- Current observation.
- Evidence references.
- Selected action.
- Short reason summary.
- Alternatives considered.
- Confidence.
- Expected postcondition.
- Actual outcome.

Raw local-model reasoning may be available only under an explicit
`debug_full_trace` setting with redaction, strict size limits, short retention,
and a warning that it may contain source or secret material.

### 9.5 Context records

Create one context record per model call. Never overwrite a single shared
context preview.

Record:

- Model call ID and specialist role.
- System-prompt version/hash.
- User/task prompt hash and redacted preview.
- Source file path and line range.
- Content hash and file mtime.
- Retrieval score and inclusion reason.
- Token estimate and truncation information.
- Memory record IDs.
- Web source URLs and fetch IDs.
- Previous-turn message IDs.
- Omitted-context counts and reasons.

Store large redacted context payloads as referenced artifacts rather than large
inline JSON fields.

### 9.6 Tool and mutation records

For every tool call, record:

- Registered tool name and version.
- Sanitized arguments.
- Approval request/result.
- Start/finish timestamps and duration.
- Success, failure, denial, or timeout.
- Structured result and referenced full artifacts.
- Exception class, message, and traceback reference.

For every mutation, record:

- File path and operation type.
- Before and after hashes.
- Unified diff.
- Transaction ID.
- Backup path.
- Rollback availability and result.
- Abstract-index stale/refresh state.

### 9.7 Retention and safety

- Redact before disk writes at one enforcement point.
- Add configurable retention for runs, raw command output, context artifacts,
  and optional raw reasoning.
- Never feed ActionLedger logs back into the model automatically.
- Add a local command to validate run artifact integrity.
- Preserve backward-compatible readers for existing logs during migration.

### Exit criteria

- One run directory answers: what the user asked, what context was used, what
  was decided, which tools ran, what changed, how it was verified, and what was
  returned.
- Failed work cannot be marked successful by response logging.
- Decision files are populated in real runs.
- Tool-call counts count calls, not called/finished JSONL records.

## 10. Work Package 3 - Execution Truthfulness

### Objective

Make edits, patches, commands, verification, and final responses agree.

### Tasks

- Fix markdown fallback behavior when all fenced blocks are usage commands.
- Distinguish zero content candidates from multiple ambiguous candidates.
- Add regression coverage for verification-only fenced commands.
- Sanitize model-generated unified diffs before validation/application.
- Remove artificial context labels that are not actual target-file lines.
- Preserve exact diff apply exceptions.
- Log validation failure, denial, apply failure, rollback failure, and success as
  different outcomes.
- Ensure PatchEngine returns a structured result rather than only `True/False`.
- Connect simple `edit_file`/`write_file` mutations to ActionLedger mutation
  records, not only audit logs.
- Track changed files from confirmed tool data.
- Select verification based on changed files and project type.
- Run lightweight verification automatically when safe.
- Ask before dependency-installing or heavy verification.
- Derive the final response from a structured completion report.
- Prevent markdown salvage from running after a successful mutation unless the
  model is explicitly proposing another file operation.
- Add a postcondition guard: a claimed file change must match current disk
  state and transaction data.

### Required regression tests

- Successful edit cannot end in `needs_input` due to a usage fence.
- Patch apply failure includes the exact exception.
- Denied patch returns `denied`, not `failed` or `success`.
- Verification failure returns `failed` or `partial` as appropriate.
- A model saying "done" without a successful tool call cannot pass.
- A tool succeeding while the model says it failed produces a truthful success
  report with a warning about the inconsistent model response.

### Exit criteria

- The `qa_probe.py` edit scenario succeeds with a truthful final answer.
- The `qa_probe.py` bug-fix scenario applies and verifies, or fails with a
  precise actionable reason.
- Zero false-success scenarios in deterministic and live critical tests.

### Implementation result - 2026-07-19

- Markdown fallback now distinguishes no content from ambiguous content, skips
  verification-only fences, and cannot reinterpret a completed write unless a
  further file operation is explicit.
- Existing workspace filenames take precedence over permissive phrase parsing,
  preventing `fix the bug in qa_probe.py` from creating `bug in qa_probe.py`.
- Model-produced diffs pass through one sanitizer that removes markdown wrappers
  and exact synthetic `# File: ... (lines ...)` context labels.
- Patch application exposes `MutationResult` statuses for validation failure,
  denial, apply failure, verified success, unverified application, and
  verification failure while preserving exact exception text.
- Code-edit and bug-fix workflows preserve structured mutation failures instead
  of flattening them to `Patch was not applied.`
- Required mutations cannot finish from a tool-less model claim, and successful
  tool evidence overrides a contradictory model failure claim.
- Safe lightweight verification now runs after interactive writes. Failed
  verification is logged and cannot finalize as success.
- `edit_file` and `write_file` now write canonical transaction records with
  touched files, before/after hashes, backup data, and patch paths.
- Every ReAct executor call now records start, bounded redacted context, visible
  response or tool-call preview, duration, and terminal phase. Raw hidden
  chain-of-thought remains disabled by default; decision summaries and evidence
  are the supported rationale log.
- Live acceptance run `run_2026-07-19_16-35-51_4001` changed `qa_probe.py`,
  recorded matching hashes and transaction data, passed `python -m py_compile
  qa_probe.py`, and finalized as `success`.
- Remaining observations are assigned to later packages: compound prompt routing
  and Rich markup handling (WP4/UX), abstract freshness disagreement (WP5), and
  end-to-end timeout/post-run latency (lifecycle/performance).

## 11. Work Package 4 - Composite Routing and Clarification

### Objective

Handle natural developer requests as one or more operations rather than one
keyword-selected route.

### Tasks

- Keep deterministic detectors for slash commands and clear single operations.
- Add a small operation parser for compound prompts.
- Represent operations as an ordered task graph.
- Preserve dependencies such as edit before test and test before Git diff.
- Record all matching route candidates and the selected operation sequence.
- Make Git inspection a follow-up step when an earlier mutation is requested.
- Distinguish mention verbs: read, show, summarize, explain, compare, and edit.
- Route `summarize @prd.pdf` to a summarizer with the resolved PDF context.
- Preserve pending questions and paused tasks at operation granularity.
- Ask only for choices that materially change the implementation.
- Make clarification answers resume the paused operation rather than rebuild the
  request from scratch.
- Add deterministic handling for references such as "that file", "run it",
  "do the same", and "show me what changed".

### Required routing scenarios

- Edit a file, run it, then show the diff.
- Fix a failure, rerun the failed command, then summarize.
- Read two files and compare them.
- Search current docs, then update a local config.
- Create a project, run it, and return the local URL.
- Summarize an explicitly mentioned PRD.
- A pure Git status question remains read-only.

### Exit criteria

- Route order cannot silently discard earlier requested work.
- Audit route and actual dispatch remain identical.
- Compound prompts expose step-level progress and partial outcomes.

### Implementation result - 2026-07-19

- Added a conservative operation parser that keeps clear single operations on
  their dedicated routes and turns true compound requests into ordered steps
  with explicit dependencies.
- Compound dispatch records every matching route candidate and the selected
  operation sequence in the canonical decision record.
- Mutation, verification, Git inspection, web, read, compare, launch, and
  summary operations receive step-level terminal statuses and evidence.
- Git inspection remains read-only, follows requested mutations, and has a
  deterministic `git_status`/`git_diff` fallback when the model skips it.
- Pure Git requests remain on the read-only Git route. Dedicated game,
  development-server, PRD-summary, read/explain, and web follow-up routes are
  preserved instead of being over-split.
- Explicit `summarize @prd.pdf` requests resolve to PRD summarization, including
  colloquial wording that contains misleading Git keywords such as `checkout`.
- Clarification resumes from the original compound request plus the user's
  answer rather than routing the generated clarification wrapper as a new task.
- Rich Git output is rendered as literal text, preventing bracketed command
  output from raising markup exceptions.
- Required routing coverage includes edit/run/diff, fix/rerun/summarize,
  read-two/compare, web/update-config, create/run/URL, explicit PRD summary, and
  pure read-only Git scenarios.
- Live run `run_2026-07-19_17-12-05_304f` correctly finalized as `partial` when
  the model completed the mutation and verification but skipped Git inspection.
- After adding the deterministic Git follow-up, live run
  `run_2026-07-19_17-15-18_f7d8` changed only `qa_probe.py`, passed
  `python -m py_compile qa_probe.py`, captured `git_status` and `git_diff`, and
  finalized all four ordered steps as `success`.
- The successful run's artifact validation returned no errors or warnings: 52
  events, 1 decision, 6 tool records, 8 model records, 1 mutation, and 4 context
  records. The final quality gate is 1328 passed, 2 skipped; Ruff passes.
- Remaining observation: the model-authored prose preceding the canonical step
  report can be awkward or incomplete. Structured execution truth is correct,
  but final-answer synthesis should be improved in a later response-quality
  package.

## 12. Work Package 5 - Abstract Index, Retrieval, and Context

### Objective

Ensure every retrieval subsystem sees the same project and never indexes
SHAMSU's internal state as user code.

### 12.1 Shared workspace file policy

Create one reusable file-inclusion policy for:

- FileWalker and SQLite FTS.
- Semantic retrieval.
- Codebase-Memory indexing.
- Workspace listing and search tools.
- PRD discovery.
- Context building.
- Diagnostics related-code lookup.

Default exclusions must include:

- `.shamsu/`
- `.git/`
- `.venv/` and common virtual environments
- `node_modules/`
- build/dist/cache directories
- generated command logs and diagnostics
- mutation backups and trash
- binary files that are not explicit document inputs

### 12.2 External index isolation

Investigate the Codebase-Memory adapter's supported ignore configuration. If it
cannot enforce the shared policy, index a safe workspace projection or add an
adapter-owned ignore mechanism. Do not rely on a user-created `.gitignore` as
the only safety boundary.

### 12.3 Freshness model

- Calculate freshness from the exact externally indexed file set.
- Track a workspace mutation generation or content manifest.
- Mark stale only after confirmed writes.
- Refresh at a bounded point before the next code query.
- Record index version in context packs.
- Detect and rebuild corrupted or mismatched projects.

### 12.4 Degraded operation

- Keep exact file tools and SQLite/semantic retrieval available if
  Codebase-Memory is down.
- Report reduced structural understanding instead of blocking all coding work.
- Keep Codebase-Memory as a high-value accelerator, not the only doorway to the
  workspace.

### Exit criteria

- Queries never return `.shamsu` mutation backups.
- A successful file creation becomes searchable on the next relevant turn.
- Fresh status means the external index matches the shared file manifest.
- Basic local editing remains possible during an external index outage.

### Implementation result - 2026-07-20

- Added one versioned workspace-file policy for internal state, VCS metadata,
  virtual environments, dependency trees, caches, build output, generated
  Codebase-Memory artifacts, binary files, and user-owned source documents.
- FileWalker, semantic retrieval, planning context, workspace listing/search,
  mention and PRD discovery, path recovery, import diagnostics, scaffold
  detection, PRD verification, and the noninteractive harness now consume the
  shared policy instead of maintaining divergent deny-lists.
- The policy preserves legitimate hidden project content such as
  `.github/workflows` while excluding `.shamsu`, nested Git state, virtual
  environments, `node_modules`, and `.codebase-memory` artifacts.
- Codebase-Memory indexing installs a SHAMSU-managed `.cbmignore` block while
  preserving user-authored rules. External search results are also filtered at
  SHAMSU's boundary, so a stale or previously polluted graph cannot expose
  internal paths to an agent.
- Incremental refresh now validates the external graph for `.shamsu` paths. If
  an old index remains polluted, SHAMSU deletes only that workspace's graph
  project and performs a clean policy-controlled rebuild.
- Freshness now compares a policy version and stable file manifest and tracks
  workspace versus indexed mutation generations. Repeated writes debounce into
  one refresh, deletion/replacement is detected, and internal run artifacts do
  not make the index stale.
- `mark_stale()` updates both `last-index.json` and `status.json`, removing the
  previous contradiction where one file said fresh and the other forced stale.
- Manual build and refresh commands now update SHAMSU's canonical bookkeeping,
  and context records include index existence, freshness, manifest hash, policy
  version, workspace generation, and indexed generation.
- A missing or failed Codebase-Memory backend no longer blocks coding. SHAMSU
  reports degraded local retrieval and retains exact file tools, policy-filtered
  local text search, and optional local semantic search.
- Live migration of `test-shamsu` found 1,564 polluted nodes. The policy rebuild
  produced a 33-node clean graph; the old internal-only phrase then returned no
  `.shamsu` path.
- A temporary `wp5_freshness_probe` file advanced the workspace generation,
  became externally searchable on the next `ensure_ready()` turn, and vanished
  after deletion and the following refresh. The workspace was left fresh with
  generation and indexed generation both equal to 2.
- Live read-only QA run `run_2026-07-19_17-51-06_e993` exposed a negation bug:
  “Do not change any files” was interpreted as a mutation and reached patch
  approval. Approval denial prevented a write and the run honestly finalized as
  `denied`.
- Negated workspace-wide mutation phrases now produce answer/read operations,
  and a deterministic model-router guard overrides mutating intents for explicit
  read-only requests while preserving scoped constraints such as “fix the app
  but do not change tests.”
- Retest `run_2026-07-20_09-56-06_312b` answered from `qa_probe.py`, logged the
  router's `code_edit` prediction and the read-only override to `qa`, made zero
  tool calls or mutations, validated without warnings, and finalized as
  `success`.
- Final quality gate: 1343 passed, 2 skipped; Ruff passes.

## 13. Work Package 6 - Memory and Session Responsiveness

### Objective

Preserve useful memory without delaying interaction or saving incorrect task
summaries.

### Tasks

- Separate user-visible request completion from post-run memory work.
- Use a bounded background queue for Graphiti writes.
- Add a short optional shutdown flush with a hard deadline.
- Store local session memory first, then mirror to Graphiti best-effort.
- Do not save "task completed" memory for failed, denied, partial, or paused
  work without outcome qualification.
- Deduplicate memory by normalized content and source run.
- Save decisions, user preferences, durable project facts, and verified bug
  lessons; avoid saving ordinary read-only QA chatter.
- Add source run IDs and confidence to memory records.
- Ensure forgotten/tombstoned facts remain excluded.
- Surface memory backend health separately from request success.

### Exit criteria

- The next prompt is accepted immediately after the final response.
- Process exit is not delayed beyond the configured flush deadline.
- Failed work cannot become a positive durable lesson.
- A resumed session reconstructs task state without replaying debug noise.

### Implementation result - 2026-07-20

- Replaced one-daemon-thread-per-write behavior with one bounded queue per
  workspace. Queue size and shutdown flush budget are configurable through
  `SHAMSU_MEMORY_QUEUE_SIZE` and `SHAMSU_MEMORY_FLUSH_SECONDS`; defaults are 64
  pending mirrors and a 1.5-second shared hard flush deadline.
- Durable candidates are policy-checked and committed to workspace-local
  SQLite before Graphiti health checks, dedup lookups, or writes occur.
  Graphiti is now an asynchronous best-effort mirror rather than the immediate
  source of truth.
- Interactive and noninteractive shutdown paths flush queued mirrors only
  within the configured deadline. A slow backend cannot keep process exit open
  beyond that budget, and an unfinished mirror never removes the local copy.
- Automatic memory records include source run ID, turn/session correlation,
  outcome, verification state, and bounded confidence. Deduplication uses
  normalized kind/content and source run.
- Failed, denied, cancelled, timed-out, partial, needs-input, and non-mutating
  runs do not create positive automatic task memories. Unverified applied
  changes are explicitly labeled `success_unverified`; bug lessons require
  verification evidence.
- Read-only QA, explanations, general chat, and audits are excluded from
  automatic durable memory. Explicit `/memory remember` requests remain
  eligible with confidence 1.0.
- Automatic summaries strip task handoffs, retrieval text, and injected SHAMSU
  context before persistence, preventing file listings and internal prompt
  scaffolding from becoming durable facts.
- Local and Graphiti recall are merged so a newly saved local fact is available
  immediately while its mirror is pending. Search and recall both enforce
  tombstones; forgetting deletes the local row first and filters any surviving
  backend copy.
- `/memory status` reports local availability, Graphiti health, storage mode,
  and mirror-queue depth separately from whether ordinary agent work is
  allowed.
- Resumed sessions lead with a deterministic compact summary and pending task,
  last route, and last failure state, followed only by recent user/assistant
  turns. Tool output and model-debug events are not replayed into conversation
  memory.
- Recent-file follow-ups are answered directly from successful session tool
  results. Live run `run_2026-07-20_10-17-16_e751` returned the exact recorded
  path in 0.20 seconds with no model, tool, command, or mutation calls.
- Deterministic tests cover queue saturation, immediate local recall, hard
  flush deadlines, mirror failures, source-run deduplication, outcome policy,
  tombstones, clean resume context, and headless flushing.
- Final quality gate: 1359 passed, 2 skipped; Ruff passes.

## 14. Work Package 7 - PRD Understanding and Project Generation

Implementation status: complete on 2026-07-20.

### Objective

Turn realistic Markdown, TXT, and PDF requirements into grounded, verifiable
project plans and runnable projects.

### 14.1 Document normalization

- Remove repeated PDF headers, footers, and page numbers.
- Join wrapped paragraphs and bullets conservatively.
- Detect numbered headings and requirement identifiers.
- Extract tables where possible.
- Preserve page/section provenance.
- Detect image-only or low-confidence extraction.

### 14.2 Normalized PRD contract

Create a validated intermediate contract containing:

- Product name and summary.
- Users and roles.
- Architecture and required stack.
- Entities, fields, constraints, and relationships.
- Authentication and authorization rules.
- Pages and user journeys.
- API endpoints and operations.
- Search, filtering, sorting, and pagination.
- Persistence requirements.
- Validation and error states.
- Security and privacy requirements.
- Nonfunctional requirements.
- Required tests.
- Acceptance criteria and Definition of Done.
- Extraction confidence and source references.

### 14.3 Extraction strategy

- Use deterministic extraction for explicit structures.
- Infer obvious CRUD/page/API requirements from validated entities.
- Use a schema-constrained local model only for low-confidence gaps.
- Validate model output against source evidence.
- Never invent required stack components without marking them as assumptions.
- Ask the user when architecture ambiguity materially changes the generated
  product.

### 14.4 Generation gates

- Full-stack signals must prevent static two-file fallback.
- Zero entities plus strong CRUD/database signals must trigger extraction repair
  or `needs_input`, not silent generic generation.
- Show confidence and assumptions before approval.
- Generate in dependency order.
- Verify generated imports, routes, forms, templates, migrations, and tests.
- Run the project's Definition of Done before reporting success.

### TaskFlow acceptance fixture

For the supplied `prd.pdf`, the plan must identify:

- Product: TaskFlow Todo App.
- Authentication and protected user data.
- Task and Category domain concepts.
- User ownership/isolation.
- SQLite persistence.
- Task CRUD, completion, reopening, and deletion.
- Search, filter, sort, and pagination.
- Profile and statistics/dashboard behavior.
- Validation and error states.
- Automated tests.

The planned output must be a real full-stack project, not only `index.html` and
`README.md`.

### Exit criteria

- TaskFlow planning fixture passes deterministically.
- Generated TaskFlow project installs, migrates, tests, and starts locally.
- User data isolation and ownership tests pass.
- Failed generation reports the failing DoD item and next action.

### Implemented result

- PDF text is normalized with repeated-margin/page-number removal,
  conservative wrapped-line joining, table extraction, page provenance,
  confidence, and extraction warnings. Ordered list items no longer become
  false numbered sections.
- `PRDContract` now preserves product summary, users, roles, architecture,
  stack, entities, authentication/authorization, journeys, APIs, query
  capabilities, persistence, validation, errors, security, nonfunctional
  requirements, tests, acceptance criteria, assumptions, source references,
  and extraction confidence. Serialization remains backward compatible.
- Stack detection uses word boundaries. TaskFlow's security instruction not to
  "trust" client IDs no longer produces a Rust stack classification.
- Full-stack PRDs deterministically route to the Django CRUD pipeline. A
  persistence-heavy PRD with no domain entities returns `needs_input` before
  any approval or file write; it cannot fall back to two generic files.
- Generation state is target-aware and manifest-aware, and completed files must
  still exist before a run may resume. A fresh output directory no longer
  inherits `done` statuses from a different target.
- The Django generator now creates a migrations package, uses environment
  settings, supports Python 3.13 installation, namespaces API routes, and
  emits owner-scoped models, forms, serializers, API viewsets, HTML views,
  account/profile endpoints, task query controls/actions, dashboard statistics,
  and deterministic ownership/security tests.
- Real `prd.pdf` acceptance generated `test-shamsu/wp7-taskflow-8`: static
  consistency checks passed, Django checks passed, initial SQLite migrations
  were created/applied, and all 22 generated tests passed.
- Live HTTP acceptance passed web login, dashboard, Task/Category lists,
  profile/password pages, email token login, search/filter/pagination,
  statistics, completion, and reopening. The in-app browser runtime itself
  failed to initialize locally; that provider failure moves into Work Package
  8 rather than being reported as successful browser inspection.
- Final quality gate: 1370 passed, 2 skipped; Ruff passes.

## 15. Work Package 8 - Web, Browser, and External Knowledge

Implementation status: complete on 2026-07-20.

### Objective

Make web research reliable, transparent, and usable inside coding tasks.

### Tasks

- Represent web availability as a provider chain.
- Report SearXNG, fallback search, fetch, cache, and browser status separately.
- Record provider, query, result rank, fetch URL, title, and retrieval time.
- Require citations in externally grounded final answers.
- Keep private workspace source out of web requests.
- Support search/fetch inside compound tasks.
- Add deterministic recorded-response tests.
- Add live smoke tests against stable official documentation targets.
- Add local-browser scenarios for page text, console errors, screenshots, and
  basic interaction.
- Record browser artifacts in the canonical run.
- Verify generated local applications through browser checks when applicable.

### Implemented result

- `/web status` reports global enablement/provider mode, SearXNG state,
  configured fallback search, public page-fetch state, and cache state/path
  independently without silently probing external services.
- `/web search` uses the same configured provider chain as agent-internal web
  search. It no longer disables DuckDuckGo fallback by forcing local SearXNG.
- Search results and canonical events include provider attempts, success/error
  state, ranked title/URL/provider records, retrieval times, fetch method, and
  fetched-page metadata. Web and browser events mirror into ActionLedger runs.
- External fetch accepts only public HTTP/HTTPS targets, rejects credentials,
  localhost, loopback/private/link-local/reserved IPs, and revalidates every
  redirect. Local application inspection remains a browser operation.
- Queries containing the active workspace path or `.shamsu` internals are
  blocked before approval/network access; oversized queries have a bounded
  privacy limit.
- Browser status distinguishes missing Python dependency, missing Chromium,
  ready, and runtime failure with an actionable install command. Browser
  results include captured console/page errors and screenshot artifact paths.
- Live SearXNG and DuckDuckGo searches both retrieved official Python docs;
  fallback fetch extracted two readable official pages and evidence chunks.
- Playwright/Chromium was repaired in the local environment. Desktop and mobile
  TaskFlow login/Tasks checks completed with zero console errors. Screenshots
  `20260720-114237.png` and `20260720-114307.png` were saved under
  `test-shamsu/.shamsu/browser`; visual QA found and fixed a navbar collision.
- A deterministic real-browser fixture now verifies local page text, title,
  console-error capture, and screenshot creation when Chromium is available.
- Final quality gate: 1381 passed, 2 skipped; Ruff passes.

### Exit criteria

- `/web status` accurately describes every available provider.
- Fallback search is visible rather than surprising.
- Web-grounded answers cite the pages used.
- Browser checks can fail a project DoD when the rendered app is broken.

## 16. Work Package 9 - Approval and Safety UX

### Implementation status

Complete on 2026-07-20.

### Objective

Keep approvals understandable for humans and deterministic for automation.

### Tasks

- Use stable semantic inputs such as `y`, `a`, and `n` instead of changing
  numeric meanings.
- Make denial the safe default for empty or invalid input.
- Keep "allow once" and "always allow this low-risk action" visually distinct.
- Never offer remembered approval for commands, deletion, web access, browser
  mutation, or external tools unless policy explicitly changes.
- Include exact scope, target paths, working directory, and risk in approval
  records.
- Support harness-provided approval policies without parsing terminal menus.
- Add a dry-run mode that previews intended tools and mutations.
- Ensure cancellation rolls back incomplete mutations.

### Exit criteria

- Interactive and automated approvals have one stable meaning.
- Approval tests cover allow, deny, remember, invalid input, timeout, and
  cancellation.
- No approval decision is inferred from ambiguous input.

### Implemented result

- Interactive approvals use fixed `[y] Allow once`, `[a] Always allow ... in
  this workspace`, and `[n] Deny` choices. Empty, numeric, EOF, and invalid
  input deny without inference or retry.
- Remembered approval remains restricted in policy to file writes and edits;
  command, deletion, web, browser, and external-tool requests always ask.
- Approval request/result records include the complete request, decision
  source and scope, risk, working directory, preview, reason, and target paths.
- Headless callers continue to inject deterministic allow/deny scripts without
  terminal parsing. `--dry-run` supersedes allow, records planned actions and
  tool calls, denies every approval-gated action, and returns a distinct
  machine-readable `dry_run` result without mutating workspace files.
- A denied tool call now stops the agent loop immediately instead of prompting
  the model to retry the same action.
- Patch application restores partial edits on cancellation or unexpected
  exceptions. Django generation now asks before creating its target, records a
  transaction, and rolls back partial multi-file generation on cancellation.
- Tests cover semantic allow/deny/remember/invalid/EOF/Windows input, scripted
  policies, request timeout, dry-run isolation, denial-loop termination, and
  patch/project cancellation rollback.

## 17. Work Package 10 - Diagnostics and Error Quality

### Implementation status

Complete on 2026-07-20.

### Objective

Make every failure actionable without allowing expected conditions to pollute
future bug-fix context.

### Tasks

- Classify expected conditions separately from command failures.
- Do not make "not a Git repository" from a pre-edit probe the last actionable
  project failure.
- Preserve exception class, message, phase, operation, and traceback artifact.
- Link diagnostics to tool and model calls.
- Record root cause, secondary noise, affected files, and suggested next check.
- Keep raw logs as artifacts with redacted compact summaries inline.
- Add diagnostics integrity and retention tests.
- Make `/doctor` report corrupted or contradictory run/index/memory state.

### Exit criteria

- Bug-fix reuse selects the last relevant user-command failure.
- Patch errors include the apply reason.
- Expected environment probes never become false project failures.

### Implemented result

- `ErrorPacket` distinguishes success, command failures, expected conditions,
  policy decisions, and environment conditions, with an explicit actionable
  flag. Non-repository Git probes are expected and never replace the last real
  user-command failure.
- Failure reuse now accepts only actionable `command_failure` records sourced
  from user commands. Ignored outcomes are retained as session events instead
  of silently becoming future bug-fix context.
- Diagnostic packets include phase, operation ID, exception class/message,
  root and secondary diagnostics, affected files/symbols, suggested next
  check, compact redacted log, raw-log path, and traceback path.
- Command diagnostics link to command operations and tool results. Tool and
  model exceptions write linked traceback artifacts with exception identity.
- Run validation checks packet JSON, command/tool/model links, raw logs, and
  traceback artifacts. Retention tests prove stale diagnostics are removed
  with their run while fresh run evidence remains.
- Patch apply failures now include the approved apply reason as well as the
  exact exception. Validation failures remain distinct pre-approval errors.
- `/doctor` adds a read-only workspace-state check for corrupt run artifacts,
  invalid index/memory/diagnostics JSON, contradictory index generations, and
  SQLite memory integrity.

## 18. Work Package 11 - REPL Modularization

### Implementation status

Complete for the beta boundary on 2026-07-20.

### Objective

Reduce maintenance risk without a broad behavior rewrite.

### Proposed boundaries

- `cli/request_lifecycle.py`: start, dispatch, finish, fail, and cancellation.
- `cli/routing.py`: route/operation detection and task graph construction.
- `cli/commands/`: slash-command handlers grouped by domain.
- `cli/rendering.py`: Rich panels, tables, and final summaries.
- `cli/session_commands.py`: session/log/run commands.
- `cli/approval_ui.py`: interactive approval presentation.
- `cli/repl.py`: prompt loop and top-level wiring only.

### Migration strategy

- Add characterization tests first.
- Move one cohesive block per PR.
- Preserve public helpers used by tests through temporary re-exports.
- Do not combine extraction with behavioral fixes.
- Remove compatibility re-exports only after callers migrate.

### Exit criteria

- `repl.py` owns interaction, not every subsystem's business logic.
- Routing and request lifecycle can be tested without prompt-toolkit.
- No behavior or benchmark regression from structural moves.

### Implemented result

- `cli/arguments.py` owns interactive/headless argument parsing and validation.
- `cli/approval_ui.py` owns request-scoped policy injection, semantic menu
  wiring, autonomy behavior, and process-scoped permission memory.
- `cli/request_lifecycle.py` owns canonical session/ledger event output and run
  finalization shared by the REPL and headless runner.
- `cli/session_commands.py` owns `/runs` and `/run` inspection, validation,
  export, and retention commands.
- `routing/operations.py` remains the structured operation-plan boundary built
  in the earlier routing package. The order-sensitive dispatcher remains in
  `repl.py` to avoid mixing extraction with a behavior rewrite.
- Temporary private compatibility exports preserve existing callers and test
  monkeypatch points. Direct module-boundary characterization tests pin those
  exports, request-scoped approval injection, and lifecycle finalization.
- The modularization/routing characterization gate passed 157 tests; the full
  repository gate passed with no behavior regression.

## 19. Work Package 12 - Product Polish and Release

### Objective

Turn the corrected system into a coherent beta release.

### Tasks

- Update README, CHANGELOG, progress tracker, benchmark docs, and demo script.
- Document supported model tiers and measured limitations.
- Document local state layout and retention.
- Add a concise run-inspection command that summarizes decisions, tools,
  changes, verification, and output.
- Add first-run checks for runtime, model, index, memory, web, and browser.
- Add upgrade handling for older `.shamsu` schemas.
- Validate Windows, Linux, and macOS install/run/uninstall paths.
- Measure startup time, first-answer time, task time, peak memory, and disk-log
  growth.
- Run dogfooding sessions on Python, Django, Node, React, and mixed repositories.
- Cut a beta release only after the release gate passes.

### Completion Record (2026-07-20)

- Released package metadata as `0.4.0b1` after the deterministic release gate
  passed.
- Canonical empty run artifacts now exist for read-only prompts, eliminating
  false integrity failures while retaining activity-specific zero counts.
- `/run show` now summarizes decisions, tool outcomes, changed files,
  verification, and final output; detailed artifact commands remain available.
- First interactive launch records runtime/model, code index, memory, web, and
  browser readiness in `.shamsu/first-run-report.json`; `/doctor` includes the
  same capabilities plus install, state-schema, and run-integrity checks.
- Workspace state schema 2 upgrades are idempotent, sanitize legacy remembered
  approvals, reject future schemas, and never rewrite historical run evidence.
- Bash lifecycle scripts were normalized to LF after Windows testing exposed a
  real parse failure. CI now validates install/run/uninstall contracts on
  Windows, Linux, and macOS and runs a real installed headless request.
- The release validator passed Python, Django, Node, React, and mixed workspace
  sessions with complete artifacts. Measured results: 1.266s startup, 1.071s
  cold first answer, 0.307s slowest warm answer, 2.193s for five tasks, 90.9 MB
  peak RSS, and 52,128 bytes of log growth.
- The web cache now closes SQLite connections explicitly and initializes lazily;
  this fixed immediate Windows cleanup failures and reduced non-web run logs.
- README, changelog, benchmark docs, demo, progress tracker, and release report
  now describe shipped behavior and measured model-tier limitations.

## 20. Issue-to-Test Matrix

| Issue | Deterministic test | Recorded-model test | Live scenario |
|---|---|---|---|
| False success | Required | Required | Required |
| Patch apply error missing | Required | Required | Required |
| Markdown usage fence | Required | Required | Required |
| Composite edit/diff route | Required | Optional | Required |
| Mentioned PDF summary | Required | Required | Required |
| TaskFlow PRD plan | Required | Required | Required |
| Abstract `.shamsu` pollution | Required | Not needed | Required |
| Memory completion delay | Required | Not needed | Required |
| Web fallback status | Required | Required | Required |
| Approval consistency | Required | Not needed | PTY required |
| Non-Git diagnostics | Required | Not needed | Required |
| Session resume | Required | Required | Required |

## 21. Performance Budgets

Initial budgets should be measured and adjusted from real low-resource hosts.

- REPL startup without model pull: target under 3 seconds.
- Deterministic workspace answers: target under 1 second after startup.
- First visible progress event: target under 500 ms.
- User-visible completion must not wait for Graphiti persistence.
- Context should stay within the active model's configured budget.
- Only one large model should remain active at a time on the default 8 GB path.
- Run logs must use bounded inline fields and configurable retention.
- Index refresh after a small edit should be incremental.

Performance regressions must be reported alongside task-success eval changes;
speed is not an acceptable trade for false success, and correctness is not an
excuse for unbounded stalls.

## 22. Release Quality Gates

### Code quality gate

- Full unit/integration suite passes.
- Ruff passes.
- No unexplained warnings are introduced.
- New behavior has deterministic regression coverage.

### Agent quality gate

- Zero false-success critical scenarios.
- Default-tier critical cases pass 3/3.
- Light-tier limitations are named and surfaced to users.
- Compound edit/test/diff prompt succeeds.
- Bug-fix apply and verification succeed on representative Python and
  JavaScript fixtures.
- QA answers are grounded in actual workspace files.

### PRD gate

- TaskFlow PDF produces a grounded full-stack plan.
- Generated project passes migrations and tests.
- Generated application starts and passes basic browser inspection.
- Ownership/security acceptance tests pass.

### Observability gate

- Every prompt has a canonical run.
- Every model call has a context reference.
- Every tool call has a result.
- Every mutation has hashes, diff, transaction, and rollback state.
- Every run has a truthful terminal outcome.
- Every final response is stored and agrees with the outcome.

### Safety gate

- Workspace escape tests pass.
- Dangerous commands remain blocked.
- Approval semantics remain stable.
- Secret-redaction tests cover prompts, model output, tool arguments/results,
  command logs, context artifacts, and exports.
- Web tools never receive private source unless the user explicitly requests a
  permitted external action.

### Product gate

- Q&A, file work, bug fixing, test generation, documentation, project creation,
  web research, browser inspection, resume, cancellation, and undo all pass
  end-to-end scenarios.
- Documentation describes the shipped behavior and current test/eval numbers.
- Install and doctor paths work on supported operating systems.

## 23. Recommended PR Sequence

1. Baseline consolidation and documentation truth.
2. Noninteractive full-request harness.
3. Canonical run IDs, outcomes, and ActionLedger wiring.
4. Patch/fallback/final-answer correctness.
5. Composite routing and mentioned-file actions.
6. Shared workspace file policy and abstract-index repair.
7. Asynchronous memory finalization.
8. PRD contract and TaskFlow fixture.
9. Web/browser status and end-to-end checks.
10. Approval and diagnostics cleanup.
11. REPL modularization.
12. Beta release hardening and documentation.

Each PR must be independently reviewable and leave tests green. Do not mix
large structural moves with prompt tuning or behavior changes.

## 24. Definition of SHAMSU "Great"

SHAMSU is great when a user can enter a real project and trust it as a local
engineering collaborator:

- It understands what the user asked, including multi-step requests.
- It uses real project context rather than invented files.
- It asks before making consequential choices.
- It performs actions through safe, observable tools.
- It can recover from ordinary model-format mistakes.
- It verifies changes instead of trusting its own prose.
- It admits failure with an actionable reason.
- It remembers verified lessons without blocking interaction.
- It can research current information without leaking private code.
- It can generate and run a complete project from a realistic PRD.
- It works on modest hardware with transparent model-tier limitations.
- Its logs make every outcome explainable without exposing raw private
  chain-of-thought by default.

The immediate next implementation step is Work Package 1 followed by Work
Package 2: build the complete harness, then establish truthful run semantics.
Those two packages make every later improvement measurable and protect SHAMSU
from becoming a system that looks capable in unit tests while behaving
unreliably in the real REPL.
