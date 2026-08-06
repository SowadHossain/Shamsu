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

### Fixed

- `RunController.cancel()` claimed thread safety but wrote run status through a
  thread-bound SQLite connection, so cancelling from a signal handler raised
  `ProgrammingError` after setting the token — leaving the token cancelled and
  the database still reporting `RUNNING`. `StateStore` is now genuinely
  thread-safe (`check_same_thread=False` plus a re-entrant lock on every method
  that touches the connection), and `wait_if_paused` races the resume gate
  against the token rather than having `cancel()` touch an asyncio primitive
  from another thread.

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

- **No legacy baseline test result.** The v1 suite could not be run on the
  rebuild machine: 74 collection errors from
  `ModuleNotFoundError: No module named 'mcp'`, with no `python3-venv`
  available to build an isolated environment. Tracked in `MIGRATION_STATUS.md`.
- **mypy is configured but unverified locally** — not installed in the rebuild
  environment. First real run happens in CI.
- v2 is not yet a usable agent. Milestones 2–15 remain.

---

## Legacy

`shamsu-v1-legacy-baseline` tags the final v1 state (`0.4.0b1`). See
[`legacy-code/LEGACY_README.md`](legacy-code/LEGACY_README.md).
