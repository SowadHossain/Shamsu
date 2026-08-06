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
| **Baseline test result** | 2339 passed / 17 failed / 6 skipped — see below |

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

### Baseline test result

Established on the rebuild machine (Linux, Python 3.12.3) at commit `7ef4664`.

| | |
|---|---:|
| **Collected** | 2362 |
| **Passed** | **2339** |
| **Failed** | **17** |
| Skipped | 6 |
| Collection errors | 0 |

Dependencies were installed with `pip install --target <dir>` and put on
`PYTHONPATH`. That sidesteps both blockers this machine has — the system Python
is PEP-668 externally managed, and `python3-venv` is unavailable — without
touching the system environment. The heavy optional extras (`playwright`,
`onnxruntime`, `rapidocr`, `pdfplumber`, `trafilatura`) were **not** installed
and were not needed for collection.

**pytest did not print its final count line**; the numbers above are counted
from the per-test progress characters, which sum exactly to the 2362 collected.

#### Important caveat: no local model on this machine

`ollama` is **not installed and not reachable here**. v1 is a local-first agent,
so every test that drives the agent loop end to end cannot pass in this
environment. Several of the 17 failures show this directly in their output
(`Waiting for model response`, `Agent stopped before completing`,
`Build/verify failed ... after 0 repair attempt(s)`).

**Therefore 17 is an upper bound on v1's real defect count, not a measurement of
it.** Re-run on a GPU machine with Ollama available to separate genuine
regressions from environmental ones.

#### The 17 failures

Showing model-dependency evidence in their output — expected to fail without a
local model:

```text
tests/test_freeform_generator.py::test_freeform_regenerates_source_when_strict_repair_has_no_edit
tests/test_freeform_generator.py::test_freeform_hardens_explicit_python_cli_prd
tests/test_prd_eval_cases.py::test_prd_eval_case_scores_acceptance_commands
tests/test_real_indexed_qa.py::test_repl_workspace_prd_request_finds_single_prd_without_routing
tests/test_composite_routing.py::test_prd_acceptance_runner_records_exact_output_verdicts
tests/test_composite_routing.py::test_prd_acceptance_runner_returns_failure_diagnostics
tests/test_composite_routing.py::test_prd_conformance_checks_named_functions_and_invalid_cli
tests/test_composite_routing.py::test_composite_verify_runs_clear_script_when_model_only_describes_command
```

No model-dependency evidence visible — cause not established, worth triaging
first on a properly provisioned machine:

```text
tests/test_action_ledger_storage.py::test_diagnostics_integrity_links_command_tool_raw_log_and_packet
tests/test_build_run_2026_08_03_fixes.py::test_a_mutation_is_never_dispatched_to_web
tests/test_diagnostics_workflow_integration.py::test_command_runner_calls_diagnostic_digest_after_successful_command
tests/test_diagnostics_workflow_integration.py::test_successful_command_does_not_replace_last_error_packet
tests/test_diagnostics_workflow_integration.py::test_command_runner_digest_failure_never_breaks_command_execution
tests/test_dry_run_and_contract.py::test_contract_normalizes_absolute_workspace_target_to_relative_path
tests/test_final_safety_audit.py::test_command_output_secrets_are_redacted
tests/test_project_env.py::test_command_runner_installs_only_through_created_project_venv
tests/test_runtime_doctor.py::test_run_doctor_combines_all_checks
```

Two in the second group are worth a look regardless of environment, because of
what they cover: `test_command_output_secrets_are_redacted` (asserted
`'[REDACTED]' in ''` — the captured output was empty) and
`test_contract_normalizes_absolute_workspace_target_to_relative_path` (an
absolute temp path survived where a relative one was expected). Both may still
be environmental; neither has been triaged.

#### Reproducing

```bash
cd legacy-code
pip install --target /tmp/legacylibs \
    "mcp>=1.28,<2" "prompt_toolkit>=3.0" "rich>=13.7" "httpx>=0.27" \
    "ollama>=0.4" "pydantic>=2.7" "instructor>=1.3" "json-repair>=0.25" \
    "tokenizers>=0.21" "tree-sitter>=0.23" tree-sitter-python \
    tree-sitter-javascript tree-sitter-html "mistletoe>=1.3" "diskcache>=5.6" \
    "psutil>=5.9" "watchdog>=4.0" "PyYAML>=6.0" "keyring>=25.0" \
    "filelock>=3.15" "rank_bm25>=0.2" "yake>=0.4.8"
PYTHONPATH=/tmp/legacylibs python3 -m pytest tests -q --tb=no
```

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
