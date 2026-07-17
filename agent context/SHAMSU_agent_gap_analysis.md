# SHAMSU Agent — Gap Analysis

**Date:** 2026-07-17
**Baseline:** `develop` @ `93bba66` + branch `fix/routing-planning-cot` (PRD-routing fix, specialist CoT, plan mode). Test suite fully green: 1080 passed / 1 skipped / 0 failed. Eval baseline 6/6 (default tier).
**Method:** code-level sweep of the REPL dispatch, agent chat loop, tool registry, planners, verify gate, retrieval, memory, evals, and failure paths. Every finding cites the file it lives in.

> **Caveat, added 2026-07-17 after being wrong once.** The original version of this doc claimed every finding was "read directly from the code, nothing speculative". **A2 was still wrong** — it was read from a call site (`_run_agent_chat` never passes `state=`) without checking the constructor (`ChatState.__init__` self-hydrates), and shipped as a HIGH. Reading code is not the same as running it. Findings below are marked ✅/◑ only after the behavior was *executed and observed*, not merely read. Treat unfixed entries as leads to verify, not established fact.

This is the successor to `SHAMSU_reliability_system_design.md`. All 13 of that doc's gaps are landed to at least a safe, tested baseline (G7's structural trim deferred behind a characterization net). This doc is what's left — and what the reliability work newly exposed.

---

## Severity summary

**Status key:** ✅ fixed · ◑ partially addressed · (blank) open

| # | Gap | Area | Severity | Status |
|---|-----|------|----------|--------|
| A1 | One unhandled exception crashes the whole REPL | Robustness | **HIGH** | ✅ |
| A2 | ~~No memory between prompts~~ → cross-route continuity (claim corrected) | Conversation | MEDIUM | ✅ |
| B1 | Keyword-list routing silently degrades to QA (systemic) | Routing | **HIGH** | |
| B2 | Routing truth exists in two hand-synced copies | Routing | **HIGH** | ✅ |
| C1 | Ungrounded planning — both planners now schema-constrained | Planning | **HIGH** | ✅ |
| F1 | Eval harness can't enforce its own rule | Measurement | **HIGH** | ✅ |
| J1 | Only the agent chat loop can ask the user anything | Clarification | **HIGH** | |
| J2 | The stuck-loop clarify path is dead code | Clarification | **HIGH** | ✅ |
| J3 | Mixed prompt signals make small models never ask | Clarification | MED-HIGH | ✅ (needed J6 to actually work) |
| J4 | Answering a question restarts the world (re-routed, amnesiac) | Clarification | MEDIUM | ◑ transcript now carries over |
| J5 | A question asked mid-plan execution is effectively lost | Clarification | MEDIUM | ✅ |
| J6 | No upfront decision-asking before acting on vague requests | Clarification | MEDIUM | ✅ |
| D1 | No web/browser access inside the agent loop | Tooling | MEDIUM | |
| D2 | No delete / move / rename tools | Tooling | MEDIUM | ✅ |
| E1 | The main agent loop never repairs, only reports | Verify/repair | MEDIUM | |
| E2 | Interactive verify is lightweight-only; node builds unverifiable | Verify/repair | MEDIUM | |
| G1 | Mid-loop approval admits being fragile on Windows | Approvals | MEDIUM | |
| G2 | Rollback exists but is practically undiscoverable | Safety net | MEDIUM | ✅ |
| G3 | 33 `except Exception` blocks in repl.py swallow failures silently | Robustness | MEDIUM | |
| B3 | Unknown models get silently-wrong capability defaults | Model registry | MEDIUM | ✅ |
| H1 | Retrieval is FTS-only — no semantic search | Retrieval | LOW-MED | |
| H2 | Taskmaster: heavy external dependency duplicating in-repo logic | Architecture | LOW-MED | |
| I1 | Agent-loop answers don't stream | UX | LOW | |
| I2 | Follow-up expansion covers only web/browser phrases | Conversation | LOW | |
| I3 | Evals are single-sample (noisy) + default tier only | Measurement | MEDIUM | |
| I4 | G7 dispatch-chain structural trim still deferred | Routing | LOW | ✅ |

---

## A. Robustness

### A1. One unhandled exception crashes the whole REPL — HIGH ✅ FIXED 2026-07-17

> **Landed.** All six ledger-tracked `raise` sites now call `_report_request_error(...)` and continue/return instead of propagating. `LLMStalledError` gets its own actionable panel (`ollama ps`, `SHAMSU_LLM_IDLE_TIMEOUT`); everything else reports type + message and says the session survived, with the traceback in the session log. `KeyboardInterrupt`/`SystemExit` are not `Exception` subclasses, so Ctrl+C and `/exit` are untouched. `_resolve_proceed` returns `True` on a failed plan — it *was* pending, and claiming "nothing to proceed" would be a worse lie. Tests: `tests/test_repl_crash_guard.py` (5).

**Evidence:** `shamsu/cli/repl.py` — the main `while True` prompt loop wraps `_handle_request` in `except Exception as exc: ledger.fail(str(exc)); clear_current_run(); raise`. That `raise` propagates out of the loop, and `main()` has **no outer handler** around `run_repl` — only around workspace resolution at startup.

**Failure story:** Ollama stalls mid-generation → `LLMStalledError` (`shamsu/llm/manager.py`) → traceback → the entire REPL process exits. The user loses plan mode, pending approvals, the warm prompt session, everything. Same for any bug in any handler: a single `KeyError` in a niche path is a full session kill. Notably, `LLMStalledError` is raised in the manager but **never caught anywhere in repl.py** (verified by grep) — the error type was built for graceful handling that was never wired.

**Fix:** catch at the loop boundary: print the error in a red panel, log it, `continue`. Keep `KeyboardInterrupt`/`SystemExit` passing through. Special-case `LLMStalledError` with its actionable hint (`SHAMSU_LLM_IDLE_TIMEOUT`). One small change, removes the largest single failure mode in the product.

### G3. Exception swallowing hides real failures — MEDIUM

**Evidence:** `repl.py` has **33** `except Exception` blocks (highest count of any first-party file); `tools/web.py` 15; `agents/chat_loop.py` 11. Many are deliberate best-effort logging (fine), but the pattern is applied uniformly — audit logging, session state writes, route recording all fail silently with no counter, no trace event, nothing.

**Failure story:** the audit log (`SessionAuditLog`) breaks on day 1 due to a permissions issue; nobody finds out until they need the audit trail. Same for `set_last_route`, pending-action writes, etc.

**Fix:** don't remove the guards — add a single `_swallowed(exc, where)` helper that increments a counter surfaced in `/context show` and emits a `debug`-level trace. Silent stays silent for users, but becomes diagnosable.

---

## B. Routing & intent

### B1. Keyword-list intent detection silently degrades to QA — HIGH (systemic)

**Evidence:** ~28 `_looks_like_*` detectors in `repl.py`, all substring/keyword lists (`_PRD_BUILD_VERBS`, `_PLAN_REQUEST_PHRASES`, `_PRD_SUMMARY_TRIGGERS`, …). When no rule matches, the request falls to the QA/agent-chat tail — with **no signal that intent was missed**.

**Failure story:** this is not hypothetical — it happened twice in one week. "Build me from the PRD" fell to QA because the PRD wasn't *named* `prd` (fixed 2026-07-17); "make me a plan to…" fell to QA because only the literal `plan ` prefix matched (fixed 2026-07-17). Both fixes added… more keywords. The next phrasing gap already exists somewhere; the architecture guarantees it. The lists are also English-only, and the fuzzy-typo net covers only two word families (weather, build verbs).

**Fix (direction):** the LLM router (`_route_prompt`) already exists and runs *after* the keyword chain. Invert the relationship for action-shaped prompts: keywords stay as fast-path accelerators, but "no keyword matched AND the prompt contains an imperative verb" should go to the LLM router (or `ask_user`) **before** the tool-less QA brain — QA should be a deliberate destination, never a catch-all for missed intent. Requires routing evals first (F1) per the reliability doc's own rule.

### B2. Routing truth lives in two hand-synced copies — HIGH ✅ FIXED 2026-07-17 (with I4)

> **Landed.** Routing is now one ordered `_ROUTE_RULES` table of `(label, detector)`; `_classify_route_label` walks it and `_handle_request` dispatches on the label it returns. The label IS the decision, so `last_route` and the audit trail cannot disagree with reality.
>
> **The drift was real, not theoretical:** the mirror carried 11 of the 20 rules in a different order, so `run the game` dispatched to `run_game` while the trace recorded `qa` — debugging a misroute from a trail that lies is worse than having no trail. A detector that raises now degrades to the QA tail instead of killing the request.
>
> **This also closes I4** (the deferred G7 trim): order is now data, so reordering is editing a list, and `tests/test_routing_matrix.py` (32) asserts every rule has a handler, every handler has a rule, labels are unique, and the two order-critical rules (prd_summary, git) stay first.

**Evidence:** `_handle_request` (repl.py:3410+) is the real ~20-rule dispatch chain; `_classify_route_label` (repl.py:3383) is a *manually maintained mirror* used for session traces — its own docstring admits "a missed/added branch degrades to agent-chat". They have already drifted: the mirror checks ~10 of the ~20 rules.

**Failure story:** `/sessions trace` and the audit log report a route that is not the route that actually ran. Debugging a misroute using the trail becomes actively misleading — the trail says `qa` while the real path was `dev_server`.

**Fix:** make dispatch data-driven — a single ordered list of `(name, detector, handler)` tuples that both the dispatcher and the label function iterate. This is also the safe shape for the deferred G7 trim (I4): reordering becomes editing a list, testable by asserting on the list.

### B3. Unknown models get silently-wrong capability defaults — MEDIUM ✅ FIXED 2026-07-17

> **Landed.** `model_is_reasoning` / `model_supports_native_tools` now fall back to family-name patterns before the blanket default: `deepseek-r1`/`qwen3`/`qwq`/`magistral`/`phi4-reasoning` -> reasoning; `gemma`/`deepseek-r1`/`llava`/`phi3`/`codellama` -> no native tools. An explicit `ModelSpec` always wins, and a model matching no family keeps the old safe defaults (tool-capable, non-reasoning) for the reasons documented there. So a pulled `deepseek-r1:14b` now gets `think=true` instead of leaking `<think>` inline through the salvager every turn. Tests: `tests/test_model_tiers.py` (+4). The salvage-rate warning suggested below is NOT done.

**Evidence:** `runtime/models.py` — `model_supports_native_tools()` returns **True** for any model not in the cookbook; `model_is_reasoning()` returns **False**.

**Failure story:** a user pulls `deepseek-r1:14b` (not in the cookbook) and points a role at it: it gets a native `tools=` schema it handles badly, and never gets `think=true` — so it leaks `<think>` inline and the salvager has to clean up every turn. It *works*, but degraded, with no hint why.

**Fix:** name-pattern heuristics for the known families (`*-r1*`/`qwen3*` → reasoning; `gemma*` → no native tools) + a one-time `tool.salvaged`-rate warning: "this model salvages >50% of turns — consider adding a ModelSpec for it."

### I4. G7 structural trim still deferred — LOW (tracked)

The ~20-rule order-dependent chain has a 57-test characterization net over its *detectors*, but no dispatch-level tests. The B2 fix is the right vehicle: converting to a data-driven table makes order explicit and testable, then trimming becomes safe. Do B2 and I4 as one change.

---

## C. Planning

### C1. Ungrounded planning — HIGH ✅ FIXED 2026-07-17

> **Confirmed empirically, then half-fixed.** The `plan_references_only_real_files` eval caught it on the first live run. A workspace containing exactly `game.js` + `index.html` (vanilla JS) produced a plan whose **every step** targeted `src/components/PauseButton.tsx` — a React component that does not exist, in a project with no React. Step 1 read: *"Reference the PauseButton component from the real files."* It is not real. A coder handed that plan inherits the hallucination as trusted context.
>
> **Root cause was not the model.** `PlanningWorkflow._relevant_files` returned `[]` whenever search found nothing — which is *always* when the index isn't set up (`NullSearchAgent`) or FTS simply misses — and the prompt then demanded a plan "grounded ONLY in the provided workspace context" while providing **none**. Told to ground in nothing, the model invents. The files were sitting on disk the whole time.
>
> **Landed (plan_mode / the user-facing `/plan`):** `_relevant_files` never returns empty while the workspace has source files — search first, then a real mtime-sorted directory listing (dependency/build dirs excluded). Plus a grounding gate: `_unreal_targets` flags step targets that don't exist and aren't being created, and the planner gets **one** corrective round naming the phantoms and listing the real files. A retry that isn't better grounded is discarded rather than swapped in. Same "correct, then accept honestly" shape as the tool-call salvager. Traced as `plan.ungrounded`. **Live re-run: the same prompt now targets `index.html` and `game.js`.** Tests: `tests/test_plan_mode.py` (+6, deterministic).
>
> **`create_plan` converged too (the original C1 target).** `agents/planner.py` no longer asks for free prose: it requests schema-constrained JSON (`plan`, plus the J6 decision fields) via `generate_structured`, parsed through `json_repair` like every other small-model boundary. That closes the last place a raw model string was spliced into a coder's prompt with no validation. An LLM without `generate_structured` (test doubles, narrower interfaces) falls back to the original `run_specialist` text path, so this never hard-depends on a capability `ILLMManager` doesn't promise — and a schema call that raises or returns junk falls back too. All four mutating workflows (CodeEdit, BugFix, TestGeneration, Documentation) keep working unchanged, since they read only `.text`.
>
> **Not claimed:** `create_plan`'s output is now *structured*, but it is not yet *file-validated* the way plan_mode's is (`_unreal_targets` + re-grounding). Its plan is prose inside a JSON field, not a step list with targets, so there is nothing mechanical to check yet. That is the remaining half.

**Evidence:** `agents/planner.py` — one LLM call, plain-text instructions ("Do not write code… under 10 lines"), output spliced raw into the coder specialist's request. No JSON schema, no real-file grounding check, no validation, no trace event. Called by CodeEditWorkflow, BugFixWorkflow, TestGenerationWorkflow, DocumentationWorkflow — i.e. **every file-mutating workflow**.

Contrast with `agents/plan_mode.py`, ten feet away, which does it right: `generate_structured` with a JSON schema, `json_repair`, "reference REAL files from the context" rule, deterministic markdown render, written artifact.

**Failure story:** a 7B planner hallucinates `src/auth/middleware.py` (doesn't exist). The coder receives that as trusted context and writes an edit against a phantom file. This is the exact failure class the reliability doc fixed for tool calls (G1) — still open for plans.

**Fix:** converge `create_plan` onto plan_mode's contract: schema-constrained output, validate referenced files against the workspace (drop or flag phantoms), emit a `plan.created` trace. Behind an eval delta (F1).

**Related:** four planner implementations exist (`prd/project.py` deterministic spec, `plan_mode.py`, Taskmaster, `create_plan`) with no shared contract. Any fix should reduce that number, not add a fifth.

---

## D. Tooling (agent loop)

The loop's full toolset (`tools/agent_tools.py`): `list_files`, `read_file`, `grep_files`, `edit_file`, `write_file`, `run_command`, `search_index`, `git_status`/git ops, `ask_user`.

### D1. No web/browser access inside the loop — MEDIUM

**Evidence:** web and browser are **pre-routed** paths (`_looks_like_web_needed_prompt` → `_run_web_assist`), decided before the agent starts. `WebTool`/`BrowserTool` exist but are not registered in `AgentToolRegistry`.

**Failure story:** mid-build, the agent needs the actual signature of a library API. It can't look it up; it guesses from 7B weights. The user *has* a working WebTool, in the same process, that the agent is not allowed to touch. Worse, the routing is either/or: a prompt that needs web *and* files gets only one of the two.

**Fix:** register a `web_search`/`fetch_url` tool in the registry (approval-gated like `run_command`). The salvager and result budgeting (G12) already handle the output shape.

### D2. No delete / move / rename tools — MEDIUM ✅ FIXED 2026-07-17

> **Landed.** `move_file` and `delete_file` in `AgentToolRegistry`, both approval-gated and both routed through the same transaction machinery as every other model-driven write - so `/undo` covers them and a model deleting the wrong file is never unrecoverable (tests round-trip both through a real rollback). `move_file` refuses to clobber an existing destination rather than silently destroying it; both are sandbox-validated. `delete_file`'s description points at ask_user when several files could be the target, rather than guessing. Tests: `tests/test_move_delete_tools.py` (9).

**Evidence:** grep confirms no `delete_file`, `move_file`, or `rename` tool names in `agent_tools.py`.

**Failure story:** any refactor that relocates a file forces the model to `run_command` shell hacks (`mv`/`del` — allowlist-dependent, Windows/POSIX-divergent) or to write-new-and-leave-the-old, littering dead files that then pollute `search_index` results and future context packs.

**Fix:** add `move_file` and `delete_file`, both routed through the existing transaction/backup machinery (`transactions.backup_file`) so they're rollback-covered, both approval-gated.

---

## E. Verification & repair

### E1. The main agent loop never repairs — MEDIUM

**Evidence:** `RepairLoop` runs in `freeform_generator`, `full_pipeline`, `scaffold_pipeline` only. `AgentChatLoop._maybe_verify` calls `verify_only` — on failure it *tells* the user, full stop. The plan executor (`_verify_completed_plan`) likewise verifies-only.

**Failure story:** a 30-minute autonomous plan run ends "Plan UNVERIFIED: SyntaxError in game.js line 4". The machinery to fix a one-line syntax error exists in the same codebase and simply isn't invited. The user must start a new prompt, re-establish context (see A2), and hope.

**Fix:** on autonomous/plan verify failure, offer one bounded repair pass (`verify_and_repair` with `max_attempts=1-2`), gated on `long_running` so interactive chat stays untouched. The sync-RepairLoop-in-thread bridge already exists in `verify/gate.py`.

### E2. Interactive verify is lightweight-only — MEDIUM

**Evidence:** `verify/gate.py` — `lightweight=True` drops pip/npm installs and marks node builds **unverifiable**. All interactive/plan gates use lightweight.

**Failure story:** every JS/TS project build ends "unverifiable" — honest, but permanently so. Users of the most common webapp stack never get a verified verdict; the verify gate effectively doesn't exist for them.

**Fix:** when the heavy path is the only real verifier, *ask* ("verification needs `npm install` — run it?") instead of silently downgrading. An approval-gated heavy verify is strictly better than a permanent shrug.

---

## F. Measurement

### F1. The eval harness cannot enforce its own rule — HIGH ◑ PARTIAL 2026-07-17

> **Routing half landed.** `tests/test_routing_matrix.py` (30) is a prompt → expected-route matrix over real workspace fixtures: PRD-workspace routing, the differently-named-spec regression, ambiguous/empty workspaces, and plain-workspace routes (location, files, file.write, direct_code, git). Deterministic, no Ollama, ~3s. It also pins the B2 mirror with two guards — `test_dispatch_mirror_is_honest` (every detector the mirror consults is still consulted by the real dispatcher) and `test_mirror_and_dispatcher_agree_on_detector_order` (order *is* the routing logic in an if/elif chain, so the shared detectors must appear in the same relative order). That converts B2's silent drift into a loud test failure and gives G7's trim its dispatch-level net.
>
> **Still open:** planning evals (a plan must reference only real files — protects C1) and clarification evals (does the model ask when the decision is the user's — would measure J3, which shipped unmeasured). Neither the routing matrix nor `evals/cases.py` covers those yet.

**Evidence:** `evals/cases.py` — 6 cases, all single-turn agent-loop file operations (create/edit/bugfix/run/ask/QA). Zero routing evals, zero planning evals, zero PRD-build evals, zero multi-turn evals.

**Failure story:** the governing rule is "no prompt/loop change ships without an eval delta." Both bugs fixed on 2026-07-17 — the PRD→QA misroute and invisible CoT — sat **entirely outside** what any eval measures. The rule is currently unenforceable for the two highest-churn areas (routing keywords, planner prompts). Every keyword added to a `_looks_like_*` list ships unmeasured.

**Fix (highest-leverage single item in this doc):** add a routing eval set — a table of (prompt, workspace-fixture, expected-route) pairs run through `_classify_route_label` (or, post-B2, the real dispatch table). Deterministic, no Ollama, <1s. Then a small planning eval (plan for a fixture task must reference only real files). These directly protect B1, B2, C1.

### I3. Evals are single-sample, so small deltas are unreadable — MEDIUM (raised 2026-07-17)

**Found by using the harness in anger.** Each case runs ONCE against a stochastic local 7B, and the PASS/FAIL is reported as if deterministic. It is not: re-running `bugfix_syntax_error` on one unchanged commit gave PASS / FAIL / PASS, and the same commit scores 8/10 or 9/10 on the roll. A run that looked like a 2-case regression from the J6 change turned out to be noise — verified by re-running the cases, not by reasoning about them.

**Why it matters:** the governing rule is *no prompt/loop change ships without an eval delta*. If the harness cannot resolve a ±1 delta, that rule silently licenses both false alarms (reverting a good change) and false confidence (shipping a bad one). Consistent flips across re-runs ARE readable — `ask_before_choosing_an_approach` and `plan_references_only_real_files` both flipped and stayed flipped — so the harness is useful today, just not at ±1 resolution.

**Fix:** run each case N times and report a rate (2/3) rather than a boolean; treat a case as regressed only when the rate drops beyond the noise band. Cost is linear in N, so gate it (`--samples`, default 1 locally, 3 for a baseline). Then baseline the light/heavy tiers, which have never been measured at all (the original LOW finding — 3B models are far more salvage-dependent, so a light-tier regression is invisible today).

`BENCHMARK.md` records 6/6 on the default tier. Light/heavy tiers have never been baselined; a light-tier regression (3B models are far more salvage-dependent) would be invisible.

---

## G. Conversation & context

### A2. ~~The agent loop has no memory between prompts~~ → Cross-route continuity — MEDIUM ✅ FIXED 2026-07-17

> **CORRECTION.** The original claim here ("the agent loop has no memory between prompts", HIGH) was **wrong**, and the reasoning was sloppy: I saw `_run_agent_chat` never passes `state=` and concluded each prompt starts blank. It doesn't. `ChatState.__init__` calls `_hydrate_from_session()`, which replays up to `HYDRATE_MAX_MESSAGES` (80) turns from `messages.jsonl`. A fresh loop per prompt still sees the previous ones. **Verified empirically**, not by reading: a second loop was handed `[system, user("build a snake game"), assistant("I built snake.js"), user(...)]`. Amnesia between agent-chat turns was never real. Lesson recorded because the same mistake — auditing a call site without checking the constructor — is what the reliability doc calls fixing by feel.

**The real (narrower) gap:** only the agent loop ever *wrote* the hydratable transcript — `chat_state._append` is the sole caller of `session_logger.append_message`. Every route that answers **without** the loop (QA, direct code, PRD summary, git read, workspace answers) logged an `assistant.message` *event* but never appended to `messages.jsonl`. Those events are not what hydration reads.

**Failure story (real):** *Prompt 1:* "what does game.js do?" → QA route → good answer, invisible to the transcript. *Prompt 2:* "now add a pause button" → agent loop hydrates and sees **nothing** about game.js. Continuity worked only as long as you stayed on one route; crossing routes silently dropped the thread.

**Fix (landed):** `_audit_simple_turn` — which already meant "record a non-tool-loop turn" and already had both the prompt and the answer — now also appends both sides to the transcript. No double-append risk: the loop persists its own turns and never calls it. Also fixed `_run_direct_code_answer` returning `None`, which made its call site audit an **empty** final — the direct-code answer was being dropped from the trail entirely. Tests: `tests/test_cross_route_continuity.py` (7), including a characterization test pinning the hydration that already worked, so a refactor dropping `session_logger` can't silently introduce the amnesia this entry originally imagined.

**Still open (lower value):** `HYDRATE_MAX_MESSAGES = 80` is a flat cap — a long session silently loses its earliest turns rather than folding them into the rolling summary that `select_for_budget` already maintains in-loop.

### I2. Follow-up expansion covers only web/browser phrases — LOW

`_expand_followup_prompt` rewrites exactly two follow-up shapes ("check on the web", "open it in the browser"). "do that again but…", "same for the other file", "why did that fail?" all route as brand-new context-free prompts. Subsumed by A2 if ChatState persists; otherwise worth 5–10 more phrase families.

---

## H. Approvals & safety UX

### G1. Mid-loop approval is admitted-fragile on Windows — MEDIUM

**Evidence:** `repl.py` (`_run_agent_chat`): auto-approve exists partly to "sidestep the fragile mid-flow input() approval on Windows" — the workaround is documented in the code comment itself. Approval inside a running `console.status` spinner + `input()` on Windows garbles or hangs.

**Failure story:** during a non-auto-approved run, the approval prompt appears while the spinner owns the terminal; on some Windows terminals the keystroke goes nowhere. Users learn to enable autonomy (`auto_approve`) to avoid the jank — meaning the *safety* mechanism trains users to turn it off.

**Fix:** route approvals through `prompt_toolkit` (already a dependency, already running the session) instead of raw `input()`, pausing the status spinner around the prompt.

### G2. Rollback exists but is practically undiscoverable — MEDIUM ✅ FIXED 2026-07-17

> **Landed.** `/undo` reverts the most recent file change (approval-gated, same as `/patch rollback`), and any agent run that wrote files now prints "Not what you wanted? `/undo` reverts the last change." — the hint appears exactly when it's needed. Scope is stated honestly: each write is its own transaction, so `/undo` steps back one change at a time rather than reverting a whole run.
>
> **Bug found while building it:** `latest_undoable_transaction` first ordered by transaction id, on the reasoning that ids are timestamp-prefixed. They are — to *second* resolution (`%Y%m%dT%H%M%S-<uuid8>`). Two writes in the same second therefore sorted by their **random uuid suffix**, so `/undo` would revert an arbitrary one of them — and back-to-back agent writes are the common case, not an edge case. It passed in isolation and failed in the full suite (uuid lottery). Now ordered by the manifest's microsecond `created_at`, with the id only breaking exact ties. Tests: `tests/test_undo_command.py` (8), including 12 rapid same-second writes.

**Evidence:** every model-driven write IS transactional (`agent_tools.py`: "Every model-driven write goes through a transaction (backup + hash)") and `/patch rollback <transaction-id>` exists. But nothing at run end tells the user a transaction id exists, and there is no "undo the last run" verb.

**Failure story:** an autonomous run mangles three files. The remedy requires knowing that `/patch rollback` exists, then excavating the right id. In the moment of "the agent just broke my code", nobody finds that path; they reach for git and hope they committed.

**Fix:** print the transaction id in the run summary ("undo with `/patch rollback <id>`"), and add `/undo` as sugar for "rollback the most recent transaction of the last run."

---

## J. Asking the user (clarification & decisions)

*User-reported 2026-07-17: "it's not asking questions when the context is not enough or the agent needs decisions made by the user — not like Claude Code does."* The report is accurate, and it is not one bug — it is six. The machinery exists (`ask_user` tool, pending-question store, cross-turn answer resolution, a passing `ask_user_clarifies` eval) but is unreachable from most paths, actively discouraged where it is reachable, and lossy when it fires.

### J1. Only the agent chat loop can ask anything — HIGH

**Evidence:** grep across `shamsu/agents/*.py` — `ask_user` / `pending_question` are referenced only by `chat_loop.py` and `clarification.py`. The QA/specialist path (a single `run_specialist` call, no tools), `plan_mode`, CodeEditWorkflow, BugFixWorkflow, TestGenerationWorkflow, DocumentationWorkflow, the freeform generator, and the PRD milestone builder have **no ask path at all**.

**Failure story:** this compounds B1 lethally. An underspecified prompt misses every keyword → lands in QA (the catch-all) → QA *cannot ask* → it answers from guesswork. The paths that most need clarification — vague prompts — are routed to precisely the one place that has no way to request it. The user's complaint is structurally guaranteed.

**Fix:** give the specialist path an escape hatch: let QA-style responses carry a structured `needs_input` marker (schema-constrained, like plan_mode's output) that the REPL converts into a pending question. For the mutating workflows, thread the existing pending-question store through their entry points.

### J2. The stuck-loop clarify path is dead code — HIGH ✅ FIXED 2026-07-17

> **Landed.** `_ask_for_help_on_stall` routes the guard exits through the same pending-question flow as the model's own `ask_user` — cross-turn, no blocking `input()` (so it also sidesteps G1). Wired at the two guards where the **user plausibly holds the answer**: the repetition breaker (repeating one call = a missing *decision*) and the exhausted read-recovery (offers the candidate paths as options). Deliberately **not** wired at the empty-response and prose-only guards: those are model pathologies, and no user answer fixes them — they still stop plainly. `agent.stuck` telemetry preserved (now with `asked: true`). Dead code deleted: `clarify_prompt`, the `ask_clarifying_question` import, `_give_up_on_repetition`, `_read_blocked_final`. Tests: `tests/test_chat_loop_clarify.py` (8).

**Evidence:** `chat_loop.py:293` — `self.clarify_prompt = clarify_prompt if long_running else None`. That is the **only** occurrence of `self.clarify_prompt` in the file: assigned, never called. `safety/clarify.py` (`ask_clarifying_question`) exists solely to serve it — its docstring says it's for "a long-running loop [that] keeps repeating the same action with no progress." Meanwhile every stall guard the loop actually has — repeated-call breaker (`_MAX_REPEATED_CALLS`), read recoveries (`_MAX_READ_RECOVERIES`), prose corrections (`_MAX_PROSE_CORRECTIONS`), empty-response nudges (`_MAX_EMPTY_RESPONSES`) — ends the run with a give-up message instead of asking.

**Failure story:** a long autonomous run hits the repetition ceiling because it genuinely needs a decision (which config file is authoritative?). The designed behavior — pause and ask — was never wired; the actual behavior is "stop and report failure." The user reads it as the agent giving up on something it could have asked one question about. They're right.

**Fix:** call `self.clarify_prompt` (or better: route through the same `ask_user` pending-question path, so it works cross-turn) at the guard exits, once, before giving up. The function, the store, and the resume flow all already exist — this is a wiring job.

### J3. Mixed prompt signals make a 7B model never ask — MEDIUM-HIGH ✅ FIXED 2026-07-17

> **Landed.** `ask_user`'s description is now framed on **decision ownership** — "ask when the answer is THEIRS to give: choosing between valid approaches or designs, naming, scope, anything destructive or hard to undo, an ambiguous target… Asking one good question is cheap; acting on a wrong guess is expensive" — with the fact-finding steer kept ("look up plain facts with find_file/grep_files/read_file yourself") but the "only ask when genuinely blocked" framing removed. The clarification rules gained the same threshold plus two concrete examples (sessions-vs-JWT; two candidate config files). Effect is unmeasured until the clarification evals under F1 exist — this is a prompt change, and the doc's own rule says prompt changes need an eval delta.

**Evidence:** the system prompt says ask ("Ask the user a clear question (call ask_user) when required input is missing", chat_loop.py:143). The tool's own description says the opposite: *"Prefer find_file/grep_files/list_files first; **only ask when genuinely blocked**… Calling this ends your turn"* (`agent_tools.py`). Small models weight the discouragement; three "prefer not to" clauses beat one "do ask."

**Failure story:** the `ask_user_clarifies` eval passes because its prompt is *unambiguously missing* an input — the ideal case. Real vague prompts ("make it better") never look "genuinely blocked" to a 7B model: it can always do *something*, so it does, and the user gets confident wrong action instead of one good question. Claude Code's behavior — ask when a **decision is the user's to make**, not merely when execution is impossible — is a different and better threshold.

**Fix:** rewrite the tool description around decision ownership ("ask when the user's answer would change what you build: naming, scope, destructive choices, ambiguous targets") and drop the "only when genuinely blocked" framing. Add 1–2 few-shot ask examples to the long-running prompt. Cheap, high-yield, and measurable once F1's routing/clarification evals exist.

### J4. Answering a question restarts the world — MEDIUM

**Evidence:** `repl._resolve_pending_question` (repl.py:1301) rewrites the reply to `original prompt + "(Answering the earlier question …)"` and dispatches it as a **brand-new request**: full keyword routing runs again, and a fresh agent loop starts with zero memory of the tool calls that led to the question (A2).

**Failure story:** the agent reads five files, asks "should auth use sessions or JWT?", the user says "JWT" — and the resumed run re-lists, re-greps, re-reads everything it already knew, burning minutes of local-model time. Worse, the appended answer text can trip a *different* keyword rule than the original prompt did, so the answer may not even return to the same workflow that asked.

**Fix:** short-circuit routing for question answers — resume the route recorded when the question was stored (`set_pending_question` already persists context; add the route). Full state resume is the A2 fix; recording the route is a one-line down-payment.

### J5. A question asked mid-plan execution is effectively lost — MEDIUM ✅ FIXED 2026-07-17

> **Landed.** `_execute_plan` now checks `result.awaiting_user` after every step. It was worse than "the question waits": the step was marked **done** — a plain lie — and later steps ran on the unanswered assumption. Now the plan pauses at the asking step (left `running`, not `done`), `_pause_plan_for_question` records `{awaiting: "plan_resume", steps, resume_index, changed_files}` next to the pending question, and the user's answer re-enters `_execute_plan` at that step via `_resume_paused_plan` (reusing the milestone/verify machinery rather than duplicating it). The paused record is popped on resume, on decline/cancel, and when a slash command clears a stale question — otherwise it would sit armed and fire off some later unrelated answer. Tests: `tests/test_plan_pause_on_question.py` (5).

**Evidence:** the pending-question check lives only at the top of the REPL prompt loop (repl.py:7686). `_execute_plan`'s `for` loop over steps never checks `get_pending_question` between steps — if step 2's agent pass calls `ask_user`, the loop proceeds to step 3 anyway, and a later step can overwrite the stored question.

**Failure story:** mid-plan, the agent legitimately asks which database file is canonical. Nobody is looking at the pending store until the whole plan finishes; steps 3–7 run on the unanswered assumption, and the eventual answer is applied to a world that already moved on.

**Fix:** after each step, if a pending question exists: pause the plan (the pending-action store already supports resumable state — same mechanism as plan approval), surface the question, resume from the same step index on answer.

### J6. No upfront decision-asking before acting on vague requests — MEDIUM ✅ FIXED 2026-07-17

> **Landed, and it is what finally made J3 real.** The prompt-only reframing of `ask_user` measurably did NOT work: `ask_before_choosing_an_approach` stayed red. The reason is structural, not lexical — *mid-loop*, a model that can always do *something* just does it. No wording fixes that.
>
> The fix moves the decision to before any work starts, and costs **zero extra model calls**: the chat loop already made one planner call per request (`_append_plan` -> `create_plan`). That call is now schema-constrained and returns `needs_input` / `question` / `options` alongside the plan. When the planner says the decision is the user's, `run()` ends the turn with the question through the same pending-question flow as `ask_user` — so it survives the turn and the answer resumes the work. `SHAMSU_ASK_UPFRONT=0` restores straight-to-work.
>
> **Measured:** `ask_before_choosing_an_approach` FAIL -> PASS (asks sessions/JWT/OAuth with concrete options, in ~6s), while `create_file`, `edit_file_targeted`, `ask_user_clarifies` and the `does_not_ask_when_unambiguous` negative guard all still pass — it did not start asking about everything, which was the real risk. Tests: `tests/test_chat_loop_planner.py` (9).
>
> **It also exposed a hidden test dependency:** `test_model_timeout_stops_loop` built a loop with no `llm=`, so its planner call reached live Ollama — a test of the *client* timeout quietly depended on a model being up. It surfaced only when the planner started (correctly) judging "do something" too vague to act on. Now injects a planner double.

**Evidence:** `_looks_like_vague_action_request` ("do it", "go", "continue") routes straight to a full PRD build — the code's own comment calls this "safe to route here" because "the build is approval-gated anyway." The PRD pipeline silently picks theme (`_select_theme`), archetype (`classify_archetype`), and stack via heuristics. `plan_mode` writes a plan without ever asking a clarifying question first, even when the task is one ambiguous sentence.

**Failure story:** approval-gating is a yes/no on *whether* to act — it cannot express "yes, but make it a REST API, not a webapp." The user's only lever is rejecting the whole build after watching it start wrong. Claude Code's pattern — 1–3 targeted option-questions *before* acting when requirements are genuinely ambiguous — front-loads exactly the decisions SHAMSU currently guesses.

**Fix:** at the two highest-stakes entry points (PRD build kickoff, plan_mode generation), when confidence signals are weak (terse prompt, multiple archetype candidates, `category_decision` confidence low), ask one option-style question via the existing pending-question store before committing. Bound it: never more than one round, never for detailed prompts.

---

## I. Architecture & performance

### H1. Retrieval is FTS-only — LOW-MED

**Evidence:** `retriever/search.py` — `SearchAgent.search` = `fts_search` (Codebase-Memory FTS) + path boosting. No embeddings anywhere in `retriever/` or `indexer/`.

**Failure story:** "where is authentication handled" finds nothing unless a file literally contains those words; the code says `login`, `session`, `jwt`. The agent then plans/edits with partial context. Worst on exactly the vague prompts where retrieval matters most.

**Fix (cheap first step):** LLM-side query expansion — have the router model emit 3–5 synonym terms and union the FTS results — before considering a real embedding index (Ollama can serve `nomic-embed-text` locally, but that's a bigger lift).

### H2. Taskmaster duplicates in-repo capability at high cost — LOW-MED

**Evidence:** `taskmaster/adapter.py` — external npm tool (`task-master-ai`), Node dependency, 900-second AI-call timeouts, health/repair subsystem. Meanwhile `prd/project.py` builds task graphs deterministically and `plan_mode` + `MilestoneTask` execute stepwise plans without it.

**Failure story:** PRD/task-graph flows hard-require (`REQUIRED_TASKMASTER_MESSAGE`) a Node toolchain that SHAMSU's core never needs otherwise; a broken npm install blocks `/prd` workflows entirely, and each Taskmaster AI call re-runs a local model for minutes to produce what `build_project_spec` approximates in milliseconds.

**Fix:** decision, not code: either commit (make it the *one* planner for PRD flows, delete overlap) or demote it to optional-enhancer with the in-repo path as default. Needs a product call; flagged, not prescribed.

### I1. Agent-loop answers don't stream — LOW

**Evidence:** `chat_loop.py` sends `stream=False` on its chat calls; the specialist QA path streams (`run_specialist_stream`). Long final answers pop in all at once after a silent wait — inconsistent with the rest of the REPL. Fix when convenient; the heartbeat mitigates the perceived hang.

---

## Suggested order of attack

1. **A1** (crash guard) — smallest change, biggest single reliability win.
2. **J2 + J3** (wire the dead clarify path; fix the ask_user prompt signals) — both are small, both directly answer the user-reported "it never asks me anything," and neither needs new infrastructure.
3. **F1** (routing + clarification evals) — unblocks everything else per the "no change without eval delta" rule.
4. **B2 + I4** (data-driven dispatch, one change) — kills the mirror drift and finishes G7 safely behind the new evals.
5. **A2 + J4** (persistent ChatState; resume-not-reroute on answers) — the largest user-perceived quality jump, and J4 falls out of A2 almost for free.
6. **C1** (ground `create_plan`) — closes the last ungrounded model output feeding a coder.
7. **J5 + J6** (pause plans on questions; upfront asks at the two big entry points) — completes the clarification story.
8. **G2, D2, E1** (undo discoverability, move/delete tools, bounded repair) — quality-of-life batch.
9. **B1 + J1** (LLM-router fallback before QA; needs-input escape hatch for specialist paths) — the two halves of "vague prompts stop silently degrading."
10. **D1, E2, G1, H1** as capacity allows; **H2** needs a product decision first.
