# SHAMSU Improvement Plan — How to Get Better Results

**Author:** Opus 4.8 (research + code audit)
**Date:** 2026-07-07
**Scope:** What limits result quality in the current SHAMSU coder, and a prioritized plan to fix it — grounded in the actual code, not aspirational.

---

## 0. TL;DR

SHAMSU already has a **strong skeleton**: two-model tiering, a safety/approval/transaction layer, Graphiti + Codebase-Memory separation, a council pattern, a diagnostics digest, and (new) a context-budget manager. The pieces that make a coder *feel smart* are mostly **missing or half-wired**, not absent by architecture.

The single biggest lever is this: **SHAMSU generates a change and stops. It almost never runs the change and reacts to what happens.** A local 7B model is wrong often; the only thing that reliably makes a weak model produce correct code is a **closed verify→fix loop against ground truth** (tests, typecheck, lint, run). Today that loop exists *only for Django* (`ErrorFeedbackLoop`).

The five highest-ROI changes, in order:

1. **Generalize the verify→fix loop** beyond Django to any project (tests / typecheck / lint / build).
2. **Fix context-window under-utilization** — every call is hardcoded to `num_ctx=8192` while the models support 32k–128k.
3. **Switch the primary edit format from unified diff to SEARCH/REPLACE blocks** — weak models fail unified diffs constantly (that's why the "full rewrite fallback" exists).
4. **Add a cheap self-review pass on the common edit path** (council is currently gated off for 95% of edits).
5. **Cut the model-swap tax** — router + planner + coder = 3 calls and 2 model swaps before any output on 8GB hardware.

Everything below expands these with file-level references and concrete tasks.

---

## 1. How SHAMSU works today (as-built)

Traced from the code so the gaps are precise:

- **Routing** — `shamsu/llm/manager.py::route()` makes a full LLM call to `qwen3:8b` that emits routing JSON (intent ∈ code_edit / bug_fix / audit / test_gen / doc_gen / qa / explain / generate). Keyword fallback exists when the index isn't ready.
- **Retrieval** — `shamsu/retriever/search.py::SearchAgent` is backed solely by the **Codebase-Memory MCP** (`search_code`, `get_symbols`, `get_code_snippet`). One query, `top_k` results, ranked **purely by MCP return position** (`score = total - position`), with an optional path-boost for known traceback locations.
- **Context assembly** — `shamsu/context/builder.py::ContextBuilder.pack()` dedupes, truncates-middle, and packs snippets under a fraction budget; `shamsu/llm/manager.py::_format_pack()` places the task **last** (correctly exploiting "Lost in the Middle"). New `ContextBudgetManager` measures tokens + compacts.
- **Two execution paths:**
  - **Workflows** (`code_edit_workflow.py`, `bugfix_workflow.py`, `test_generation_workflow.py`, `doc_workflow.py`, `audit_workflow.py`): `search → create_plan → coder emits a unified diff → validate → apply`, with a **full-file-rewrite fallback** when the diff is malformed.
  - **ReAct chat loop** (`agents/chat_loop.py`): stateful tool loop with **5 tools** (`list_files`, `read_file`, `write_file`, `run_command`, `search_index`), a per-request planner call, a repetition guard, and a model timeout.
- **Council** (`llm/council.py`): sequential draft→critique→reconcile, but **gated** (`should_convene_council`) to only fire on destructive actions, security-sensitive paths, or routing-confidence < 0.5.
- **Verify loop** (`agents/error_feedback_loop.py`): real test→fix→retest loop — **but hardcoded to `DjangoTestRunner`**.
- **Memory** — Graphiti (long-term) + Codebase-Memory MCP (code facts), plus a new token-budget layer.

**What's good and should be kept:** the safety/sandbox/approval/transaction model, tiering, the budget manager + calibration, the diagnostics `ErrorPacket`, and the council *mechanism* (it's just under-deployed).

---

## 2. What's lacking (ranked by impact on result quality)

### 2.1 No general verify→fix loop  ★★★★★ (biggest gap)
`CodeEditWorkflow.run()` validates diff *structure*, applies it, and returns. It never asks *"did that actually work?"* — no test run, no `tsc`/`mypy`, no `ruff`/`eslint`, no build. The only closed loop is `ErrorFeedbackLoop`, wired to `DjangoTestRunner` (`error_feedback_loop.py:68`). For a React app, a Go service, a plain Python lib, SHAMSU is fire-and-forget. A 7B model without a feedback signal ships broken code; *with* one it converges. This is the difference between a demo and a tool.

### 2.2 Context window thrown away  ★★★★★ (cheap to fix)
`num_ctx=8192` is hardcoded in **every** call path (`manager.py:196`, `manager.py:400`, `chat_loop.py:148`) even though `MODEL_CONTEXT_WINDOWS` in `context/budget.py` already knows `qwen3:8b`→32768 and `mistral-nemo:12b`→131072. So ~75% of the coder's available context is unused: fewer files fit, more truncation, worse edits. The new budget manager *computes* the real window but nothing *uses* it to set `num_ctx`.

### 2.3 Unified-diff-first is the wrong format for weak models  ★★★★☆
`CODE_EDIT_INSTRUCTIONS` demands a strict unified diff. Small models produce broken hunk headers / wrong line counts constantly — which is *exactly* why `rewrite_fallback.py` exists (rewrite the whole file when the diff won't parse). Aider's public edit-format benchmarks show weaker models score dramatically higher with **SEARCH/REPLACE blocks** than with unified diffs, because S/R blocks don't require the model to count lines or compute offsets. The full-rewrite fallback is a worse safety net: it risks dropping code the model "forgot" to re-emit, and it burns the whole context re-printing unchanged content.

### 2.4 Retrieval is shallow and single-shot  ★★★★☆
`SearchAgent.search()` fires **one** query and trusts MCP ordering as the score. There is:
- no **query expansion / rewriting** (the user's words rarely match code identifiers — HyDE-style expansion or symbol extraction from the prompt helps a lot),
- no **neighbor / dependency expansion** (pull the imports, the callee/caller, the class the method lives in — RepoCoder-style iterative retrieval),
- no **re-ranking** (positional score ≠ relevance; a cheap cross-encoder or even a model-scored rerank of the top-20 beats raw FTS order),
- **no grounding gate**: if `search()` returns `[]`, the coder still runs with zero snippets and hallucinates APIs.

Garbage context in → garbage edit out, regardless of model quality.

### 2.5 Self-review is gated off for the common case  ★★★★☆
The council draft→critique→reconcile is genuinely useful for weak models (a second cheap pass catches obvious bugs), but `should_convene_council()` only fires on destructive/security/low-confidence cases. A normal `code_edit` gets a **single** coder pass with no review. Self-Refine / Reflexion-style single self-critique is one of the cheapest known quality wins for small models.

### 2.6 The model-swap tax  ★★★★☆ (perf → users bail)
On the default 8GB tier, a single request can trigger: **router** (`qwen3:8b`, `keep_alive=-1` so it's pinned) → **planner** (`qwen3:8b`) → **coder** (`qwen2.5-coder:7b`). Two 7–8B models cannot co-reside in 8GB, so Ollama **swaps** the coder in and the router back out repeatedly. That's 3 LLM calls and ≥1 expensive reload before the first token of a diff. Meanwhile `_append_plan` in the chat loop calls the planner **on every message, including "hi"** (`chat_loop.py:129-130`). Latency is a *result-quality* issue: slow tools get abandoned mid-task.

### 2.7 Redundant Graphiti injection  ★★★☆☆ (verified)
Memory is injected **twice** on the workflow path: `CodeEditWorkflow._build_pack()` bakes `graphiti_brief` into the prompt (`code_edit_workflow.py:117`), and then `LLMManager.run_specialist()` calls `_with_long_term_memory()` which injects Graphiti memory **again** into `prd_context` (`manager.py:332`). Same double-hit in `bugfix_workflow.py` and `test_generation_workflow.py`. This wastes tokens and can drown the actual code context in stale memory.

### 2.8 The planner is cosmetic  ★★★☆☆
`create_plan()` returns ≤10 lines of free text folded into the coder prompt. It doesn't produce a **structured, checkable** plan (target files validated against the repo, ordered steps, explicit acceptance/verify command). It also doesn't feed the verify loop. It looks like planning without changing behavior.

### 2.9 Thin ReAct tool surface  ★★★☆☆
5 tools only. `write_file` **always overwrites the whole file** — no range edit / patch tool — so editing one line of a 900-line file forces re-emitting all 900 lines (and re-truncating context). No `run_tests`, `git`, `lint`, or `format` tool. No way to edit surgically, which compounds 2.2 and 2.3.

### 2.10 No streaming on the workflow path  ★★☆☆☆
`run_specialist()` is non-streaming; the user watches a spinner for 30–120s per call. `run_specialist_stream()` exists but workflows don't use it. Perceived quality and the ability to abort a bad generation early both suffer.

### 2.11 No evaluation harness  ★★★★☆ (can't improve what you can't measure)
`BENCHMARK.md` exists but there's no repeatable **task suite** that reports a success rate. Every change in this plan needs a number to move. Without an eval loop, "getting better results" is guesswork.

### 2.12 Prompts are terse and example-free  ★★☆☆☆
Specialist prompts are one-liners ("Output ONLY a unified diff."). Weak models need **few-shot examples** of the exact edit format, explicit "read before you edit," and repo-convention hints. No prompt carries a single worked example.

---

## 3. The plan

### Phase 1 — Quick wins (days, highest ROI)

**P1.1  Tier-aware `num_ctx`.**
Thread the real context window into generation. Add `num_ctx_for_model(model)` (derive from `MODEL_CONTEXT_WINDOWS` minus reserve) and pass it in `_generate()` / `_generate_stream()` / `chat_loop` instead of the hardcoded `8192`. **Tier-gate it:** light tier stays conservative (swapping risk on 8GB), heavy tier opens up to 32k+. This alone lets more correct context fit with zero model change.
_Files:_ `llm/manager.py`, `agents/chat_loop.py`, `context/budget.py`. _Tests:_ assert `num_ctx` scales with tier/model.

**P1.2  De-duplicate Graphiti injection.**
Inject long-term memory in **one** place. Keep `_with_long_term_memory()` in `run_specialist()` and remove the per-workflow `graphiti_brief` (or vice-versa) so the coder sees memory once.
_Files:_ `code_edit_workflow.py`, `bugfix_workflow.py`, `test_generation_workflow.py`.

**P1.3  Don't plan trivial turns.**
Guard `_append_plan()` / `_append_long_term_memory()` in `chat_loop.py` behind a cheap "is this a real task?" check (skip greetings, one-word replies, pure questions). Removes a full thinking-model call + swap from casual turns.
_Files:_ `agents/chat_loop.py`.

**P1.4  Grounding gate on empty retrieval.**
When `search()` returns `[]` for a code_edit/bug_fix, don't call the coder blind — inspect (list/read likely files) or ask one clarifying question first.
_Files:_ `code_edit_workflow.py`, `bugfix_workflow.py`, orchestrator.

### Phase 2 — Correctness engine (the real quality jump)

**P2.1  Generalize the verify→fix loop.**  ← flagship
Extract the `ErrorFeedbackLoop` shape into a project-agnostic **VerifyLoop** driven by a `Verifier` abstraction:
- detect the stack (already have `registry/detector.py`) → pick verify command(s): `pytest` / `npm test` / `go test` / `tsc --noEmit` / `ruff` / `eslint` / build.
- run → parse failures through the existing `DiagnosticDigest`/`ErrorPacket` → feed compact failures to the bugfix coder → re-run. Reuse the stall/same-signature guards already in `error_feedback_loop.py`.
- Make `CodeEditWorkflow` optionally run VerifyLoop after apply (behind `/autonomy` or a `--verify` flag so approval-gated users keep control).
_New:_ `agents/verify_loop.py`, `verify/verifier.py` (stack→command map). _Tests:_ non-Django (pytest + node) fixtures that fail then pass.

**P2.2  SEARCH/REPLACE edit format as primary.**
Add an edit format where the model emits ` <<<<<<< SEARCH / ======= / >>>>>>> REPLACE ` blocks per file. Apply by exact-match substring replace (fall back to fuzzy/whitespace-tolerant match). Keep unified-diff parsing as a secondary acceptor and full-rewrite as the last resort. Route weak tiers (light/default) to S/R by default.
_New:_ `patch/search_replace.py`. _Files:_ `code_edit_workflow.py`, `patch/engine.py`. _Tests:_ malformed-diff cases that S/R handles cleanly.

**P2.3  Cheap self-review on the common edit path.**
Lower the council bar: run a **single** critique pass (not full reconcile) on every code_edit whose blast radius is > N lines or > 1 file, using the coder model itself (no swap) with a tight "list concrete bugs or say NONE" prompt. Only reconcile if it flags something. This is Self-Refine with a hard 1-iteration cap to bound local cost.
_Files:_ `llm/council.py` (add a `light_review` mode), `code_edit_workflow.py`.

**P2.4  Structured, checkable plan.**
Upgrade `create_plan()` to emit a small JSON plan (schema-constrained, like routing): `{target_files[], steps[], verify_command}`. Validate `target_files` against the repo; feed `verify_command` straight into P2.1. Fall back to free-text on parse failure.
_Files:_ `agents/planner.py`.

### Phase 3 — Retrieval quality

**P3.1  Query expansion.** Before `search_code`, extract identifiers/symbols from the prompt and issue 2–3 expanded queries (raw prompt + symbol query + a HyDE-style "code that would do X" query); merge + dedupe results.
**P3.2  Neighbor expansion.** For each top hit, pull its enclosing symbol and direct imports/callers via `get_symbols`/`get_code_snippet` so the coder sees the *unit*, not a fragment.
**P3.3  Re-rank.** Replace positional `score` with a real rank over the top-K (start simple: term-overlap + path/recency signals; optional model-scored rerank for high-stakes).
_Files:_ `retriever/search.py`, new `retriever/rerank.py`, `retriever/expand.py`. _Tests:_ ranking asserts the traceback file and defining symbol land in the top results.

### Phase 4 — Performance / cost (so people actually use it)

**P4.1  Reduce swaps.** Batch same-model calls (router+planner are both the thinking model — keep them adjacent, coder last). Reconsider `keep_alive=-1` on the router for the 8GB tier (pinning the thinking model guarantees a coder swap every request). Measure swap cost and surface it (the budget indicator already gives you the UI surface).
**P4.2  Heuristic fast-path routing.** For obvious prompts (`/edit`, `/fix`, clear imperatives, file mentions) skip the router LLM call entirely and route by rule; reserve the LLM router for genuinely ambiguous input.
**P4.3  Stream the workflow path.** Use `run_specialist_stream()` in workflows so users see progress and can abort early.
_Files:_ `llm/manager.py`, `cli/repl.py`, workflows.

### Phase 5 — Measurement (do this early, in parallel)

**P5.1  Internal task suite + scorer.** 15–30 fixed tasks (bug fixes, small features, test-gen) across Python + a JS project, each with an automatic pass check (its own tests). A `scripts/eval.py` that runs SHAMSU headless and reports **success rate, mean model calls, mean wall-clock, swap count**. Every phase above reports its delta against this. Without it, none of the rest is falsifiable.

---

## 4. Sequencing & effort

| Phase | Item | Impact | Effort | Order |
|------|------|--------|--------|-------|
| 1 | Tier-aware `num_ctx` | ★★★★★ | S | 1 |
| 5 | Eval suite + scorer | ★★★★☆ | M | 2 (parallel) |
| 1 | De-dupe memory / skip trivial plan / grounding gate | ★★★☆☆ | S | 3 |
| 2 | General verify→fix loop | ★★★★★ | L | 4 |
| 2 | SEARCH/REPLACE edit format | ★★★★☆ | M | 5 |
| 2 | Cheap self-review | ★★★★☆ | S | 6 |
| 3 | Retrieval expansion + rerank | ★★★★☆ | M | 7 |
| 2 | Structured plan | ★★★☆☆ | M | 8 |
| 4 | Swap reduction / fast-path / streaming | ★★★★☆ | M | 9 |

Rule of thumb: **build P5.1 second** (right after the free `num_ctx` win) so every later change is measured, not guessed.

---

## 5. Research basis (why these specifically)

- **Verify→fix loops** — agentic SWE systems (SWE-agent, and the general "execution feedback" result) show that letting the model observe test/compiler output and iterate is the dominant factor in solve rate, far more than raw model size. Directly motivates P2.1.
- **Edit format for weak models** — Aider's edit-format benchmarks: SEARCH/REPLACE blocks beat unified diffs by a wide margin on smaller models because they remove line-counting/offset arithmetic. Motivates P2.2. (SHAMSU's existing full-rewrite fallback is itself evidence the diff format is failing.)
- **Self-Refine / Reflexion** (Madaan et al.; Shinn et al.) — a single self-critique pass reliably lifts correctness for cheaper models at bounded cost. Motivates P2.3 (with a hard 1-iteration cap for local hardware).
- **Repository-level retrieval** (RepoCoder; iterative retrieval) and **RAG re-ranking / query rewriting (HyDE)** — expanded queries + neighbor/dependency expansion + reranking materially raise context relevance vs. single-shot lexical search. Motivates Phase 3.
- **Lost in the Middle** (Liu et al., TACL 2024) — already correctly applied (task placed last in `_format_pack`); the corollary is that **using the full window** (P1.1) only helps if important context sits at the ends, so pair P1.1 with keeping the task tail-anchored.

---

## 6. Concrete first PR (suggested)

Smallest change that moves a number:

1. `num_ctx_for_model()` in `context/budget.py`; thread through `_generate`, `_generate_stream`, `chat_loop` (tier-gated).
2. Remove the duplicate `graphiti_brief` injection in the three workflows.
3. Add `scripts/eval.py` + 6 seed tasks (3 Python, 3 JS) with pass checks.

Then measure, then start Phase 2.

> Guiding principle unchanged from the README: *deterministic tools find the context; a small local model reasons over it.* Everything above is about *tightening the loop between the model and ground truth* — that, not a bigger model, is how SHAMSU gets better results on the hardware it targets.
