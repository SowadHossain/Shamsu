# SHAMSU vs SmallCTL — Dimension-by-Dimension

**SHAMSU:** commit `7075067`, branch `mayday-lastresort`
**SmallCTL:** `other peoples work/SmallCTL`, HEAD `2a3c78c`, v0.1.4
**Companion docs:** `AGENT_LOOP_AND_TOOLING_REPORT.md` (SHAMSU internals),
`SMALLCTL_STACK_REPORT.md` (SmallCTL overview)

> Verification note: for this pass I read implementation in SmallCTL's
> `prompts.py`, `risk_policy.py`, `reasoning_policy.py`, `guards.py`,
> `context/policy.py`, `context/tiers.py`, `graph/tool_dag.py`,
> `graph/tool_plan_schema.py`, `tools/dispatcher.py`, `memory/taxonomy.py`,
> `recovery_metrics.py`, `logging_utils.py`, and `aho/*`. That closes most of the
> "names only" gap flagged in §13 of the previous report. Remaining gaps in §12.

---

## 0. Scoreboard

| Dimension | Winner | Margin |
|---|---|---|
| Chat loop | SmallCTL | large — framework vs hand-rolled |
| Safety loop | SmallCTL | large — phase contracts + claim gate |
| Planning | SmallCTL | moderate — DAG + spec contract |
| Tool calling | SmallCTL | moderate — DAG parallelism, repair layer |
| Context gathering | **SHAMSU** | moderate — real code graph |
| Context engineering | SmallCTL | large — ~35 tuned budgets, tiering |
| Context construct | SmallCTL | large — compiled frame vs message list |
| System prompts | SmallCTL | large — per-model-family composition |
| Memory | **SHAMSU** | moderate — temporal graph + floor |
| LLMOps / evals | **split** | SHAMSU: reliability metrics. SmallCTL: self-optimizing harness |

SHAMSU wins on **retrieval and memory substrate**. SmallCTL wins on **everything
between the prompt and the tool result**.

---

## 1. Chat loop

**SHAMSU** — `agents/chat_loop.py`, one function, 3546 lines.
`for round_index in range(self.max_tool_rounds)` (8 / 50 long-running). ~20
mutable guard counters tracked inline in the loop body. No checkpointing, no
resume, **no cancellation** (`run_control.py` is dead code — see the loop report
§11.2).

**SmallCTL** — LangGraph. Five runtime classes (`AutoGraphRuntime`,
`ChatGraphRuntime`, `LoopGraphRuntime`, `ToolPlanRuntime`, `ChildSubgraphRunner`)
over 113 modules in `graph/`. Node-based: `model_call_nodes`, `interpret_nodes`,
`tool_execution_nodes`, `lifecycle_nodes`, plus `checkpoint.py`, `interrupts.py`,
`cancel_result.py`, `autocontinue.py`, `subgraphs.py`.

`GuardConfig` (`guards.py`):
```python
max_steps: int = 50
max_consecutive_errors: int = 8
max_repeated_actions: int = 6
max_tokens: int | None = None
```

**Difference that matters:** SHAMSU's loop state is *local variables in one
function*. SmallCTL's is a *typed graph state object* that can be checkpointed,
interrupted, resumed, and spawned as a child subgraph. That is why SmallCTL has
working cancel/interrupt and SHAMSU's died on the vine — SHAMSU had to hand-build
what LangGraph provides.

**Consecutive-error ceiling (8)** is a stop condition SHAMSU lacks entirely.

---

## 2. Safety loop

**SHAMSU** — linear, per-command:
`classify_command` → BLOCKED (126) / MEDIUM → `ApprovalManager.ask` → execute
with 120 s timeout → `DiagnosticDigest` on non-zero. Plus `Sandbox.validate`
(fan-in 75) and `_looks_like_arbitrary_python`. Tool gating is a per-step
allow-list + required-prefix.

**SmallCTL** — multi-dimensional. Six independent gates:

1. **Phase contract** (`phases.py`) — `explore`/`plan` block mutation **and**
   shell; `verify` blocks writes and terminal tools; `author` blocks terminal
   tools. `_READ_ONLY_PHASES = {"explore", "verify"}` in `risk_policy.py`.
2. **Risk policy** (`risk_policy.py`) — `RiskPolicyDecision(allowed,
   requires_approval, reason, proof_bundle, tool_risk, task_classification,
   approval_kind)`. Risk is a function of *(tool, phase, task classification)*,
   not the command string alone.
3. **Claim gate** (`reasoning_policy.py`) — `ClaimGateResult`, `ClaimRecord`,
   `task_requires_claim_support()`, `has_supported_claim()`,
   `build_claim_proof_bundle()`. For diagnosis/remediation tasks the model
   **cannot assert a conclusion without registered supporting evidence.**
4. **Dispatcher guards** (`tools/dispatcher_policy_guards.py`) —
   `_fama_dispatch_block`, `_staged_tool_allowlist_error`,
   `_verifier_loop_dispatch_block`.
5. **Loop guards** — two tiers plus a directory-listing relaxation multiplier.
6. **Write-session FSM** — bare `file_write` to a session-owned path is blocked.

**The claim gate is the sharpest idea here.** SHAMSU fights hallucinated success
*reactively*: `_FALSE_FAILURE_RE`, `_MUTATION_PROMISE_RE`, `unconfirmed_failed_writes`,
the end-of-run verify gate — all detecting a false claim after it's made.
SmallCTL makes the claim **structurally impossible to register** without proof.

`_RISKY_TOOL_NAMES` is shared vocabulary across risk policy, scaling constants,
and tool-plan blocklist — one definition of "dangerous", reused. SHAMSU
duplicates `_MUTATION_TOOL_NAMES` twice inside `chat_loop.py` alone (`:304`,
`:429`).

---

## 3. Planning

**SHAMSU** — `agents/planner.py`. One planner call before each mutating
workflow, schema-constrained JSON (was free prose). Carries an "is this the
user's call?" flag. The docstring is candid about why: a prompt-only nudge to
`ask_user` measurably failed to make a 7B model ask — the
`ask_before_choosing_an_approach` eval stayed red — because "mid-loop, a model
that can always do *something* just does it." Plus `plan_mode.py` for reviewable
user-facing plans.

**SmallCTL** — `ExecutionPlan` / `PlanStep` (`state.py`) with a **spec contract**:
`inputs`, `outputs`, `constraints`, `acceptance_criteria`, `implementation_plan`,
`claim_refs`. Rendered two ways — canonical export and a *playbook* that
"tells the model how to proceed in small, bounded steps instead of trying to
complete the whole script in one shot."

Plan execution is separate (`graph/plan_execution.py`) from plan verification
(`graph/plan_verification.py`, with `StepCompletionGate` and
`compact_step_evidence`).

**Difference:** SHAMSU's plan is *advice folded into the next prompt*.
SmallCTL's plan is *a contract with acceptance criteria that a gate checks step
by step*, and `claim_refs` ties plan steps back to evidence.

Both teams independently discovered the same thing — that asking upfront beats
nudging mid-loop. SHAMSU documents it with eval evidence; that reasoning is worth
keeping.

---

## 4. Tool calling

| | SHAMSU | SmallCTL |
|---|---|---|
| Surface | 42 tools, flat | ~116 modules; local + **SSH/remote** + web + git + artifact + control |
| Execution | strictly serial | **DAG, parallel batches** (`graph/tool_dag.py`) |
| Parse | native → 5-stage salvage cascade | native → `tool_call_parser` + `tool_inline_parsing` + `tool_call_repair` |
| Validation | type + required + unexpected-key | `validate_tool_args` + `ToolCallValidationIssue` + coercion, **risk-tiered strictness** |
| Failure | `ToolResult(ok=False)` | `ToolEnvelope` + outcome resolution + recovery modules |

**DAG execution** — `build_execution_dag()` topologically sorts steps into
batches that run in parallel, with an explicit cycle fallback to serial order.
`PARALLELIZABLE_TOOL_PLAN_TOOLS` is a 14-tool read-only frozenset.
SHAMSU executes tools one at a time, always.

**Risk-tiered argument strictness** (`tools/dispatcher.py`): observe-only tools
surface unknown arguments as a *visible warning*; everything else hard-rejects.
SHAMSU rejects any unexpected argument uniformly — stricter, but it burns a turn
on a harmless extra key from a small model.

**Recovery depth:** SmallCTL has `tool_execution_recovery.py` (1026 ln),
`tool_artifact_recovery`, `write_recovery`, `tool_call_repair`,
`recovery_coercion.py`, `runtime_error_repair.py`. SHAMSU's equivalent is inline
guard counters in the chat loop.

---

## 5. Context gathering — **SHAMSU wins**

**SHAMSU** — codebase-memory-mcp is the sole search backend: tree-sitter across
159 languages, `search_graph`, `trace_path`, `get_code_snippet`, impact analysis,
Cypher. Plus a last-resort local-embedding semantic rescue
(`nomic-embed-text`, file-level, JSON index, `SHAMSU_SEMANTIC_SEARCH=0` to
disable).

**SmallCTL** — `search_server/`, `runtime_indexer`, `context/retrieval.py`
(1416 ln) with `retrieval_scoring`, `retrieval_query`, `retrieval_constants`, and
`retrieval_safety.py`. Text/embedding-oriented. **Nothing corresponds to
`trace_path` or `search_graph`.** `aho/similarity_retriever.py` is embedding
similarity.

**Verdict:** SHAMSU's is a genuine structural code graph; SmallCTL's is search.
For "what calls this function" or "what breaks if I change this", SHAMSU is
categorically better. This is SHAMSU's clearest architectural advantage and it
should be defended, not traded away.

SmallCTL does have `retrieval_safety.py` — a notion of retrieval being *unsafe*
(noise poisoning context) that SHAMSU lacks. Its failure taxonomy names
`RETRIEVAL_NOISE` explicitly.

---

## 6. Context engineering — SmallCTL, decisively

**SHAMSU** — `context/budget.py` (87 ln) + `manager.py` (300 ln) +
`builder.py` (111 ln). Four knobs:
```python
TOTAL_BUDGET_DEFAULT    = 6000
PER_HOLE_BUDGET_DEFAULT = 3500
CHARS_PER_TOKEN_ESTIMATE = 4
SAFE_FALLBACK_CTX_WINDOW = 8192
```
Plus `_CHAT_MAX_CTX = 12288`, `_TOOL_RESULT_MAX_TOKENS = 2000`,
`_CHAT_SUMMARY_BUDGET_TOKENS = 512`, `_CHAT_PROMPT_TARGET_FRACTION = 0.70`.
Real tokenizer (`qwen3-tokenizer.json`) — an accuracy advantage.

**SmallCTL** — `ContextPolicy` (`context/policy.py`) is a single dataclass with
**~35 independently tunable budgets**, including:

```python
swa_prompt_cap                     = 12288   # sliding-window-attention cap
reserve_completion_tokens          = 1024
reserve_tool_tokens                = 512
summarize_at_ratio                 = 0.8
transcript_token_limit             = 1400
run_brief_token_limit              = 240
working_memory_token_limit         = 640
episodic_summary_token_limit       = 320
artifact_snippet_token_limit       = 400
artifact_summarization_threshold   = 1560
tool_result_inline_token_limit     = 325
artifact_read_inline_token_limit   = 1024
file_read_preview_line_limit       = 300
memory_staleness_step_limit        = 8
memory_low_confidence_threshold    = 0.6
hot_message_limit                  = 8
warm_brief_limit                   = 3
warm_tier_token_budget             = 400
observation_token_limit            = 768
observation_token_floor            = 2048
fresh_tool_output_token_limit      = 1200
```

Every *kind* of content has its own ceiling. SHAMSU has one budget for history
and one cap for tool results.

**Token estimation is more careful than SHAMSU's**, despite lacking a tokenizer:
```python
# 0.4 tok/char for ASCII; non-ASCII charged at 1.0
# "CJK, emoji, many accented scripts tokenize at roughly one token per
#  character, so charge them at 1.0 to keep every downstream budget honest"
```
SHAMSU's fallback is a flat `CHARS_PER_TOKEN_ESTIMATE = 4` — which
**under-counts CJK by ~4×**. SHAMSU's real tokenizer covers this on the main
path; the fallback does not.

`swa_prompt_cap` shows awareness of sliding-window-attention models — a class of
model constraint SHAMSU's budget code doesn't model.

---

## 7. Context construct — different in kind

**SHAMSU** — `_messages_within_budget()` returns a **list of chat messages**.
Trim oldest-first, keep the most recent, prepend a rolling summary
(512-token budget), hard-trim marker when needed. Linear.

**SmallCTL** — a compiled **prompt state frame**:
- `context/frame.py`, `frame_compiler.py`, and six `frame_*_rendering.py`
  (phase / recovery / run / session / working-memory)
- `frame_invalidation_filtering.py`, `frame_invalidation_utils.py`
- `assembler.py` (1826 ln) + `assembler_rendering.py`
- `MessageTierManager` (`tiers.py`) — **hot / warm / cold** with explicit
  demotion events recorded to `state.scratchpad["_compaction_demotion_events"]`
  (last 24 retained)
- `rewoo_lanes.py` — lane-based partitioning with `PromptStateDrop`
- `observations.py` → `ObservationPacket`
- `step_sandbox.py`, `subtasks.py`, `messages_next_steps.py`,
  `messages_reused_artifacts.py`

**The structural difference:** SHAMSU builds a *transcript*. SmallCTL compiles a
*state document* — phase, run brief, working memory, episodic summaries,
observations, artifacts, reused-artifact notes, next steps — each independently
budgeted, tiered, and invalidatable.

Invalidation is the piece SHAMSU has no analogue for:
`tool_result_context_invalidation.py` + `frame_invalidation_filtering.py` mean a
tool result can *revoke* previously-assembled context that it contradicts.

---

## 8. System prompts

**SHAMSU** — one `AGENT_SYSTEM_PROMPT` (~90 lines, `chat_loop.py:146`) plus
`_TOOL_PROTOCOL_PROMPT`, `_RAW_WRITE_PROTOCOL_PROMPT`, and correction snippets.
Written defensively and specifically — the `read_file`-returns-candidates rule,
the "path from a traceback may not be the real path" rule, and the whole-file-write
preference are all hard-won and genuinely good. But **one prompt for all models**,
refreshed per round via `_refresh_system_prompt()`.

**SmallCTL** — `prompts.py` (841 ln) + `prompt_fragments.py` (319 ln) +
`prompts_support.py` + `graph/lifecycle_prompt.py` (414 ln). **Composed, not
written.** ~40 named fragments assembled per call:

```
_CONTRACT_PHASE_FOCUS_SMALL / _CONTRACT_PHASE_FOCUS_LARGE
_TOOL_CALL_FORMAT_JSON / _TERMINAL / _TERMINAL_SAME_TURN
_RESPONSE_STRUCTURE_GEMMA / _SMALL_GEMMA / _THINK
_GEMMA_4_STRICT_FORMAT, _SMALL_GEMMA_STRICT_FORMAT, _LFM_25_8B_STRICT_FORMAT
_LARGE_GEMMA_26B_ANTI_LOOP_RULE
_EVIDENCE_ANCHORED_DIAGNOSIS_RULE, _REFLECTION_GATE
_PATCH_VERBATIM_RULE, _TARGET_FILE_READ_ONCE, _SECRET_HANDLING
_STDERR_CIRCUIT_BREAKER_PREFIX, _META_COGNITIVE_REPAIR_BRIEF
_INSTALLER_TIMEOUT_RECOVERY, _REMOTE_PROBES_BATCH, _PRIVILEGES_NO_SUDO_GUESS
```

Selection is driven by model classifiers: `is_small_model_name`,
`is_seven_b_or_under_model_name`, `is_over_twenty_b_model_name`,
`is_gemma_model_name`, `is_exact_small_gemma_4_it_model_name`,
`is_exact_large_gemma_4_26b_a4b_it_model_name` — **plus the active phase
contract.**

**Difference:** a Gemma-4 4B in `explore` phase and a 26B in `author` phase get
materially different prompts. SHAMSU sends the same text to `gemma3:4b` and
`qwen2.5-coder:14b`. Given SHAMSU already has a model cookbook with per-role
temperatures, per-family prompt fragments are a natural and cheap extension.

An anti-loop rule *named for a specific 26B model* is what maintaining this
looks like in practice — that is real operational scar tissue.

---

## 9. Memory — **SHAMSU wins on substrate**

**SHAMSU** — two-tier:
- **Graphiti** (external, temporal knowledge graph, entity/edge extraction via
  local LLM — hence `WRITE_CALL_TIMEOUT_SECONDS = 300` vs 120 read)
- **SQLite floor** (`sqlite_store.py`) — always available. Its docstring is the
  best design note in the SHAMSU codebase: requiring Graphiti to start "blocks
  SHAMSU on the exact low-resource machines it targets."
- Bounded mirror queue (64 deep, 1.5 s flush), 7 memory kinds, write policy with
  secret patterns / explicit markers / transient-noise rejection / 1200-char cap

**SmallCTL** — `ExperienceStore` (`memory_store.py`): JSON, thread-locked, atomic
temp-file writes, namespaced (`memory_namespace.py`), tagged
(`experience_tags.py`), redacted. Plus `state_memory.py`, `harness/memory.py`,
`memory_cli.py`.

Where SmallCTL is ahead: **`memory/taxonomy.py`** defines 11 named failure modes
as the memory schema —

```
tool_not_called, wrong_tool_called, schema_validation_error,
zero_arg_tool_arg_leak, repeated_tool_loop, premature_task_complete,
phase_mismatch, retrieval_noise, stale_memory_applied,
environment_mismatch, unknown_failure
```

with `normalize_failure_mode()` classifying raw errors into them. Memory is
organized around *how the agent fails*. SHAMSU's kinds (`user_preference`,
`project_decision`, `bug_lesson`, …) are organized around *what the content is*.

`ContextPolicy` also carries `memory_staleness_step_limit = 8` and
`memory_low_confidence_threshold = 0.6` — memory ages out and is confidence-scored.
SHAMSU has `STALE_MEMORY_APPLIED` as neither a concept nor a guard.

**Verdict:** SHAMSU's substrate (temporal graph + guaranteed floor) is stronger.
SmallCTL's *schema* is smarter. These are independently adoptable — SHAMSU could
add a failure taxonomy and staleness scoring without touching Graphiti.

---

## 10. LLMOps and eval loops — split, and the most interesting section

**SHAMSU:**
- `evals/harness.py` — deterministic scoring, **explicitly never self-report**,
  throwaway workspaces, DI driver, BENCHMARK-style pass-rate tables,
  `python -m evals` exits non-zero to gate CI-lite
- `ActionLedger` — durable per-run evidence tree; redaction at three write
  funnels as the single "no secrets on disk" enforcement point
- `telemetry/reliability.py` — read-only aggregation computing
  `apply_success_rate`, `verification_pass_rate`, `first_pass_verified_rate`,
  `repair_success_rate`, **`false_success_rate`**,
  `success_without_verification_rate`, `tool_pressure_rate`,
  `tool_truncation_rate`, with failure-class ranking

**SmallCTL:**
- `logging_utils.py` — `EVENT_SCHEMA_VERSION = 1`, per-subsystem debug
  (`client`, `graph`, `tools`, `context`, `fama`, `ui`, `memory`), run logger with
  synthetic trace IDs
- `recovery_metrics.py` — in-state counters and buckets
- **`aho/` — "Agentic Harness Optimizer", 51 files.** A recursive
  self-improvement loop explicitly modeled on Karpathy's `autoresearch`:

  ```
  S = (W1 × Accuracy) + (W2 × Format_Adherence) − (W3 × Token_Latency_Penalty)
  ```

  `researcher.py` is "the recursive improvement loop… analogous to the LOOP
  FOREVER section of autoresearch's program.md." An LLM proposes mutations to
  `harness_config.json` (`mutation.py`); trials are scored (`eval.py`); **kept
  strategies are git-committed, discarded ones `git checkout --` reverted, so
  the git log *is* the experiment history**; `results.jsonl` records the short
  hash. A SHA-256 digest of `src/smallctl/` + `aho/*.py` detects harness code
  changes between iterations.

  Supporting cast: `challenge_loop.py`, `harness_runner.py`, `run_baseline.py`,
  `mock_client.py`/`mock_tools.py` (model-free testing),
  `context_optimizer{,_v2}.py`, `fact_extractor.py`/`fact_validator.py`,
  `tool_deduplication.py`, `visualizer.py`, `openrouter_proxy.py`,
  `bug_tracker.jsonl`, timestamped `challenge_summary_*.json` with
  `baseline_mean_pass_at_n` and `baseline_mean_harness_score`.

**Verdict — genuinely split:**

- **SHAMSU's metrics are better.** `false_success_rate` and
  `success_without_verification_rate` measure *honesty*. AHO's score measures
  accuracy, format adherence, and latency — nothing about hallucinated success.
  SHAMSU is measuring the thing that actually matters for small models.
- **SmallCTL's loop is better.** SHAMSU's evals *report*; a human reads the table
  and changes the prompt. AHO *closes the loop* — propose, score, keep-or-revert,
  commit, repeat.

Combining them is the highest-leverage idea in this document: **AHO's optimization
loop scored on SHAMSU's honesty metrics.** Neither project currently has that.

Caveat: the newest `challenge_summary_*.json` files are dated 2026-05-23, ~2.5
months before SmallCTL's HEAD. AHO may be dormant. Its `harness_config.json`
does not exist in the tree — so I could not verify the loop currently runs.

---

## 11. What SHAMSU should take, ranked

1. **Phase contracts** (§2) — ~100 lines, structural, biggest win available.
2. **Claim gate** (§2) — make unsupported conclusions unregistrable rather than
   detecting them after the fact. Directly targets SHAMSU's stated #1 problem.
3. **Per-model prompt fragments** (§8) — SHAMSU already has a model cookbook with
   per-role temperatures; add per-family fragments. Cheap.
4. **Failure taxonomy for memory** (§9) — 11 named modes; independent of Graphiti.
5. **Split the tool-result budget by content kind** (§6) — one cap for every
   result is coarse.
6. **Non-ASCII token estimation** (§6) — SHAMSU's flat `/4` fallback under-counts
   CJK ~4×.
7. **Consecutive-error ceiling** (§1) — `max_consecutive_errors = 8`; SHAMSU has
   no such stop.
8. **Context invalidation** (§7) — let a tool result revoke contradicted context.
9. **Close the eval loop with AHO's pattern, scored on SHAMSU's metrics** (§10).

## What SHAMSU should NOT copy

- **Its module explosion.** 113 files in `graph/`, 96 in `harness/`, with names
  like `tool_result_verification_ssh_recovery.py` and
  `state_flow_failure_semantics.py`. `harness/` alone has ~20
  `tool_result_verification_*` modules. SHAMSU's 3546-line `chat_loop.py` is too
  monolithic; this is the opposite failure.
- **Abandoning the code graph** (§5) — SHAMSU's clearest advantage.
- **`aho/` sprawl** — 51 files, results committed into the repo, apparently
  dormant. Take the *pattern*, not the implementation.

---

## 12. Remaining verification gaps

Still unread (bodies, not headers): `graph/interpret_nodes.py` (1910),
`context/assembler.py` (1826), `fama/detectors.py` (1651),
`graph/model_stream_loop.py` (1314), `graph/lifecycle_nodes.py` (1301),
`harness/core_facade.py` (1274), `ui/bubbles.py` (1557).

So: claims about **what mechanisms exist and how they are configured** are
verified from constants, dataclasses, and docstrings. Claims about **how well
they work** are not — I have no runtime evidence for either system, and no
benchmark comparing task success. SmallCTL's 0.70:1 test ratio is suggestive of
quality, not proof of it.

One concrete staleness finding: SmallCTL's own `AGENTS.md` §Evals references
`evals/tool_plan` and `evals/test_time_scaling`, but **no top-level `evals/`
directory exists** in the tree. Their agent doc has drifted from their code —
the same class of drift found in SHAMSU's README §Retrieval.

---

# ADDENDUM — dimensions §§1–10 did not cover

§§1–10 answered ten named dimensions. They are **not** the full difference set.
Fourteen more follow, several material. Two change the ranking in §11.

## A. Model escalation — SmallCTL only, and it is major

**This is the largest single capability gap found, and §§1–10 missed it entirely.**

SmallCTL escalates **to a bigger model mid-task**:
`harness/escalation_{config,packet,policy,response,service}.py` — **1419 lines**
across 5 modules, exposed to the model as the `escalate_to_bigger_model` tool
(`tools/control.py:87`).

`EscalationPolicy.can_escalate()` gates on: `escalation_enabled`,
`escalation_max_per_task` (default 3), `escalation_cooldown_turns` (default 1),
and evidence — `EscalationPolicyDecision` carries `evidence_count` and
`missing_signals`. Escalation history lives in `state.scratchpad["_escalation_history"]`.

**SHAMSU's `_RetryEscalation` is a different thing entirely.** It escalates
*sampling temperature* on the **same model** when output repeats byte-identically
(`chat_loop.py:319-331`). Its docstring is one of the best in the repo — "at
temperature 0.1 an unchanged prompt reproduces byte-identical output… 2026-08-03
burned all three mutation rounds on the same broken call, byte for byte" — but it
is a sampling fix, not a capability fix.

SHAMSU **has model tiers** (`runtime/models.py`, `TIER_MODEL_SPECS`) and
per-role model assignment. What it lacks is *promoting a stuck task to a larger
tier at runtime*. The models are already configured; the routing already exists.
This is closer to reach than its absence suggests.

**Ranking impact:** this belongs at #2 in §11, above per-model prompts.

## B. Remote / SSH execution — SmallCTL only

17 modules: `ssh_files.py`, `ssh_files_mutation_tracking`,
`ssh_files_patch_utils`, `ssh_files_preconditions`, `ssh_parsing`,
`network_ssh_helpers`, `network_interactive_sessions`,
`network_installer_preflight`, `dispatcher_ssh_{auth,context,memory,recovery}`,
`dispatcher_remote_{detection,paths}`, `control_remote_mutation`,
`remote_scope.py`, `tools/network.py` (956 ln).

Tools: `ssh_exec`, `ssh_file_read`, `ssh_file_write`, `ssh_file_patch`,
`ssh_file_replace_between`, `ssh_dir_list`. `remote_scope.py` detects remote
intent from the task text (absolute-path, IPv4, and `user@host` regexes) and
scopes the run accordingly. There is SSH-specific memory, auth recovery, and a
dedicated `progress_guard_ssh.py`.

**SHAMSU has zero SSH** — `grep -rln ssh shamsu/` returns nothing.

This is a **scope difference, not a quality gap**: SmallCTL targets remote
diagnosis and ops; SHAMSU targets one local workspace. Worth stating explicitly
because it explains much of SmallCTL's module count, and adopting its patterns
wholesale would import complexity SHAMSU has no use for.

## C. Checkpointing and resume — different mechanisms

**SmallCTL** — `graph/checkpoint.py` (**941 ln**) implements LangGraph's
`BaseCheckpointSaver` (`langgraph.checkpoint.base`, `RunnableConfig`,
`ChannelVersions`, `WRITES_IDX_MAP`). Durable **graph state** checkpointing:
a run can be suspended and resumed with execution state intact.

**SHAMSU** — `SessionManager.create_session` / `resume_session` / `export_session`
(`session/manager.py`, 1350 ln). Resumes a **conversation** from a logged event
stream — not loop execution state. Different guarantee: SHAMSU can restore what
was said; SmallCTL can restore where the graph was.

## D. Interrupts and human-in-the-loop

**SmallCTL** — `graph/interrupts.py` + `interrupt_replies.py` +
`harness/approvals.py` (async `asyncio.Future` resolution, `UIEvent`-driven) +
the `ask_human` control tool + `graph/cancel_result.py` + `autocontinue.py`.
Approval is **non-blocking**: a future is created, the UI resolves it.

**SHAMSU** — `ask_user` tool + `ApprovalManager.ask()` + `safety/approval.py`
with `_MAX_EMPTY_TTY_READS = 3`. **Blocking TTY read.** Works for a REPL; would
not survive a GUI or a daemon.

## E. Config presets and provider profiles — SmallCTL only

`presets.py` defines named bundles — `safe-small-model`, `coding-local`,
`lmstudio-small-model` — each setting `provider_profile`, `reasoning_mode`,
`max_prompt_tokens`, `reserve_completion_tokens`, `reserve_tool_tokens`,
`first_token_timeout_sec`. Plus `provider_profiles.py`, `config_projection.py`,
`config_support.py` (720 ln), and a `.smallctl.yaml` + `.env` layering.

**SHAMSU configures by environment variable** — ~20 `SHAMSU_*` vars scattered
across modules, no named bundles, no YAML. A `safe-small-model` equivalent would
be a genuine UX win given how many knobs `chat_loop.py` exposes.

## F. Web/search architecture — different shapes

**SmallCTL** — `search_server/` is a **subsystem**: `app.py`, `providers.py`,
`provider_base.py`, `fetch.py`, `extract.py`, `cache.py`, **`citations.py`**,
`security.py`, `models.py`, `config.py`. Pluggable providers with citation
tracking.

**SHAMSU** — `tools/web.py` (1401 ln) + `trafilatura` + permission-gated
auto-lookup. No provider abstraction, no citations module.

SHAMSU has something SmallCTL lacks entirely: **browser automation** —
`tools/browser.py` (Playwright), `tools/dev_server.py`, `/browse` for local app
preview and debugging. For "does the generated app actually render", SHAMSU can
check and SmallCTL cannot.

## G. Project generation — SHAMSU only

Absent from SmallCTL entirely: `prd/` (290 nodes — Markdown/PDF/OCR parsing,
entity extraction, `ProjectSpec`, requirement ledgers), `templates/` (django,
frontend, game-2d, multiplayer-game), `registry/` (`blueprints`, `detector`,
`scaffold`, `stack_policy`, `suitability`, `categories`), `agents/full_pipeline`,
`scaffold_pipeline`, `freeform_generator` (3911 ln).

SmallCTL is a **task harness**. SHAMSU is a task harness **plus a project
generator**. Roughly a third of SHAMSU's differentiated surface has no
counterpart to compare against.

## H. Skills — SHAMSU only

`skills/` — `loader.py`, `selector.py` (`DEFAULT_MAX_SKILLS = 5`), `ingest.py`
(`MAX_REFERENCE_SOURCE_CHARS = 14_000`), `types.py`, `bundled/`, plus
`MAX_SKILL_INSTRUCTION_CHARS = 20_000`. Ingest external references as reusable
skill documents, selected per request. No SmallCTL equivalent.

## I. Concurrency — SmallCTL parallel, SHAMSU serial

SmallCTL: parallel DAG batches (`tool_dag.py`), async throughout, a dedicated
`_AsyncRuntime` thread, `pytest -n auto --dist=loadfile`.

SHAMSU: **fully serial.** The only `concurrent.futures` import in the package is
the MCP manager's sync↔async bridge. Every tool call, every workflow step, one
at a time.

## J. Type checking

SmallCTL declares `mypy` in dev deps. SHAMSU declares `ruff` only — **no type
checker**, on a 3546-line loop with ~20 mutable counters and `Any`-typed
`control` parameters (`tool_calling_loop.py:153`, `:212`). Cheap to add,
disproportionate value at that size.

## K. Docker awareness — SmallCTL only

`docker_retry_normalization.py`, `_DOCKER_INSPECT_HINT` prompt fragment,
container-aware retry normalization. SHAMSU has none.

## L. UI

SmallCTL: 18-module Textual app (`app.py`, `app_flow*`, `bubbles.py` 1557 ln,
`approval.py`, `model_selector.py`, `chat_selector.py`, `statusbar.py`,
`styles.tcss`).
SHAMSU: 3 modules (`narrative.py`, `trace.py`, `progress.py`) rendering into a
`prompt_toolkit` REPL.

Different products: a TUI application vs a terminal REPL.

## M. Subtask ledgers — both, differently

SmallCTL: `harness/subtask_ledger_service.py`, `subtask_checklist.py`,
`recovery_schema.SubtaskLedger`, `context/subtasks.py`, `task_transactions.py`.
SHAMSU: `taskmaster/` (adapter, service, types), `plans/store.py`,
`tasks/state.py`, `agents/task_harness.py`, `agents/plan_mode.py`.

Comparable in intent. Not examined closely enough to rank.

## N. Structured reasoning modes

SmallCTL: `reasoning_policy.py`, `reasoning_mode` config key,
`STAGED_THOUGHT_ARCHITECTURES = {"multi_phase_discovery", "staged_reasoning"}`,
`_RESPONSE_STRUCTURE_THINK` fragment, `prompt_model_classifiers.py` for
think-capable models.
SHAMSU: `think: true` support with per-model fallback memoization
(`_THINK_UNSUPPORTED`, `manager.py:91-94`) and `_log_thinking()` persisting full
CoT to the ledger's `cot/` folder.

SHAMSU's CoT **artifact persistence** is arguably better; SmallCTL's
**reasoning-mode selection** is more configurable.

---

## Revised §11 ordering

1. **Phase contracts** (§2)
2. **Model escalation to a bigger tier** (§A) — *new; tiers already exist*
3. **Claim gate** (§2)
4. **Per-model prompt fragments** (§8)
5. **Named config presets** (§E) — *new; cheap UX win*
6. **Failure taxonomy for memory** (§9)
7. **mypy** (§J) — *new; cheapest item on the list*
8. Split tool-result budget by content kind (§6)
9. Non-ASCII token estimation (§6)
10. Consecutive-error ceiling (§1)
11. Context invalidation (§7)
12. Close the eval loop on SHAMSU's honesty metrics (§10)

## Still not compared

Streaming/rendering internals, redaction implementations (both have one),
git integration depth, installer/updater (`install.sh` vs `scripts/install.*`),
error-taxonomy internals, state-schema durability, `fama/` runtime behavior,
SmallCTL's `challenge_loop`/`harness_runner`, SHAMSU's `audit/`, `diagnostics/`,
`verify/` internals, and `action_ledger/store.py`.

**No runtime evidence for either system.** Every claim in this document is
static-analysis only.
