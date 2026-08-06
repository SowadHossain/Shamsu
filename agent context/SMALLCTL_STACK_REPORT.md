# SmallCTL — Stack and Architecture Report

**Subject:** `other peoples work/SmallCTL` (gitignored from this repo; its own git repo)
**Upstream:** `github.com/lowspeclabs/SmallCTL` — v0.1.4, MIT-adjacent, HEAD `2a3c78c`
**Graph project:** `home-shamsu-Shamsu-other-peoples-work-SmallCTL` — 13 515 nodes / 96 084 edges
**Author:** Claude (Opus 5)

> **Why this matters:** SmallCTL is not an adjacent tool — it is a **direct competitor
> to SHAMSU's core thesis.** Its README: *"an experimental agent harness for small
> local or self-hosted language models… so the model can handle real technical work
> with fewer runaway loops and less guesswork."* That is SHAMSU's premise, stated
> almost verbatim. This report is written for comparison, not neutral description.

---

## 1. Scale

| | SmallCTL | SHAMSU |
|---|---|---|
| Source files | **497** `.py` | ~413 `.py` |
| Source lines | **142 988** | ~50 k (est.) |
| Test files | **248** | — |
| Test lines | **99 383** | — |
| Test : source ratio | **0.70 : 1** | not measured |
| Graph nodes / edges | 13 515 / 96 084 | 161 071 / 238 995 |

The node counts invert the line counts because SHAMSU's graph is dominated by
`Variable` nodes (152 993 of 161 064). On *structural* mass — functions, classes,
call edges — SmallCTL is the larger system.

**The 99 383 lines of tests is the headline number.** SHAMSU has no comparable
ratio. Three of SmallCTL's largest test files are named for specific model
failure modes: `test_small_model_freeze_guard`, `test_tool_call_repair`,
`test_qwen_parser_and_ssh`.

---

## 2. Stack

From `pyproject.toml` (Python ≥3.10, setuptools):

| Concern | SmallCTL | SHAMSU |
|---|---|---|
| Agent loop | **`langgraph>=1.2,<2.0`** | hand-rolled `for round_index in range(...)` |
| Model API | **`openai`** — OpenAI-compatible | `ollama` native + raw `httpx` |
| Providers | generic, openai, ollama, vllm, lmstudio, openrouter, llamacpp | Ollama only |
| UI | **`textual`** (full TUI) + `rich` | `prompt_toolkit` + `rich` |
| Code editing | **`libcst`** (CST, lossless) | `tree-sitter` (read) + text diffs (write) |
| Structured output | `pydantic` | `pydantic` + `instructor` + `json-repair` |
| Query/config | `jmespath`, `pyyaml` | `PyYAML` |
| Async IO | `aiofiles` | — |
| CLI | `click` | `argparse` |
| Test runner | `pytest -n auto --dist=loadfile` (xdist) | `pytest` |
| Type checking | **`mypy`** | none declared |

### Three consequential divergences

1. **LangGraph vs hand-rolled.** SmallCTL buys checkpointing, subgraphs, and
   interrupt/resume from a framework. SHAMSU hand-rolls the loop — which is why
   its cancellation plane ended up dead code (see `AGENT_LOOP_AND_TOOLING_REPORT.md`
   §11.2); LangGraph would have provided that for free.
2. **OpenAI-compatible vs Ollama-native.** SmallCTL runs against vLLM, LM Studio,
   llama.cpp, OpenRouter, and Ollama through one interface with per-provider
   adapters. SHAMSU is Ollama-only. This is a portability gap, not a taste
   difference.
3. **libcst vs text diffs.** CST-based patching (`tools/ast_patch*.py`,
   `ast_cst_transformers.py`) survives formatting and preserves comments —
   structurally safer than SHAMSU's diff-apply path for small-model edits.

---

## 3. Architecture — 8 declared layers

From its own `AGENTS.md` §Core Architecture (a 30 KB agent-facing doc — itself
worth noting as a practice):

1. CLI + config resolution → `HarnessConfig` (flags, YAML, env, presets)
2. `Harness` — owns session state, tools, context policy, logging, approvals
3. **LangGraph runtimes** (`graph/`, 113 files) — model calls, interpretation,
   tool execution, recovery, completion
4. **Tools** (`tools/`, 116 files) — fs, shell, SSH, web, git, memory, artifact,
   control, planning
5. **Context** (`context/`, 37 files) — what evidence/memory/artifacts/summaries
   reach the model
6. **State** (`state*.py`, 12 files) — durable task/session records
7. **Safety** — `risk_policy.py`, `phases.py`, `phase_contracts.py`, `guards.py`,
   FAMA, loop guards, write sessions
8. **UI** (`ui/`) — Textual

Its guidance is explicit: *"first decide which layer owns the behavior. Most bugs
are easier to fix by changing the owning layer instead of patching symptoms in a
downstream renderer or test."*

---

## 4. Phase contracts — the biggest structural idea

`src/smallctl/phases.py`:

```python
PHASES = ("explore", "plan", "author", "execute", "verify", "repair")

@dataclass(frozen=True)
class PhaseContract:
    phase: str
    focus: str
    prompt_priority: str
    blocked_tools: tuple[str, ...] = ()
    required_handoffs: tuple[str, ...] = ()
    allow_tool_reuse: bool = True
```

Per-phase tool blocking, enforced structurally:

| Phase | Blocks |
|---|---|
| `explore` | mutation **and** shell execution |
| `plan` | mutation **and** shell execution |
| `author` | terminal task tools (can't declare done while writing) |
| `execute` | — (runs approved actions, verifies effects) |
| `verify` | writes **and** terminal task tools |
| `repair` | — (recovers from failed verify/execute) |

**This is the single most transferable idea in the codebase.** SHAMSU has an
allow-list (`_tool_is_allowed`) and a required-prefix gate, but no notion of a
*phase* that mechanically forbids mutation during exploration. SmallCTL cannot
write a file while exploring; SHAMSU relies on prompt discipline plus per-step
allow-lists.

### Six runtime modes

`chat`, `loop`, `planning`, `indexer`, `tool_plan`, `auto`. The `tool_plan` mode
is a **ReWOO** implementation: planner emits a bounded *read-only* evidence plan,
workers execute safe reads, observations are compressed, then a solver acts.
Read-only tool set is pinned in `graph/tool_plan_schema.py`.

---

## 5. Named research techniques in production

SmallCTL implements published agent patterns by name — SHAMSU implements none of
these as such:

| Technique | Location |
|---|---|
| **ReWOO** (plan → observe → solve) | `context/rewoo_lanes.py`, `graph/runtime_tool_plan.py` |
| **Reflexion** (self-reflection memory) | `harness/reflexion_service.py`, `fama/reflexion_bridge.py`, `recovery_schema.ReflectionMemory` |
| **Test-time scaling** (N variants, pick best) | `graph/test_time_scaling.py`, `graph/scaling_constants.py`, `graph/solver_refine.py` |
| **FAMA** (failure-aware mitigation) | `fama/` — 14 modules |

### Test-time scaling

`graph/scaling_constants.py` defines five `PROMPT_VARIANTS` — standard staged,
conservative, alternate-route, debug-first, minimize-risk — run as parallel
attempts and scored. A genuine inference-time compute lever SHAMSU does not have.

### FAMA — failure-aware mitigation

`fama/`: `detectors.py` (1651 ln), `detector_classifiers.py`, `judge.py`,
`router.py`, `tool_policy.py`, `fingerprints.py`, `capsules.py`, `signals.py`,
`state.py`, `runtime.py` (994 ln), `reflexion_bridge.py`.

Classifies failures into `FamaFailureKind`, fingerprints them, routes to an
`ActiveMitigation`, and can **alter tool policy in response**. SHAMSU's nearest
equivalent is `_error_signature()` in `error_feedback_loop.py` — a string
fingerprint with a repeat counter. FAMA is a full subsystem where SHAMSU has a
function.

---

## 6. Timeout architecture — finer-grained than SHAMSU's

`client/transport_constants.py`:

```python
STREAM_CONNECT_TIMEOUT_SEC                          = 10.0
STREAM_WRITE_TIMEOUT_SEC                            = 30.0
STREAM_READ_TIMEOUT_SEC                             = 120.0
STREAM_FIRST_TOKEN_TIMEOUT_SEC                      = 30.0
STREAM_POOL_TIMEOUT_SEC                             = 30.0
STREAM_TOOL_CALL_CONTINUATION_TIMEOUT_SEC           = 30.0
SMALL_MODEL_TOOL_CALL_CONTINUATION_TIMEOUT_SEC      = 12.0
LMSTUDIO_FIRST_TOKEN_TIMEOUT_SEC                    = 45.0
LMSTUDIO_TOOL_CALL_CONTINUATION_TIMEOUT_SEC         = 90.0
LMSTUDIO_SMALL_MODEL_TOOL_CALL_CONTINUATION_TIMEOUT_SEC = 135.0
```

Three axes SHAMSU does not distinguish:

1. **First-token vs steady-state.** `_next_stream_read_timeout()`
   (`client/streaming.py:109`) returns the first-token timeout while
   `chunk_count == 0`, then the normal read timeout. Separates "model is loading"
   from "model stalled mid-generation" *at the transport layer*. SHAMSU
   approximates this after the fact via `_timeout_category()`.
2. **Per-provider.** LM Studio gets 45 s vs 30 s first-token; 135 s vs 12 s
   small-model continuation. `resolve_first_token_timeout_sec()` resolves
   override → adapter policy → provider profile → default.
3. **Small-model-specific.** A distinct, *shorter* tool-call continuation timeout
   (12 s) for small models — encoding that a small model which hasn't continued a
   tool call in 12 s is stuck, not thinking.

Also present: `prompt_processing_timeout_sec` (prefill, separate from generation)
and a llama.cpp context-overflow regex that parses the backend's own error to
recover (`_LLAMACPP_CONTEXT_OVERFLOW_RE`).

**SHAMSU's tension** — a 120 s `asyncio.wait_for` masking a 180 s idle timeout —
does not arise here, because the layers are explicitly reconciled.

---

## 7. Loop guards

`graph/tool_loop_guard_constants.py`:

```python
_REPEATED_TOOL_HISTORY_LIMIT        = 24
_IDENTICAL_TOOL_CALL_STREAK_LIMIT   = 3
_REPEATED_TOOL_UNIQUE_LIMIT         = 5
_STRICT_LOOP_GUARD_IDENTICAL_LIMIT  = 3
_STRICT_LOOP_GUARD_WINDOW_LIMIT     = 6
_STRICT_LOOP_GUARD_UNIQUE_LIMIT     = 3
# directory-listing guards derived via a strictness multiplier
```

Two tiers (normal/strict) plus a *derived* relaxation for directory listing —
acknowledging that repeated `ls` is less pathological than repeated writes.
SHAMSU has one flat `_MAX_REPEATED_CALLS = 3`.

Additional guard families: `progress_guard*` (6 files, incl. an SSH-specific
variant), `hard_step_detector.py`, `escalation_triggers*`, `chat_progress_guard`,
`lifecycle_step_budget.py`.

---

## 8. Write sessions — an FSM for authoring

A reliability feature with no SHAMSU counterpart. `write_session_fsm.py` plus
~12 `graph/write_session_*` and `graph/tool_write_session_*` modules.

Rules from `AGENTS.md`:

- A `patch_existing` session requires an explicit first write choice:
  `file_patch`, `ast_patch`, or `file_write(..., replace_strategy="overwrite")`.
- **A bare `file_write` to a session-owned path is blocked** — the write must
  carry `write_session_id` and `section_name`.

Contrast with SHAMSU, where model-facing `write_file` *always overwrites*
because "small models forget an overwrite flag, get blocked, and then hallucinate
success" (`agent_tools.py`). Both teams hit the same failure; SmallCTL answered
with a state machine and explicit strategy selection, SHAMSU with a permissive
default. **SmallCTL's answer is stronger, and it is the more instructive
disagreement in this report.**

---

## 9. Evidence and artifacts

First-class durable records (`state.py`): `EvidenceRecord`, `ArtifactRecord`,
`DecisionRecord`, `ExperienceMemory`, plus `SubtaskLedger` and `FailureEvent`
(`recovery_schema.py`).

Tool output becomes a tracked **artifact** with a lifecycle:
`harness/artifact_tracking.py`, `artifact_read_ledger.py`,
`tool_result_artifact_lifecycle.py`, `tool_result_artifact_updates.py`,
`context/artifact_read_coverage.py`, `context/artifact_visibility.py`,
`graph/tool_artifact_recovery.py`.

The read ledger tracks *which artifacts the model has actually read* — so context
assembly can avoid re-sending what was already consumed, and completion gates can
refuse a "done" claim citing unread evidence.

SHAMSU's `ActionLedger` is a **write-only audit trail** — its docstring states
"nothing here is ever fed back into a model prompt." SmallCTL's equivalent is
**bidirectional**: evidence flows back into context assembly. Different by
design, and SHAMSU's choice is defensible (audit purity), but it forgoes reuse.

### Context assembly

`context/assembler.py` (1826 ln) + `retrieval.py` (1416 ln) + `frame*.py` (8
files) + `tiers.py`, `policy.py`, `summarizer.py`, `step_sandbox.py`,
`subtasks.py`. A compiled "prompt state frame" with phase-aware rendering and
invalidation filtering — considerably more machinery than SHAMSU's
`_messages_within_budget()` + rolling summary.

---

## 10. Verification

`harness/` contains **~20 `tool_result_verification_*` modules**: `assess`,
`audit`, `blocker`, `readback`, `removal`, `repair`, `semantic`,
`ssh_recovery`, `store`, `timeout`, `artifact`, `constants`, `helpers`, plus
`verifier_staleness` and `verifier_monitor.py`.

Notable: **readback** (re-read what was written to confirm), **staleness** (a
verifier result can go stale and be rejected), **blocker** (verification can
block completion). `tools/control_task_complete_gates.py` (1089 ln) is dedicated
to gating terminal "task complete" claims.

SHAMSU's `verify/` (3282 ln, 9 files) is a real subsystem but smaller, and its
verify gate runs **once, at end of run, autonomous mode only**. SmallCTL verifies
continuously and can revoke a stale verdict.

---

## 11. What SHAMSU has that SmallCTL does not

Not a one-way comparison:

- **Structural code graph.** SHAMSU delegates retrieval to codebase-memory-mcp
  (tree-sitter, 159 languages, call graphs, impact analysis). SmallCTL's
  `search_server/` and `runtime_indexer` are text/embedding-oriented; nothing in
  it corresponds to `trace_path` or `search_graph`.
- **PRD → project generation.** `shamsu/prd/` (290 nodes) + Django templates +
  scaffold pipeline. SmallCTL is a task harness; it does not generate projects
  from a spec.
- **Reliability telemetry as a product surface.** `telemetry/reliability.py`
  computes `false_success_rate`, `first_pass_verified_rate`,
  `success_without_verification_rate` over durable run artifacts. SmallCTL has
  `recovery_metrics.py` (counters) but no comparable aggregate report.
- **Deterministic eval harness with pass-rate tables** — `evals/harness.py`
  scores against the real request path, never self-report. SmallCTL has
  `evals/tool_plan` and `evals/test_time_scaling` fixtures; I did not verify
  whether they produce a comparable gating report.
- **Graphiti temporal memory** with a SQLite floor. SmallCTL's memory is
  `ExperienceStore` (JSON, namespaced, tagged) + `memory/taxonomy.py` — simpler.

---

## 12. Recommendations

Ranked by value-to-effort:

1. **Adopt phase contracts.** `PHASES` + `PhaseContract` with `blocked_tools` is
   ~100 lines and would give SHAMSU a structural guarantee it currently gets from
   prompt text. Highest ratio in this report.
2. **Split first-token from steady-state timeout at the transport layer.** SHAMSU
   already *classifies* this after the fact (`_timeout_category`); enforcing it in
   `manager.py` would also resolve the 120 s/180 s layering tension.
3. **Reconsider `write_file`'s always-overwrite default** against SmallCTL's
   write-session FSM. Both teams hit the same small-model failure; SmallCTL's
   answer preserves safety.
4. **Feed evidence back into context.** SHAMSU's ledger is deliberately
   write-only; an artifact read-ledger would let context assembly skip
   already-consumed evidence — directly relevant to its token-efficiency goal.
5. **Evaluate LangGraph** for the loop rewrite, if one is ever on the table. It
   would have prevented the dead-cancellation-plane defect.
6. **Raise the test ratio.** 0.70 : 1, with tests named for specific model failure
   modes, is the discipline behind SmallCTL's reliability claims.

---

## 13. Method and limits

- Indexed as a **separate** graph project — deliberately not merged with
  `home-shamsu-Shamsu`, so queries stay unambiguous. Query with
  `--project home-shamsu-Shamsu-other-peoples-work-SmallCTL`.
- **Read directly:** `pyproject.toml`, `README.md`, `phases.py`,
  `client/transport_constants.py`, `graph/tool_loop_guard_constants.py`,
  `graph/scaling_constants.py`, `AGENTS.md` §§Core Architecture / Runtime Modes /
  Write Sessions, package `__init__` docstrings, several module headers.
- **Structure only** (file listings, graph overview, names): `graph/` (113),
  `harness/` (96), `tools/` (116), `context/` (37).
- **Not read:** any implementation body over ~40 lines. `interpret_nodes.py`
  (1910 ln), `context/assembler.py` (1826 ln), `fama/detectors.py` (1651 ln),
  `graph/model_stream_loop.py` (1314 ln), and `harness/core_facade.py` (1274 ln)
  are **uninspected**.

**Therefore:** §§2–4 and 6–7 rest on constants and declared contracts and are
solid. §§5, 9, 10 are inferred substantially **from module names and AGENTS.md
prose**, not from reading the code — a module named
`tool_result_verification_staleness.py` is strong evidence that staleness is
handled, but I have not confirmed *how well*. Treat capability claims there as
"present and structured", not "verified working".

I also have no runtime evidence: nothing here was executed, and no benchmark
compares the two systems' actual task success.
