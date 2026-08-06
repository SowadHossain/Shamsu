# SHAMSU v1 — Archived Implementation

> **This is not the production implementation.**
> Production SHAMSU v2 lives under [`src/shamsu/`](../src/shamsu/).
> Nothing in v2 may import from this directory. See §"Import boundary" below.

---

## What this is

The complete SHAMSU v1 implementation (`shamsu` v0.4.0b1), archived verbatim
from the repository root on branch `shamsu-v2.0.0`.

| | |
|---|---|
| **Archived commit** | `b64780e` (`docs: capture v1 analysis reports and the v2 rebuild plan`) |
| **Baseline tag** | `shamsu-v1-legacy-baseline` |
| **Archived from branch** | `mayday-lastresort` |
| **Package version** | `0.4.0b1` |
| **Python** | `>=3.11` |
| **Archival commit** | `bb84e2f` (`chore: archive SHAMSU v1 under legacy-code`) |

Layout after archival:

```text
legacy-code/
├── shamsu/          v1 package (317 tracked files, 33 subpackages)
├── tests/           v1 pytest suite (168 tracked files)
├── evals/           v1 evaluation harness and PRD fixtures
├── scripts/         v1 install/doctor/benchmark lifecycle scripts
├── pyproject.toml   v1 dependency set and ruff config
└── docs/
    ├── README.md, CHANGELOG.md, BENCHMARK*.md, DEMO*.md, RELEASE_VALIDATION.md
    └── agent-context/
        ├── AGENTS.md, CURRENT_STATE.md, PROGRESS.md, REQUIREMENTS.md
        ├── AGENT_LOOP_AND_TOOLING_REPORT.md   ← the loop teardown cited below
        ├── SMALLCTL_STACK_REPORT.md
        ├── SHAMSU_VS_SMALLCTL_DIMENSIONS.md
        └── CLAUDE-v1.md                       ← v1 agent orientation, pre-rebuild
```

---

## Why it was archived

v1 works, but its orchestration is owned by the *model* rather than the
*runtime*. Reliability improvements kept requiring new inline guard counters
rather than a change to the control structure. The decision recorded in
[`docs/migration/v2-full-rebuild-plan.md`](../docs/migration/v2-full-rebuild-plan.md)
is to rebuild the orchestration core around a typed runtime and keep v1 as a
donor, not a dependency.

---

## Which loop was live

This matters more than anything else in this file, because the repository
contains two loops and **the more capable one was never wired up.**

| | `AgentChatLoop` | `ToolCallingAgentLoop` |
|---|---|---|
| File | `shamsu/agents/chat_loop.py` | `shamsu/agents/tool_calling_loop.py` |
| **Live in production?** | ✅ Yes — the only loop `repl.py` constructs | ❌ No — test-only |
| Cancellation | **None** | `register_run` / `cancel_event` / feedback queue |
| Only importer | `shamsu/cli/repl.py` | `tests/test_native_tool_calling_agent.py` |

`shamsu/runtime/run_control.py` — which implements `register_run`, `cancel_run`,
`add_feedback`, `complete_run`, and in-flight model-task cancellation — is
**dead code in production**. Its only importer is the test-only loop.

Source: `docs/agent-context/AGENT_LOOP_AND_TOOLING_REPORT.md` §1, §11.2,
verified there by exhaustive repo-wide grep.

---

## Known orchestration problems

1. **The model owns the loop.** Progress, tool choice, and completion are all
   model-driven; the runtime reacts. There is no authoritative external state
   machine.
2. **Recovery is a pile of inline counters.** At least fifteen independent
   per-run guard counters live as mutable state inside `chat_loop.py`
   (`:1051-1080`): identical tool-call repeats (3), read-failure recoveries (3),
   prose-only corrections (2), stall answers (2), empty responses (2),
   missing-mutation recoveries (2), truncation recoveries (3), and more. Each
   was added to patch an observed failure. They interact in ways nobody has
   modelled.
3. **Two tool registries, two surfaces.** `ToolRegistry` (3 action-only tools)
   and `AgentToolRegistry` (42 tools, 3,731 lines in one file) coexist with
   different validation and gating paths.
4. **Implicit success classification.** Completion is inferred from model
   behaviour rather than gated on registered evidence.
5. **Loop-bound sprawl.** Bounds are scattered across nine files with no single
   place to reason about total work per task. See the inventory in
   `AGENT_LOOP_AND_TOOLING_REPORT.md` §4.
6. **Long-running mode multiplies every bound at once** — tool rounds 8→50,
   error-feedback iterations 3→50, repair attempts 3→25 — with no
   progress-based stopping criterion.

## Known cancellation problems

- The live `AgentChatLoop` has **no mid-run cancellation path at all**,
  including in long-running mode where a run may legitimately last an hour.
- `chat_loop.py`'s only occurrence of `cancel` is `beat.cancel()` at `:984`,
  which cancels the *heartbeat* task, not the run.
- `cancel_run` and `add_feedback` can be called, but nothing in the live path
  observes them.
- Consequence: an in-flight v1 run is not observably cancellable. **This is the
  single defect that most directly motivates v2's Run Controller.**

## Known memory concerns

- Memory is Graphiti (external, `~/.shamsu/tools/graphiti/`) with a SQLite floor
  at `shamsu/memory/sqlite_store.py`, mirrored asynchronously via a bounded
  queue.
- The Graphiti integration is **not trusted to be correctly wired into the live
  runtime**, and it is expensive: a separate service to keep healthy, extra
  model calls for memory extraction, significant CPU/RAM, and harder debugging.
- No freshness, confidence, or invalidation model — stale structural claims can
  reach the model without a warning.
- v2 therefore drops Graphiti from the critical path entirely
  (plan §2). SQLite is authoritative; Graphiti is at most a future optional
  adapter, gated by plan §33.

## Other flagged issues

- Ledger truncation fields are not uniformly meaningful across call sites
  (`AGENT_LOOP_AND_TOOLING_REPORT.md` §11.3).
- `README.md` and `pyproject.toml` in this archive are **stale**: the described
  `.shamsu/index.db` SQLite FTS5 index no longer exists, and `rank_bm25` and
  `yake` are declared dependencies that are imported nowhere.

---

## How to run the legacy tests

From this directory, not the repository root:

```bash
cd legacy-code
pip install -e ".[dev]"
ruff check shamsu/ tests/ evals/ scripts/
pytest tests/ -q --tb=short
```

CI equivalent: `.github/workflows/legacy-ci.yml` (manual dispatch, or on pushes
touching `legacy-code/**`). It is `continue-on-error` — legacy results report
but never gate v2.

### ⚠️ Baseline test status: NOT ESTABLISHED

The suite was **not** successfully run at archival time. On the rebuild machine:

```text
74 errors during collection
E   ModuleNotFoundError: No module named 'mcp'
```

and an isolated environment could not be built (`ensurepip`/`python3-venv`
unavailable; system Python is PEP-668 externally managed). Every one of the 74
errors is a collection-time import failure, not a test assertion failure — so
**no statement about v1 pass/fail rates can be made from this archive.**

Before using this tag as an evaluation baseline, run the suite on a machine with
the full dependency set and record the result here. Until then, treat the
"known failures" list as unpopulated rather than empty.

---

## Import boundary

Production v2 code **must not** import from `legacy-code/`.

- `legacy-code/` is not on the production Python path.
- `legacy-code/pyproject.toml` is a *separate* project definition; the root
  `pyproject.toml` for v2 does not reference it.
- This is enforced mechanically — see the import-boundary check in v2's CI.

## Components that may be migrated

These are candidates only. Each must go through the ten-step migration process
in plan §8.2 (identify symbol → review deps → isolated tests → clean v2
interface → rewrite → strip old-loop deps → document → v2 tests → security
checks → eval tasks) and be recorded in
[`../LEGACY_COMPONENTS.md`](../LEGACY_COMPONENTS.md).

| Candidate | v1 location |
|---|---|
| Sandbox path validation | `shamsu/safety/` |
| Command risk classification | `shamsu/safety/`, `shamsu/tools/executor.py` |
| Command timeout handling | `shamsu/tools/executor.py` |
| Model-output normalization | `shamsu/llm/output.py` |
| Tool-call salvage / quote repair | `shamsu/llm/output.py` |
| Test-output digesting | `shamsu/verify/` |
| Error-signature generation | `shamsu/agents/error_feedback_loop.py` |
| Tool-result truncation | `shamsu/agents/chat_loop.py:90-97` |
| Git utility functions | `shamsu/tools/agent_tools.py` (git group) |
| Structural code-graph client | `shamsu/abstract/`, `shamsu/retriever/` |
| Reliability metrics | `shamsu/telemetry/` |
| Secret-redaction utilities | `shamsu/safety/` |

The three-layer timeout architecture (`AGENT_LOOP_AND_TOOLING_REPORT.md` §5) is
described as the most carefully-reasoned part of v1 and is worth reading before
designing v2's equivalent, even if the code is not copied.

## Components that must NOT be migrated as architecture

- `AgentChatLoop`
- The old main loop structure
- The old task lifecycle
- The old prompt-conversation replay model
- The old planner orchestration
- The old completion logic
- The old memory orchestration
- The old two-registry tool architecture
- Large collections of inline recovery counters
- Long-running mode behaviour
- Implicit success classification

---

## Permitted uses of this directory

1. Reference reading.
2. A source of known failure cases.
3. A source of isolated, testable utilities.
4. A baseline for evaluations.
5. A donor for specific algorithms, migrated behind clean v2 interfaces.

Anything else — in particular, importing it at runtime — is out of bounds.
