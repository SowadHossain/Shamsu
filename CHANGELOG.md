# Changelog

All notable changes to SHAMSU v2. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The v1 changelog is archived at
[`legacy-code/docs/CHANGELOG.md`](legacy-code/docs/CHANGELOG.md).

## [Unreleased]

### Added

- **v2 package skeleton** under `src/shamsu/` — thirteen subpackages, each
  documenting its responsibility and the milestone that fills it.
- **Interface layer** (`src/shamsu/interfaces/`) defining every runtime seam:
  - `enums` — the shared vocabulary (`Phase`, `AgentState`, `StepOutcome`,
    `RunStatus`, `Risk`, `ArtifactStatus`, `ArtifactKind`, `EvidenceKind`,
    `FailureKind`, `ApprovalDecision`). Effectively frozen.
  - `ids` — distinct `NewType` identifiers, so mypy rejects passing a `TaskId`
    where a `RunId` belongs.
  - `cancellation` — `CancellationToken`, `Cancelled`, `FeedbackInterrupt`,
    `NullCancellationToken`. Cancellation is a parameter on every blocking
    call, which is the structural fix for v1's unreachable control plane.
  - `tools` — `ToolContract`, `ToolRequest`, `ToolResult`, `Tool`,
    `ToolGateway`, `ToolPolicyViolation`.
  - `artifacts` — `Artifact`, `ArtifactMeta`, `SourceRef`, `Contradiction`,
    `ArtifactStore`, `ArtifactGenerator`.
  - `models` — `ModelClient`, `ModelRequest`/`ModelResponse`,
    `ModelContractError`, `ModelTimeout`, `ModelUnavailable`,
    `OutputNormalizer`.
  - `code_intelligence` — `CodeIndex`, `SemanticIndex`, `SymbolRef`,
    `SearchHit`, `ImpactReport`.
  - `context` — `ContextCompiler`, `ContextFrame`, `ContextSection`,
    `TokenBudget`.
- **Import-boundary enforcement** — `scripts/check_import_boundary.py` fails on
  any import, `sys.path` injection, path literal, dependency, or wheel
  packaging that reaches into `legacy-code/`. AST-based, so documentation may
  discuss the archive freely.
- **CI** (`.github/workflows/ci.yml`) — boundary gate, then lint, format, strict
  mypy, and tests across Linux/macOS/Windows on Python 3.11 and 3.12, plus a
  guard that the archived suite was not collected.
- **Deterministic test double** — `tests/fixtures/fake_model.py` provides a
  scripted `ModelClient` and a `CancelAfter` token. No test contacts a model.
- `README.md`, `ARCHITECTURE.md`, `MIGRATION_STATUS.md`, `LEGACY_COMPONENTS.md`.
- **State and persistence** (`src/shamsu/state/`):
  - `records` — frozen typed records for project, run, task, plan, plan step,
    tool event, evidence, approval, checkpoint, and failure. The per-task
    counters that v1 kept as loop-local attributes are persisted here, so they
    survive a crash and can be asserted on.
  - `schema` — SQLite DDL with enforced foreign keys, WAL journaling, and
    append-only `user_version` migrations. Refuses a database written by a
    newer build rather than risking semantics it does not implement.
  - `transitions` — the state machine as a table rather than as the order of
    `if` branches. Cancellation is a separate parameter, not an edge from every
    node, so the graph stays readable.
  - `store` — `StateStore`, which validates every state change against the
    transition table, writes plans and their steps atomically, and resumes a
    task from its latest checkpoint.

  Two guarantees now hold at the storage layer: **transitions cannot be
  bypassed** (`advance_task` raises without writing), and **evidence cannot be
  forged** (`evidence.source_event_id` is a non-null foreign key to
  `tool_events`, so no model assertion can become a row).
- **Run control** (`src/shamsu/runtime/`):
  - `tokens` — `RunToken`, observable by polling from any thread *and* by
    `await` on the event loop, so a long model call can be raced against
    cancellation rather than only checked between calls. Feedback uses the same
    machinery with different semantics: the run continues, only the in-flight
    call is abandoned.
  - `controller` — `RunController`: registration, status, cancel, pause/resume,
    feedback injection, wall-clock enforcement, and the event log. `CANCELLING`
    and `CANCELLED` are separate statuses so status can never claim a stop that
    has not happened.
  - `limits` — `ExecutionLimits`, plan §11's bounds in one frozen object rather
    than v1's nine files. Long-running mode and automatic production actions
    are off by default.
  - `events` — `RunEvent` / `EventKind`, the observable run timeline.

  **This closes the defect that motivated the rebuild.** v1's control plane was
  fully implemented and never imported by the live loop. Here the controller
  owns the token and the token is a required parameter on every blocking call,
  so a component cannot forget to observe cancellation — it cannot block
  without being handed the thing that reports it.

- **Artifact foundation** (`src/shamsu/artifacts/`) — Milestone 3:
  - `hashing` — content hashes rather than timestamps, and git-aware scanning
    (`ls-files --cached --others --exclude-standard`) so the project's own
    `.gitignore` decides what belongs to it. On this repository that is 64
    files scanned instead of 894.
  - `registry` — `ArtifactRegistry`: content on disk under `.shamsu/artifacts/`
    so it stays readable and diffable, freshness in SQLite so it stays
    queryable. Invalidation by source hash, by path, by generator version, and
    by recorded contradiction. `usable()` gates INVALIDATED, MISSING, and
    GENERATION_FAILED so they cannot reach the model.
  - `python_source` — deterministic extraction via stdlib `ast`. No new
    dependency; tree-sitter arrives with the other languages in Milestone 8.
  - `generators` — repository manifest, repository map, module cards, and
    symbol cards (plan §15.1–15.4), including real reverse import edges.
  - `refresh` — `ArtifactRefresher`: scan, recompute freshness, retire absent
    subjects, regenerate, report. Cancellable between artifacts.
  - Schema migration 2: `artifact_records`, `artifact_sources` (indexed by
    path), `artifact_contradictions`.

  Cards state **"Not yet computed"** for callers, callees, and measured
  coverage rather than leaving those sections blank — a blank "Callers" heading
  reads as *nothing calls this*, which is a structural claim nothing has
  earned.
- Boundary checker: a `# boundary-ok: <reason>` pragma for code that must
  *name* the archive in order to *exclude* it. Per-line, reason-carrying, and
  unable to exempt an import.

- **Tool gateway and path sandbox** (`src/shamsu/tools/`, `src/shamsu/security/`)
  — Milestone 4:
  - `security/paths` — `PathSandbox`. Resolves before deciding, follows
    symlinks, and accepts absolute paths that land inside the workspace. The
    third rule is the direct v1 lesson: v1 regex-scraped paths and silently
    demoted `/tmp/ws/x.md` to `tmp/ws/x.md`.
  - `tools/base` — typed `Tool`; the model-facing JSON schema is derived from
    the input model, so the schema shown and the validation applied cannot
    drift. `run` receives a validated object, never raw arguments.
  - `tools/gateway` — one registry. Resolve, phase, approval, validate,
    mutation budget, execute under timeout racing cancellation, cap output.
    Every refusal happens before the side effect. Default approval is
    `deny_all`.
  - `tools/readonly` — `project.inspect`, `code.search`, `file.read`.
- **Context compiler** (`src/shamsu/context/`) — priority budgeting where hot
  context raises rather than being silently dropped, and stale artifacts are
  labelled before they reach the model.
- **Output contracts** (`src/shamsu/models/contracts.py`) — narrow shapes a
  response is allowed to take, plus a compact `schema_hint` renderer that fits
  the 400-token tool-definition budget.
- **Read-only agent** (`src/shamsu/agent/readonly.py`) — the bounded
  investigate loop and grounded planning. `is_grounded()` rejects a plan citing
  files the agent never opened.

### Fixed

- **mypy strict now passes** on all 35 source files, and every `type: ignore`
  is gone from `src/`. Seven real errors surfaced on its first run: a
  `functools.wraps` return-type widening in the state store's lock decorator,
  and `dict[str, object]` manifest parsers that made every nested `.get()` an
  error. The twelve `type: ignore` comments were hiding `object`-typed database
  rows; typing them as `sqlite3.Row`/`sqlite3.Connection` removed the need, and
  narrowing `ArtifactRegistry._evaluate` to `Sequence[tuple[str, str]]` also
  made the invalidation rules testable without a database.
- **The v1 legacy suite runs again.** `pip install --target` plus `PYTHONPATH`
  sidesteps this machine's PEP-668 restriction and missing `python3-venv`,
  which had blocked the baseline since archival. 74 collection errors → 0.

- `RunController.cancel()` claimed thread safety but wrote run status through a
  thread-bound SQLite connection, so cancelling from a signal handler raised
  `ProgrammingError` after setting the token — leaving the token cancelled and
  the database still reporting `RUNNING`. `StateStore` is now genuinely
  thread-safe (`check_same_thread=False` plus a re-entrant lock on every method
  that touches the connection), and `wait_if_paused` races the resume gate
  against the token rather than having `cancel()` touch an asyncio primitive
  from another thread.
- Repository-wide artifacts did not notice files being added or deleted. The
  manifest reports a file count and directory list but declared only
  `pyproject.toml` as a source, so deleting a module left it FRESH and wrong.
  A synthetic `<repository:file-list>` source now covers the set of indexed
  paths.
- `src/` was stripped unconditionally when deriving dotted module paths.
  Correct for a src-layout project, wrong when `src/__init__.py` exists — and
  in that case it silently broke every import edge the module cards reported.
  The repository context now detects which packaging roots actually apply.
- A card whose subject was deleted was invalidated but not reported as retired,
  because hash-based recomputation reached it first.
- `NullCancellationToken.wait_cancelled()` raised instead of never resolving.
  Since the method exists to be raced against real work, a raising
  implementation completed instantly and the tool gateway read that as
  cancellation — turning every timeout into a spurious user interrupt.

### Changed

- **Archived SHAMSU v1 under `legacy-code/`** (522 paths). v2 is a greenfield
  implementation under `src/shamsu/` and does not import the v1 agent loop,
  prompt builder, planner lifecycle, or memory orchestration.
- Root `pyproject.toml` rewritten for v2: src layout, `pydantic` as the only
  runtime dependency, strict mypy, ruff with a banned-import rule for the
  archive. The v1 dependency set is archived, not inherited.
- v1's CI workflow became `legacy-ci.yml` — scoped to `legacy-code/**`,
  manual-dispatch, `continue-on-error`. Legacy results no longer gate v2.

### Removed

- **Graphiti from the critical path.** v2 works fully without it. SQLite plus
  repository artifacts is the memory design. Reconsideration is gated by the
  plan's §33 and Graphiti may never become authoritative for task state, plan
  state, completion evidence, approvals, or checkpoint recovery.

### Known gaps

- **One genuine v1 bug is known and unfixed** (in archived code, so it does
  not affect v2): `_FILE_TOKEN_RE` cannot match a POSIX absolute path, leaving
  the run-contract layer's workspace-relative normalization dead on Linux and
  macOS. Documented in `LEGACY_COMPONENTS.md` as a defect any migration must
  fix. Three baseline failures still need a local model to verify.
- v2 is not yet a usable agent. Milestones 4–15 remain.
- Live local inference has never been exercised. The suite runs entirely
  against a deterministic fake; the `ModelClient` implementations arrive with
  Milestone 4 and must be validated on a GPU-equipped machine.

---

## Legacy

`shamsu-v1-legacy-baseline` tags the final v1 state (`0.4.0b1`). See
[`legacy-code/LEGACY_README.md`](legacy-code/LEGACY_README.md).
