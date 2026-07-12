# SHAMSU Reliability & Output-Contract System Design

_Status: proposal. Complements `imporvement plan by opus.md` (verify loop, context reuse,
diff format, retrieval, eval). This doc goes deep on the layer that plan under-weights: the
**model I/O / tool-contract boundary** — the root cause behind the JSON leaks, diff leaks, and
agentic dead-loops observed in testing — and gives a concrete system design + sequencing._

Reflects current state after this session's changes: template scaffolds are **disabled by
default** (`SHAMSU_ENABLE_TEMPLATES=1` restores), and **plan mode** (`plan`/`proceed`,
`.shamsu/plans/`, milestone execution) has landed.

---

## 1. Root-cause thesis

SHAMSU is built for models that emit **clean structured output** — native `tool_calls`,
byte-perfect unified diffs, well-formed JSON — then runs on **4–7B local models that don't**
(deepseek-r1:7b for qa/router, gemma3:4b thinking, qwen2.5-coder:7b for coding). Almost every
fragile behavior is *compensating scaffolding* for that one mismatch:

| Observed failure | Proximate cause | Root cause |
|---|---|---|
| Raw `{"name":"ask_user",...}` printed to chat | loop only reads native `message.tool_calls` | output-contract mismatch |
| `<<<<<<< SEARCH`/full-file dumped, files untouched | edit path expects a unified diff | output-contract mismatch |
| "Agent finished", 0 changes, infinite re-plan | no `tool_calls` → prose treated as final answer | output-contract mismatch |
| `_MAX_PROSE_CORRECTIONS`, `_MAX_READ_RECOVERIES`, empty-response nudges | band-aids per failure | output-contract mismatch |

**Design principle:** stop adding per-symptom band-aids. Put a **single normalization boundary**
between the model and the agent loop that tolerates messy small-model output, make model
**capabilities explicit**, and **measure** task success so every change is provable.

---

## 2. Gap inventory (prioritized)

★ = impact on real task success. "New" = not in `imporvement plan by opus.md`.

| # | Gap | Sev | New? | Where |
|---|---|---|---|---|
| G1 | Tool calls only read from native field; `_json_action_tool_call` matches `action` not `name`, excludes `ask_user`, requires whole-message JSON | ★★★★★ | ✔ | [chat_loop.py](../shamsu/agents/chat_loop.py) `_tool_calls_from_message`, `_json_action_tool_call` |
| G2 | No task-success eval; only `test_m6` Django fixtures. Every fix is unmeasured | ★★★★★ | ✔ | `tests/` only |
| G3 | Native tool-calling invoked on non-tool models (deepseek-r1 qa/action, gemma3) | ★★★★☆ | ✔ | [tool_calling_loop.py:69](../shamsu/agents/tool_calling_loop.py#L69), model routing |
| G4 | No general verify→fix gate; agent trusts tool `ok`, hallucinates success | ★★★★★ | ~ (2.1) | [chat_loop.py](../shamsu/agents/chat_loop.py); only PRD/freeform paths verify |
| G5 | Unified-diff-first edit format; SEARCH/REPLACE unhandled in chat path | ★★★★☆ | ~ (2.3) | [code_edit_workflow.py:26](../shamsu/agents/code_edit_workflow.py#L26) |
| G6 | Two divergent tool loops; `ToolCallingAgentLoop` defined but **never instantiated** | ★★★☆☆ | ✔ | [tool_calling_loop.py](../shamsu/agents/tool_calling_loop.py) |
| G7 | `_handle_request` = ~25 hand-maintained `_looks_like_*` detectors; brittle, order-dependent (the plan-prose dead-loop slipped through) | ★★★☆☆ | ✔ | [repl.py](../shamsu/cli/repl.py) `_handle_request` |
| G8 | CoT hidden with no live surface; `think` never enabled; suppressed by prompt + logged-only | ★★☆☆☆ | ✔ | [manager.py](../shamsu/llm/manager.py) `_log_thinking` |
| G9 | Context feed invisible (queries/snippets behind `SHAMSU_SHOW_CONTEXT`; static status strings) | ★★★☆☆ | ~ | [repl.py](../shamsu/cli/repl.py) |
| G10 | Raw PRD/plan text re-dumped every step instead of the compact brief + checklist | ★★★★☆ | ~ (2.2) | milestone + plan-step requests |
| G11 | Graphiti `forget()` is a stub — wrong facts retrieved forever | ★★☆☆☆ | ✔ | [graphiti_adapter.py](../shamsu/memory/graphiti_adapter.py) |
| G12 | Big tool results can blow the window mid-loop (only char-truncated, not budgeted) | ★★☆☆☆ | ✔ | [chat_loop.py](../shamsu/agents/chat_loop.py) `_compact_value` |
| G13 | Router runs on a 7B reasoning model for pure JSON classification (latency each turn) | ★★☆☆☆ | ✔ | `model_for_role("router")` |

---

## 3. System design

Five components. The first two are the load-bearing ones; the rest reuse machinery that
already exists.

```
                       ┌────────────────────────────────────────────┐
   user request  ─────▶│  AgentLoop  (single, consolidated)         │
                       │                                            │
                       │  ┌────────────┐   raw    ┌───────────────┐ │
                       │  │  LLM call  │ ───────▶ │ parse_model_  │ │  ← (A) Model I/O boundary
                       │  │ (tools if  │ response │   turn()      │ │
                       │  │  capable)  │          │  salvage      │ │
                       │  └────────────┘          │  cascade      │ │
                       │        ▲                 └──────┬────────┘ │
                       │        │ capability            │ ModelTurn │
                       │  ┌─────┴───────┐               ▼           │
                       │  │ Capability  │        tool_calls / text  │
                       │  │  Registry   │        / thinking          │  ← (B) Capability registry
                       │  └─────────────┘               │           │
                       │                          ┌─────▼────────┐  │
                       │                          │ ToolRegistry │  │
                       │                          │  execute     │  │
                       │                          └─────┬────────┘  │
                       │        loop until done         │           │
                       │                          ┌─────▼────────┐  │
                       │                          │ Verify Gate  │  │  ← (C) Verify gate
                       │                          └─────┬────────┘  │
                       └────────────────────────────────┼──────────┘
                                                         ▼
                                              verified result / UNVERIFIED
        (D) Eval harness drives this whole path headlessly and scores pass/fail.
        (E) TraceView surfaces queries, snippets, and ModelTurn.thinking live.
```

### (A) Model I/O boundary — `shamsu/llm/output.py`  [fixes G1, G5, G8]

The single place a raw model response becomes a normalized turn. **Every** loop calls it; no
loop parses `tool_calls` or diffs itself again.

```python
@dataclass(frozen=True)
class ModelTurn:
    text: str                    # visible answer, tool-syntax stripped
    thinking: str                # reasoning trace, kept out of `text`
    tool_calls: list[ToolCall]   # normalized, validated against the registry
    salvaged: bool               # True if calls came from content, not the native field

def parse_model_turn(response: Any, tools: ToolRegistry) -> ModelTurn: ...
```

Salvage **cascade** (stop at first hit that yields ≥1 valid call):

1. **Native** — `message.tool_calls` (Ollama structured). Preferred; `salvaged=False`.
2. **Embedded JSON** — brace-scan `content` for objects matching
   `{"name"|"action"|"tool": <registered>, "arguments"|"parameters"|"args": {…}}`,
   even inside prose or ``` fences; repair with `json_repair`; map to `ToolCall`.
   *This is the direct fix for the `{"name":"ask_user",...}` leak.*
3. **SEARCH/REPLACE** — detect `<<<<<<< SEARCH … ======= … >>>>>>> REPLACE` blocks and a
   preceding path → synthesize `edit_file(filepath, old_string, new_string)` calls.
4. **XML-ish** — `<tool_call>{…}</tool_call>` (some templates emit this).

Then: split `<think>…</think>` (or the Ollama `thinking` field) into `thinking`, and **strip
any leaked tool JSON / SEARCH-REPLACE from `text`** so the UI never shows raw syntax even when a
real call was also parsed.

Replaces: `_tool_calls_from_message` + `_json_action_tool_call` (chat_loop), `_message_from_response`
+ `_tool_calls_from_message` (tool_calling_loop), `_clean_diff` (code_edit).

### (B) Capability registry — extend `shamsu/runtime/models.py`  [fixes G3, G8, G13]

`ModelSpec` gains flags; the loop uses them instead of assuming every model does native tools.

```python
@dataclass(frozen=True)
class ModelSpec:
    ...
    supports_native_tools: bool = False   # qwen2.5-coder: True; deepseek-r1/gemma3: False
    is_reasoning: bool = False            # deepseek-r1/qwen3: True -> set think=True
```

Loop policy:
- executor model `supports_native_tools` → pass `tools=` schema, prefer native (salvager backs it up).
- **not** tool-capable → **don't** pass a tools schema (it confuses these models); inject a
  compact tool protocol into the system prompt and make the salvager the **primary** parser.
- `is_reasoning` → send `"think": true` so CoT separates into the `thinking` field cleanly
  (feeds TraceView; keeps `text` clean) instead of leaking inline `<think>`.
- Bind the **executor/action loop to a tool-capable coding model**, not the qa/reasoning model
  (today `ToolCallingAgentLoop` defaults to `model_for_role("qa")` = deepseek-r1). Route the
  **router** to a small instruct model (qwen2.5:3b) — faster, no reasoning overhead for JSON.

### (C) Verify gate — `shamsu/verify/gate.py`  [fixes G4]

Reuse, don't rebuild: `FreeformGenerator._default_verify` already picks a deterministic
verifier from the stack + changed files, and `RepairLoop` + `diagnostics/` already do
bounded fix loops.

```python
def verify_and_repair(workspace, changed_files, *, generate, max_attempts=2) -> VerifyOutcome:
    cmd = default_verify_command(stack_of(changed_files), changed_files)  # extracted from freeform
    if not cmd:
        return VerifyOutcome(verified=False, unverifiable=True)          # honest, not "success"
    return RepairLoop(workspace, CommandVerifier(cmd), LLMProposer(generate), max_attempts).run()
```

Plug at the **end** of: the consolidated `AgentLoop` (when any write happened), plan-mode
`_execute_plan`, and after each milestone. Contract: **never report success unless verified or
explicitly unverifiable.** This is the single biggest quality lever for small models, which
routinely hallucinate success.

### (D) Eval harness — `evals/`  [fixes G2]  — *do this first*

Deterministic task-success runner. This is what makes every other change provable.

```python
@dataclass
class EvalCase:
    name: str
    workspace_seed: Path | None      # files to start from (or empty)
    prompt: str                      # what the user would type
    check: Callable[[Path], bool]    # deterministic: file contains X / exit 0 / test passes

def run_evals(cases, tier) -> EvalReport   # spins a temp workspace, drives the request path headlessly
```

Seed set (8–12 cases, one per real path): a QA, a targeted `edit_file`, a bugfix-from-traceback,
a 3-file freeform build, a `plan`→`proceed` run, a "create file" write, an ask_user clarification,
a run-command verify. Emit a `BENCHMARK.md`-style pass-rate + per-case table. Run in CI-lite
against the active tier. **Baseline before touching anything.**

### (E) Observability — `TraceView`  [fixes G9, G8]

The data already flows through `on_trace` / ActionLedger `_context_preview`. Surface at
`normal` level (not behind `SHAMSU_SHOW_CONTEXT`): the actual `search_index(query=…)`, the
top-k file hits + scores, and a token-composition breakdown. Add a dim, collapsible
**Reasoning** pane fed by `ModelTurn.thinking` (kept out of the answer). Add `/context show`.

### Supporting: context discipline [G10], memory correctness [G11], tool-result budgeting [G12]

- **G10** — swap raw-PRD/plan dumps for `PRDContract.render_brief()` + a carried requirement
  **checklist** (source of truth so nothing is dropped). Not full RAG — a single PRD fits the
  window; completeness beats retrieval here.
- **G11** — implement Graphiti `forget()` (tombstone + exclude-from-recall) so wrong facts can
  be evicted.
- **G12** — budget/paginate large `read_file`/`grep_files` results before they enter history,
  not just char-truncate at emit time.

---

## 4. Sequencing

| Phase | Work | Gaps | Effort | Gate |
|---|---|---|---|---|
| **0** | Eval harness + baseline number | G2 | ~1–2d | baseline recorded |
| **1** | Model I/O boundary (salvager) + capability registry; route executor to tool-capable model | G1, G3, G5, G8 | ~2–3d | eval pass-rate ↑; zero raw-JSON leaks in eval logs |
| **2** | Verify gate wired into AgentLoop + plan mode | G4 | ~2d | eval "build" cases only pass when they compile |
| **3** | Consolidate the two loops onto the boundary; delete/absorb `ToolCallingAgentLoop` | G6 | ~1–2d | one tool parser; suite green |
| **4** | Context discipline (brief+checklist) + TraceView | G9, G10 | ~2d | context tokens/step ↓; queries visible |
| **5** | Cleanups: router model, `forget()`, tool-result budgeting, trim `_looks_like_*` | G7, G11, G12, G13 | ongoing | — |

**Rule: no prompt/loop change ships without an eval delta.** The reason this codebase
accreted so many `_MAX_*` band-aids is that small-model failures were fixed by feel, not by
measurement.

---

## 5. Risks & non-goals

- **Non-goal: document RAG / per-PRD graph DB.** A single PRD fits the window; structured
  extraction (`PRDContract`) + checklist is safer (completeness) and cheaper. Retrieval only
  for genuinely large corpora — see the RAG discussion in the CHANGELOG/notes.
- **Risk: salvager false positives** (treating an example JSON in an answer as a tool call).
  Mitigate: only salvage when native `tool_calls` is empty, require the name to be a
  *registered* tool, and validate arguments before executing.
- **Risk: capability flags drift** as the cookbook changes. Keep them on `ModelSpec` next to
  the model definition, and add an eval case per tier so a wrong flag shows up as a pass-rate drop.
- **Risk: verify gate over-blocks** on projects with no verifier. Mitigate: `unverifiable` is a
  first-class, non-failing outcome (report honestly, don't claim success).

---

## 6. First PR (suggested)

`evals/` harness + 8 seed cases + a `BENCHMARK`-style report, run against the `default` tier to
record a baseline. Then PR #2: `shamsu/llm/output.py` (`parse_model_turn` + salvager) behind the
existing loops, and re-run the eval to prove the lift. Everything after sequences off that number.
