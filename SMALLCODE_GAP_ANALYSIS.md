# SHAMSU vs SmallCode — where we stand, what's missing, what to take

**Date:** 2026-08-20
**SHAMSU reviewed:** `Shamsu/shamsu/` (304 Python files, v0.4.0b1, SIMPLE MODE default)
**SmallCode reviewed:** `Shamsu/reference/smallcode/` (v1.6.0, Node/JS)

> **Note on repo layout:** the live package is `Shamsu/shamsu/`. `Shamsu/src/shamsu/`
> is an empty leftover directory tree (0 `.py` files) — anything that still says
> "the package lives under `src/`" is stale.

---

## 0. The one-paragraph summary

SHAMSU is **not behind SmallCode**. On the things that matter most for small
models — patch-first editing, two-stage tool routing, hybrid search, context
budgeting, evidence capture, truncation detection — SHAMSU has either already
ported SmallCode's idea or built something better (its token accounting is
calibrated against real `prompt_eval_count` numbers; SmallCode still uses
`chars/4`). The gap is **not** in the big architecture. It is in a specific
band of **cheap, boring safety nets** that SmallCode has and SHAMSU does not:
guards that catch a bad situation *before* it costs a round, and **recovery
paths that give the model a third option when its first two fail**.

Your two reported bugs land almost exactly on that gap.

---

## 1. Issue 1 — "generated code is trimmed at the end / incomplete files"

### What SHAMSU already does right

This is not an unhandled case. SHAMSU has a real, working truncation story:

| Mechanism | Where | What it does |
|---|---|---|
| Dynamic reply cap | [simple_chat.py:1973](shamsu/agents/simple_chat.py#L1973) `_reply_cap` | Gives the reply *all* the free window, not a fixed share. Floor = `output_reserve`, ceiling = `MAX_REPLY_TOKENS` (16,384). |
| Truncation detection | [simple_chat.py:1892](shamsu/agents/simple_chat.py#L1892) `_hit_the_length_limit` | Reads Ollama's `done_reason == "length"`. |
| Refuse the partial write | [simple_chat.py:2815](shamsu/agents/simple_chat.py#L2815) `_refuse_truncated_write` | If the generation was cut off, the **last** tool call is not executed. Nothing partial hits disk. |
| Escape hatch | [simple_chat.py:3474](shamsu/agents/simple_chat.py#L3474) `_refuse_unwritable_rewrite` | A file bigger than one reply can hold cannot be rewritten whole; model is pushed to `patch_file`. |
| Post-write check | [simple_verify.py](shamsu/agents/simple_verify.py) | Real parse per language, `skipped` said out loud rather than faked. |

So *why are you still seeing trimmed files?* Four concrete holes.

### Hole 1.1 — a brand-new file has **no** structural gate at write time

Both write-time gates in the tool layer bail out immediately when the target
does not already exist:

```python
# agent_tools.py:3777  _breaks_working_python
if target.suffix.lower() != ".py":   return ""   # ← Python only
if not target.is_file():             return ""   # ← new file = no check

# agent_tools.py:3811  _gutting_overwrite
if not target.is_file():             return ""   # ← new file = no check
```

**Plain terms:** SHAMSU will refuse to *damage* a good file, but it will
happily *create* a broken one. And it only truly understands Python. A new
`game.js` that stops mid-function has nothing standing between the model and
the disk — the only thing that notices is `simple_verify`, which runs *after*
the file is already written.

The parser SHAMSU needs is already sitting in the codebase:
`simple_verify.bracket_problem` / `check_file` handle JS, TS, JSX, CSS, JSON,
Python. They are simply not called on the *content argument* before the write.

### Hole 1.2 — no size limit on tool-call arguments

SmallCode treats this as a hard runtime constraint, stated three separate times
in three places:

```js
// bin/smallcode.js:2085 (system prompt)
CRITICAL — large file rule: write_file calls are limited to 60 lines / ~8KB.
llama.cpp's JSON parser crashes on larger tool calls. For any file over 60 lines:
(1) write_file with just the skeleton, then (2) use multiple patch calls...

// bin/executor.js:209 (enforced in the tool)
append_file: chunk too large (${KB}KB). Keep each append under 60 lines.

// bin/tools.js:15 (in the schema the model reads)
write_file — LIMIT: 60 lines / 8KB max.
```

SHAMSU allows a single reply of **16,384 tokens ≈ 60 KB ≈ 1,500 lines**
([simple_chat.py `MAX_REPLY_TOKENS`](shamsu/agents/simple_chat.py#L327)), and
SHAMSU's own prompt mentions `append_file` **without ever naming a number**:

```python
# simple_prompt.py  BIG_FILE_CAPABILITY
"For a file too big to write in one go: write_file the first section,
 then append_file the rest."
```

"Too big" is not a number a 3B model can act on. SmallCode says "60 lines".
This is the single cheapest fix on this whole list.

### Hole 1.3 — `done_reason == "length"` is the **only** truncation signal

The refusal fires only when Ollama admits it ran out of room, **and only for
the last call in the turn** ([simple_chat.py:2646](shamsu/agents/simple_chat.py#L2646)).
Every other way a generation goes short slips through:

- the model just stopped early (`done_reason: "stop"`, content incomplete);
- the tool-call JSON was mangled by the template/JSON parser rather than by the
  token cap (the llama.cpp limit above);
- the truncated write was not the final call in a multi-call turn.

In all of those, the partial content is written and reported `ok`.

### Hole 1.4 — refusal instead of recovery

When a write is cut off, simple mode **throws the content away** and asks the
model to start again in sections. Three of those in a row and the turn stops.

The legacy loop actually had the better answer, and it was never carried over:

```python
# chat_loop.py:4465  _truncated_write_correction
"Do not re-send the whole file (that is what truncated). Call append_file on
 {path} with ONLY the missing remainder, continuing exactly from where this
 current tail ends..."   ← plus the last 12 lines of the file, verbatim
```

Continue-from-the-tail is strictly better than start-over: the good 80% is
kept, and the model only has to produce what is missing. But the legacy version
is `.py`-only, and simple mode has no version of it at all.

SmallCode also does the simplest possible thing first — **retry once with
double the budget**:

```js
// bin/smallcode.js:2672
if (_finish === 'length' && _emptyContent && !body.__lengthRetry) {
  const retryBody = { ...body, max_tokens: Math.min(_curMax * 2, _cap) };
```

### Hole 1.5 — unknown models silently drop to an 8k window

```python
# context/budget.py:27-41
MODEL_CONTEXT_WINDOWS = { ...9 hardcoded model names... }
SAFE_FALLBACK_CTX_WINDOW = 8_192   # anything not in the list
```

If you are running a model that is not one of those nine exact strings, SHAMSU
asks for an 8k window. At 8k with a 4k prompt, the reply cap collapses to a few
thousand tokens — roughly 10–15 KB of code — and *everything* larger truncates.
Worth checking against whatever model you were actually running when you saw
this.

SmallCode instead keeps per-model profiles with **pattern fallback**
(`src/model/profiles.js`) carrying `context_length`, `max_output`,
`tool_format`, `strengths`, `weaknesses`. SHAMSU's `ModelSpec`
([runtime/models.py:26](shamsu/runtime/models.py#L26)) has only
`supports_native_tools` and `is_reasoning` — no output ceiling, no tool format.

---

## 2. Issue 2 — "asking it to fix a file fails; patching doesn't work; it reads the whole large file and gives up"

This is a **dead-end chain**, not one bug. Each link is individually
reasonable; together they leave the model with no move left.

```
1. read_file(big_file)          → head-only cut at 24,000 chars
2. model patches from what it saw → old_string not found (it never saw that part)
3. patch retried                  → fuzzy match fails too
4. model tries write_file whole   → REFUSED (partial read + too big to rewrite)
5. ...no third option exists      → spins, then stops
```

### Link 1 — the read cap is a fixed byte count, not context-aware

```python
# agent_tools.py:101
MAX_READ_CHARS = 24000
# simple_chat.py:81
MAX_TOOL_RESULT_TOKENS = 8000
```

Reading a large file with no line range gives back **the first 24,000
characters and nothing else** ([agent_tools.py:1831](shamsu/tools/agent_tools.py#L1831)),
then `_budgeted` ([simple_chat.py:4409](shamsu/agents/simple_chat.py#L4409))
trims again to 8,000 tokens. Both include a decent hint naming the next call —
that part is good, and better than SmallCode's old behaviour.

What's missing is the **trigger logic**. SmallCode's read guard
(`src/session/read_guard.js`) does not use a fixed number at all:

```js
const aggressive = window > 0 &&
  (usagePct >= budgetPct || (fileTokens * 2) >= window);
// → return the first 30 lines (imports + signatures) + an explicit directive
```

Two triggers: **live context already past budget**, or **this one file is over
50% of the whole window**. When either fires it returns only ~30 lines — enough
to see the shape of the file — plus a directive to grep and then read a range.
Otherwise it does a head **and tail** trim, so the model at least sees how the
file ends.

SHAMSU's version is head-only and always the same size regardless of how full
the window already is. On a session that's already 70% full, a single big read
can push it over; on an empty session, a 24k cut is needlessly stingy.

### Link 2 — `patch_file` has good matching but no last resort

SHAMSU's ladder ([agent_tools.py:2077–2146](shamsu/tools/agent_tools.py#L2077)) is
already strong:

1. exact match →
2. decode literal `\n` escapes →
3. `_fuzzy_match_block` →
4. context-candidate disambiguation →
5. error + `_edit_recovery_excerpt` showing the real nearby text.

SmallCode has a step 6 that SHAMSU does not — **semantic merge**:

```js
// bin/executor.js:252
if (count === 0) {
  const merged = await semanticMerge(args.path, args.new_str, content);
  if (merged) { fs.writeFileSync(filePath, merged); ... }
}
```

When `old_str` cannot be found, it asks the model to *merge the intended change
into the file it actually has*, and returns the whole corrected file. It is a
full-file replacement rather than a surgical patch — deliberately, because the
surgical option has already provably failed. This converts a hard error into a
recovery attempt.

### Link 3 — repeated failure is blocked, but no new strategy is offered

`_refuse_repeated_failure` ([simple_chat.py:2775](shamsu/agents/simple_chat.py#L2775))
correctly refuses to run a byte-identical failing call a tenth time, and hands
back the error the model apparently couldn't see. But the advice it gives is
*the same approach again*: "call read_file and copy the text character for
character." If the read is the thing that's clipped, that advice is a loop.

SmallCode escalates instead. `EarlyStopDetector.recordPatchResult`
(`src/governor/early_stop.js:110`) counts failures **and** total attempts per
file, and at 4 failures or 6 attempts forces a strategy change:

```js
[SYSTEM] You have attempted to patch ${filePath} ${totalAttempts} times.
The file is likely corrupted or your patches don't match. STOP using patch. Instead:
1. read_file to see the current state
2. Decide what the ENTIRE file should contain
3. write_file to rewrite it completely.
Do NOT attempt another patch on this file.
```

And above that sits `decomposeTask` (`bin/features_adapter.js`), where the model
picks a named strategy — `split_file` / `one_error_at_a_time` /
`rewrite_section` / `extract_function` — with a concrete instruction, when a
file keeps failing after all retries.

**This is the "doesn't take any other approaches" you described, exactly.**

### Link 4 — the two refusals can lock each other

- After a clipped read, the path lands in `_partial_reads`
  ([simple_chat.py:2708](shamsu/agents/simple_chat.py#L2708)) → whole-file rewrite refused.
- `_refuse_unwritable_rewrite` → files over the reply budget cannot be rewritten anyway.
- `patch_file` keeps missing because the model never saw the relevant region.

There *is* a legitimate way out — read in ranges until `_seen_ranges` covers the
file ([simple_chat.py:3387](shamsu/agents/simple_chat.py#L3387)) — but nothing
puts that path in front of the model at the moment it is stuck. It has to
invent it.

---

## 3. Full feature comparison

### 3.1 Already have it (equal or better than SmallCode)

| SmallCode feature | SHAMSU equivalent | Notes |
|---|---|---|
| Patch-first editing | `patch_file`, `read_and_patch`, prefer-patch refusals | SHAMSU's matching ladder is **richer** (fuzzy + escape-decode + candidate disambiguation) |
| 2-stage tool routing | [`agents/simple_router.py`](shamsu/agents/simple_router.py) | Direct port, same 16,384 threshold, credited in the docstring |
| Hybrid code search | [`tools/hybrid_search.py`](shamsu/tools/hybrid_search.py), `search_files` with `hybrid\|regex\|keyword\|semantic` | Parity |
| Context budget engine | `context/budget.py`, `_elide_under_pressure`, turn-boundary compaction | **Better** — SHAMSU calibrates against real `prompt_eval_count`; SmallCode still estimates `chars/4` |
| Evidence store | `_record_evidence` (simple_chat) | Already ported, and the docstring credits SmallCode |
| Working memory | `memory_remember/load/list/forget` + FTS5 relevance | Parity |
| Composite tools (`find_and_read`, `read_and_patch`, `create_and_run`) | Same four, `_COMPOSITE_TOOLS` | Parity |
| Conditional capability prompt | `RECALL/BIG_FILE/GRAPH_CAPABILITY` in `simple_prompt.py` | Same idea, same reasoning (SmallCode issue #58) |
| Benchmark harness | `evals/` + `BENCHMARK.md`, repeat-N runs | Parity on running; see gaps for diffing |
| Reply budgeting / cut-off honesty | `_reply_cap`, `_out_of_room_message` | **Better** — SHAMSU names *which* limit bound and gives advice that fits it |

### 3.2 Have it partially

| Feature | What SHAMSU has | What's missing |
|---|---|---|
| **Forgiving tool-call parser** | `llm/output.py` — JSON, `<tool_call>` XML, SEARCH/REPLACE, `functions.` prefixes, arg aliases (`path`→`filepath`, `old`→`old_string`) | No Hermes format, no YAML, no Liquid AI `<\|tool_call_start\|>` markers, **no fallback to scanning `reasoning_content` when content is empty** |
| **Thinking budget** | `_should_disable_thinking` — ported from SmallCode, disables thinking after the first repair round | No token budget, no hard truncation of an oversize thinking block before it enters history |
| **Early-stop detection** | Repeated reads, unproductive edits, identical-failure refusal, prose nudges, promise nudges | No patch-spiral→forced-rewrite; no greeting regression; no in-stream repetition detection (SHAMSU runs `stream: False`) |
| **Model profiles** | `ModelSpec` (`supports_native_tools`, `is_reasoning`) + `MODEL_CONTEXT_WINDOWS` (9 exact names) | No `max_output`, no `tool_format`, no strengths/weaknesses, **no pattern fallback** — unknown model → 8,192 |
| **Snapshot / rollback** | Per-mutation transactions, `.shamsu/mutations/`, trash, `/undo`, `/patch rollback` | No **turn-level checkpoint** that auto-reverts *all* edits in a turn when validation hard-fails |
| **Plan-then-execute** | `plans/`, `plan_mode.py`, `taskmaster/` — legacy path | Simple mode (the default) has **no plan anchor at all**; no plan re-injection on later turns |
| **Contract / definition of done** | `verify/contract.py`, `runtime/phase_contracts.py`, `verify/gate.py` | Legacy/PRD-only. Simple mode has no done-guard, no `/contract`, no user-declarable assertions |
| **Code intel routing** | `search` category contains `graph_search` + `explain_symbol` | Not a dedicated `code_intel` category placed *before* `search`, so "who calls Y" can still pull write tools into scope |
| **Tool-name repair** | `canonical_tool_name` + aliases | A genuinely hallucinated name returns a bare `Unknown tool: X` with **no closest-match suggestion** |

### 3.3 Missing entirely

Ordered by how much they'd help *your two bugs*:

| # | Feature | SmallCode source | Why it matters here |
|---|---|---|---|
| 1 | **Context-aware read guard** | `src/session/read_guard.js` | Directly fixes Issue 2, link 1 |
| 2 | **Semantic merge on patch failure** | `bin/features_adapter.js:352` | Directly fixes Issue 2, link 2 |
| 3 | **Patch-spiral → forced rewrite** | `src/governor/early_stop.js:110` | Directly fixes Issue 2, link 3 |
| 4 | **Decompose task (LLM strategy pick)** | `bin/features_adapter.js` | The "other approaches" that never get tried |
| 5 | **Length-retry with doubled budget** | `bin/smallcode.js:2672` | Cheapest partial fix for Issue 1 |
| 6 | **Read-before-write guard** | `src/tools/read_tracker.js` | Stops blind overwrites of unread files |
| 7 | **Tool-call dedup** (pure tools + idempotent writes) | `src/tools/dedup.js` | Saves context and latency on looping models |
| 8 | **Quality monitor** | `src/governor/quality_monitor.js` | Empty turns, blank/hallucinated tool names with suggestions, cross-turn exact repeats |
| 9 | **Per-tool trust decay** | `src/tools/trust_decay.js` | 3 fails → demote, 5 fails → drop from schema for the session |
| 10 | **Bootstrap detection** | `src/session/bootstrap.js` | One-line project summary on turn 1; saves 3–5 discovery calls |
| 11 | **Test-runner auto-discovery** | `src/tools/test_runner.js` | Model knows `pytest -q` / `npm test` without hunting |
| 12 | **Error diagnosis** | `bin/features_adapter.js` `diagnoseError` | Typed `[ERROR-DIAGNOSIS]` hint prepended to a failed command's output |
| 13 | **Adaptive retry temperature** | `src/model/adaptive_temp.js` | Attempt 1 colder, attempt 2 hotter — stops three identical broken outputs |
| 14 | **Persistent shell sessions** | `src/tools/shell_session.js` | `cd src` then `pytest` currently loses the `cd` |
| 15 | **Knowledge injection** | `src/knowledge/loader.js` + `knowledge/` | Keyword-matched cheat sheets into the system prompt (~1,500 token budget) |
| 16 | **Multi-file edit coordination** | `[MULTI-FILE-EDIT]` header at ≥3 files/turn | Stops forgetting file 3 while editing file 2 |
| 17 | **File state tracker / diff re-reads** | `src/session/file_state.js` | Re-reading a known file returns a unified diff instead of the whole thing |
| 18 | **Benchmark diff with exit codes** | `bench/diff.js` | 0 improved / 1 regressed / 2 noise — CI-usable A/B |
| 19 | **MarrowScript cognition layer** | `marrow/`, `src/compiled/` | Declarative prompts → caching, retry, validation, traces, budgets for free |

### 3.4 Deliberately not for us

| Feature | Why not |
|---|---|
| **Model escalation to cloud** (Claude/GPT/DeepSeek on hard fail) | Violates SHAMSU's stated invariant #5, *local-first: no cloud inference*. This is a product decision, not an oversight. |
| **BoneScript** | Node/TS-specific codegen. SHAMSU has its own Django writer + scaffolds. |
| **MarrowScript, as a language** | The *outcomes* (prompt caching, traces, retry, budgets) are worth having; adopting a DSL + compiler toolchain to get them is not a good trade for a Python codebase. Take the outcomes, skip the compiler. |

---

## 4. Recommended plan

### Tier 0 — fixes your two bugs, all small, all local

1. **Run the structural check on the write content, not just after the write.**
   `simple_verify.check_file` / `bracket_problem` already exist and handle JS,
   TS, JSX, CSS, JSON and Python. Call them on the `content` argument in
   `write_file` — *including for new files* — and refuse a write whose brackets
   or strings don't close. This closes hole 1.1 for every language, not just
   Python.

2. **Put a number in the prompt and in the schema.** Copy SmallCode's phrasing:
   "write_file is limited to ~60 lines / 8 KB; for anything larger, write a
   skeleton then `append_file` each section." Add the same sentence to the
   `write_file` and `append_file` tool descriptions, and enforce it in the tool
   with a clear error. A 3B model acts on "60 lines"; it cannot act on "too
   big".

3. **Recover instead of refusing.** Port `_truncated_write_correction` from
   `chat_loop.py` into simple mode, and make it language-agnostic: when a write
   arrives cut off, show the model the last ~12 lines of what *did* land and ask
   for `append_file` with only the remainder. Keep the current refuse-and-stop
   as the fallback after 2–3 tries.

4. **Length-retry once with a doubled cap** before doing anything else, when
   `done_reason == "length"` *and* the reply was effectively empty
   (`bin/smallcode.js:2672`). Roughly 15 lines of code.

5. **Context-aware read guard.** Replace the fixed `MAX_READ_CHARS = 24000` cut
   with SmallCode's two triggers (live context past budget, or file > 50% of
   window) → return the first ~30 lines plus an explicit "grep, then read a
   range" directive. Keep head **and tail** in the non-aggressive case so the
   model can see how the file ends.

6. **Semantic merge as `patch_file`'s last resort.** When `old_string` is not
   found *and* fuzzy matching also fails, ask the model to merge the intended
   change into the file's real current content and write the result. Behind an
   env flag, and gated on file size so it can't be used to launder a whole-file
   rewrite of a 3,000-line file.

7. **Patch-spiral escalation.** Extend the existing `_stalls` counters: count
   attempts per *file* (not just per identical signature), and at 4 failures or
   6 attempts inject a strategy change — read current state, then rewrite the
   affected *section*. Right now the refusal message recommends the approach
   that just failed.

8. **Fix the unknown-model fallback.** Add pattern-based context detection
   (`qwen3*` → 32k, `*-coder*` → 32k, etc.) so an unlisted model doesn't
   silently collapse to 8,192, and add `max_output` to `ModelSpec` so the reply
   cap respects what the model can actually emit.

### Tier 1 — cheap, high-value, low-risk

9. Read-before-write guard (one-shot refusal, `patch_file` counts as a read).
10. Tool-call dedup for pure read tools + the stricter same-turn guard for
    `memory_remember` / `memory_forget`.
11. Quality monitor — especially closest-match suggestions on a hallucinated
    tool name, which today returns a bare `Unknown tool: X`.
12. Bootstrap detection + test-runner auto-discovery, both injected once into
    the system prompt. SHAMSU already has most of the machinery in
    `tools/project_env.py`; it just isn't summarised into the prompt.
13. Adaptive retry temperature — a few lines, and it stops three identical
    broken outputs in a row.

### Tier 2 — worth doing, more work

14. Turn-level snapshot + auto-rollback on validation hard-fail (build on the
    existing `.shamsu/mutations/` transactions).
15. Per-tool trust decay.
16. Plan anchor in simple mode — numbered plan on multi-step tasks, re-injected
    each turn.
17. Error diagnosis on non-zero exits, cached.
18. Knowledge injection directory.
19. Benchmark diff tool with exit-coded verdicts for `evals/`.
20. Persistent shell sessions (note: needs care on Windows + the existing
    command risk classifier and path sandbox).

### Tier 3 — evaluate later

21. The MarrowScript *outcomes* — a small prompt-call wrapper giving caching
    (content-hash + TTL), retry with validation, and trace IDs to every internal
    LLM call (router, classifier, summariser, semantic merge, decompose). Not
    the DSL; just the wrapper. SHAMSU already depends on `diskcache`.
22. Multi-file edit coordination header.
23. File state tracker / diff-based re-reads.

---

## 5. Honest scorecard

| Area | Verdict |
|---|---|
| Core loop architecture | **SHAMSU ahead** — smaller prompt, better token accounting, honest failure reporting |
| Editing primitives | **SHAMSU ahead** on matching, **behind** on recovery |
| Guards & safety nets | **SmallCode clearly ahead** — this is the whole gap |
| Recovery paths ("what do I do when stuck") | **SmallCode clearly ahead** |
| Verification | **SHAMSU ahead** — real parsing, explicit `skipped`, no fake "checked" |
| Context management | **SHAMSU ahead** — calibrated tokens vs `chars/4` |
| Onboarding a new workspace | **SmallCode ahead** — bootstrap + test runner |
| Measurement / CI | Roughly even; SmallCode has the A/B diff, SHAMSU has repeat-N variance handling |
| Local-first discipline | **SHAMSU ahead** by design |

The pattern is consistent: **SHAMSU builds the right mechanism and then gives
the model one way out of a bad state. SmallCode gives it three.** Everything in
Tier 0 is about adding the second and third exit.

---

# 6. Restoring the headroom ratio (design note)

> **Added 2026-08-20 after reviewing both loops. This section is the fix plan for
> the truncation problem — read it with §1.**

## 6.0 The decision this is built on

**More tool calls is the correct trade. Truncation is not.**

Stated by the project owner, 2026-08-20:

> "I don't want to have fewer tool calls if I get the truncation issue. Later
> fixing them will cost me more time and tokens. Doing it in small steps at a
> time would be more reasonable for us."

This overrides the assumption baked into `MAX_REPLY_TOKENS = 16384` — that
bigger single generations are the goal. From here on, **the unit of work is
bounded, and the number of calls is allowed to grow to fit it.**

Two things make this a better trade than it first looks:

1. **A truncated write is not a slow path — it is a pure-waste path.** Every
   token generated after the cut is 100% loss: the content is refused, and the
   model no longer has it either. A 500-line file that truncates burns the full
   ~8,000-token reply budget and produces *nothing*. The same file written in
   six chunks burns roughly 4,000 output tokens total and **all of it lands.**
   Chunking is cheaper in tokens, not just safer.

2. **Smaller generations are faster per round.** SHAMSU's own measurement
   ([simple_chat.py:3468](shamsu/agents/simple_chat.py#L3468)): whole-file
   rewrites drove one turn to **18 minutes over 18 rounds at ~100s per write.**
   Generation time scales with tokens emitted. Six small writes are not six
   times one big write — they are meaningfully faster in wall-clock.

So the "cost centre" framing in §5 is only half true. Correcting it: **chunked
writing costs more *round-trips*, but fewer wasted tokens and less wall-clock
time than the truncate-and-recover cycle it replaces.**

## 6.1 The arithmetic

SmallCode's ratio, verified against their source:

| | Value | Source |
|---|---|---|
| Output budget per reply | 8,192 tokens | `bin/smallcode.js:2358` |
| Max `write_file` content | 8,000 chars ≈ 2,000 tokens | `bin/executor.js:170` |
| **Headroom** | **4×** | — |

The model is never permitted to attempt a write large enough to exhaust its own
output budget. That is the whole mechanism.

Working the same ratio backwards for SHAMSU:

```
content of C chars
  ~= C / 4 tokens                (SHAMSU's CHARS_PER_TOKEN_ESTIMATE)
  x  ~1.10 for JSON escaping     (newlines -> \n, quotes and backslashes doubled)
  ~= C / 3.6 tokens on the wire

want:  content_tokens <= reply_cap / 4
       C / 3.6        <= reply_cap / 4
       C              <= 0.9 x reply_cap
```

Sanity check against SmallCode: `0.9 x 8192 = 7,373`, and they chose 8,000.
Same order, same intent. The arithmetic converges — good sign.

**Note the estimate runs the wrong way for code.** `chars/4` is tuned for prose;
dense code is closer to 3.3–3.7 chars per token, so `C/4` *underestimates* the
real token count. Use 0.85 rather than 0.9 to absorb that.

## 6.2 There are two independent walls, not one

This matters — fixing only the first leaves the bug alive.

**Wall A — the reply budget.** Soft, dynamic, and SHAMSU already knows about it
(`_reply_cap`). Hitting it produces a clean `done_reason: "length"` that the
existing guard catches.

**Wall B — llama.cpp's tool-argument JSON parser, ~13 KB.** Hard, fixed, and
**SHAMSU does not know it exists.** SmallCode caps at 8,000 chars specifically
to stay 1.6x under it (`bin/executor.js:168`). Hitting this wall does *not*
produce `done_reason: "length"` — it produces a mangled or empty tool call, which
is why the truncation guard sometimes never fires (§1, hole 1.3).

A dynamic cap derived only from the reply budget would still allow a 60 KB write
on a large window and walk straight into Wall B. **The cap must be the minimum of
both.**

## 6.3 The rule

```
MAX_WRITE_CHARS = clamp(
    lower = 2_000,                     # ~50 lines; below this, fail loudly instead
    value = 0.85 * reply_cap_tokens,   # Wall A, scales with the window
    upper = 8_000,                     # Wall B, llama.cpp — absolute, never scaled up
)
```

Behaviour across real configurations:

| Window | `reply_cap` | 0.85 x cap | Binding wall | `MAX_WRITE_CHARS` |
|---|---|---|---|---|
| 32k, empty session | ~16,384 | 13,926 | **B** (llama.cpp) | **8,000** |
| 32k, half full | ~8,192 | 6,963 | **A** (budget) | **6,963** |
| 16k after OOM shrink | ~4,096 | 3,482 | A | 3,482 |
| 8k unknown-model fallback | ~3,000 | 2,550 | A | 2,550 |
| 8k, prompt already large | ~2,048 | 1,741 | **floor** | **2,000** -> refuse the turn |

The last row is the important one. When the floor binds, the honest answer is
**not** to let the model write 1,700 chars at a time — it is to say the window
is the wrong shape for this task and stop. Silently degrading to useless chunk
sizes is how a turn burns 24 rounds achieving nothing.

### Applies to every tool that carries content

Not just `write_file`. All of these can carry a large payload and all of them hit
both walls:

- `write_file.content`
- `append_file.content`
- `patch_file.new_string` — a patch replacing 10 lines with 800 has the identical problem
- `read_and_patch.new_string`
- `create_and_run.content`

`WRITING_TOOLS` ([simple_chat.py:868](shamsu/agents/simple_chat.py#L868)) is
already exactly this set. Reuse it.

### Keep `MAX_REPLY_TOKENS` where it is

Do **not** shrink the reply budget to fix this. Once the content cap exists, the
reply budget stops being the binding constraint for writes, and a large budget
is still genuinely useful for prose — a long explanation, a review, a plan.
**Bound the unit of work, not the budget.**

One residual case the per-call cap does not cover: a single generation emitting
*several* write calls can still exhaust the budget in aggregate. SmallCode has
the same hole. The existing truncated-write refusal (which refuses the last call
of a cut-off generation) remains the backstop.

## 6.4 Enforce in three places, not one

SmallCode states the rule in the prompt, in the schema, and in the tool. A small
model that ignores one hits the next. SHAMSU currently states it in **none** of
them with a number.

**1 — System prompt** ([simple_prompt.py](shamsu/agents/simple_prompt.py),
`BIG_FILE_CAPABILITY`). Replace *"a file too big to write in one go"* with a
number the model can act on:

> Keep every `write_file` and `append_file` under **60 lines**. For anything
> larger: `write_file` the first 60 lines, then `append_file` each following
> section, 60 lines at a time.

Use the **static, conservative 60 lines** in prose, not the computed byte cap.
Prose guidance has to be memorable; the tool is what has to be exact. 60 lines of
dense code is ~2,500 chars — comfortably under every row in the table above, so
a model that follows the prompt never reaches the hard refusal. That gap is
deliberate belt-and-braces, and it is exactly what SmallCode does (their prompt
says 60 lines; their enforced cap is 8,000 chars ~= 200 lines).

**2 — Tool schema descriptions.** The model reads these more reliably than it
reads the system prompt. Add to `write_file` and `append_file`:
`"LIMIT: 60 lines / 8KB per call."`

**3 — The tool itself.** Hard refusal, and **the error must name the strategy**,
not just the limit. SmallCode's wording is the model to copy:

```
write_file: content too large (312 lines / 14KB).
Tool calls larger than ~8KB cannot be parsed reliably.
Strategy: write a skeleton file first (imports + empty function stubs),
then use multiple patch calls to fill in each section.
Keep each write_file under 60 lines.
```

This converts an **unrecoverable** failure into a **recoverable** one. The
content was fully generated and is merely rejected at the door — nothing is lost,
and the model learns the strategy at the moment it needs it. Contrast today's
behaviour, where generation stops mid-file and the remainder never existed.

## 6.5 What this actually costs

Correcting the "~10 tool calls" figure from §5 — that was based on SmallCode's
*60-line prose guidance*, not their 8,000-char *enforced cap*:

| File size | At the 8,000-char hard cap | At the 60-line prose guidance |
|---|---|---|
| 200 lines (~6 KB) | 1 call | 3–4 calls |
| 500 lines (~15 KB) | **2 calls** | 6 calls |
| 1,500 lines (~45 KB) | 6 calls | 18 calls |

**Recommendation:** aim the prompt at 60 lines and let the tool enforce 8,000
chars. Most files land in 2–6 calls, every one of which succeeds. Given the
decision in §6.0, this is the right side of the trade — and per §6.0's second
point, likely faster in wall-clock than the single large write it replaces.

## 6.6 The tension chunking creates: verifying a half-built file

**This must be solved in the same change, or the fix creates a new bug.**

`append_file` is deliberately inside `WRITING_TOOLS`, so
`_append_verification` runs after every chunk
([simple_chat.py:2745](shamsu/agents/simple_chat.py#L2745)). That was added to
fix stale verdicts — but it means that once chunked writing is the *default*,
**every intermediate chunk will legitimately fail `bracket_problem`.** The
model will be told "1 unclosed {" on a file that is simply not finished yet,
and will start repairing something that is not broken. The code comment at that
line already flags this exact failure shape.

Three options, best first:

**(a) Report open blocks as *progress*, not as a *problem*.** When a file has
been appended to during this turn and has not been declared finished, the
verifier says:

> `game.js` — 3 blocks still open. Continue with `append_file`.

instead of reporting a fault. This turns the verifier into precisely the signal
the model needs to write the next chunk correctly, and it costs almost nothing:
`bracket_problem` already returns the position and the open stack.

**(b) Verify only the last write to a given path in a turn.** Simpler, but the
model gets no feedback until the end.

**(c) Defer verification until the model stops writing to that file.** Same as
(b) in effect, more bookkeeping.

Take **(a)**. It is the only one that makes chunking *better* than a single
write rather than merely tolerable.

## 6.7 The pre-write gate must detect truncation, not invalidity

Related trap, same change. §4 item 1 proposes running
`simple_verify.check_file` / `bracket_problem` on the content *before* writing.
**Under a chunking strategy that would refuse every legitimate first section**,
because a first section correctly has unclosed blocks.

The gate must therefore test for **truncation signatures**, not for validity:

| Signal | Verdict |
|---|---|
| Ends mid-string literal (`return render(request, "item`) | **Always truncation** -> refuse |
| Ends mid-identifier / mid-token, no trailing newline | **Truncation** -> refuse |
| Ends cleanly on a complete line, blocks still open | **Plausibly a section** -> allow |
| Balanced and parses | Allow |

The list already exists — `_TRUNCATION_ERROR_MARKERS`
([chat_loop.py:4455](shamsu/agents/chat_loop.py#L4455)): *unterminated string
literal*, *was never closed*, *unexpected EOF*, *incomplete input*. It is
Python-only today. Generalise it and pair it with `bracket_problem`'s existing
string/comment-aware scanner, which already covers JS, TS, JSX, CSS and JSON.

## 6.8 Implementation checklist

Ordered so each step is independently shippable and testable.

- [x] **1.** Add `MAX_WRITE_CHARS` per §6.3 — clamp(2,000 · 0.85×reply_cap · 8,000). One function, derived from the existing `_reply_cap`.
- [x] **2.** Enforce it in the tool layer for every member of `WRITING_TOOLS`, with SmallCode's strategy-naming error message (§6.4, point 3).
- [x] **3.** Put **60 lines** in the system prompt (`BIG_FILE_CAPABILITY`) and in the `write_file` / `append_file` schema descriptions.
- [x] **4.** Change `_append_verification` to report open blocks as *progress* on a file still under construction this turn (§6.6, option a).
- [x] **5.** Add the pre-write **truncation-signature** gate — for new files too, and for every language `bracket_problem` handles, not just Python (§6.7). This closes hole 1.1.
- [x] **6.** Add the continue-from-the-tail recovery from `chat_loop.py:4465` to simple mode, language-agnostic (§4 item 3). With steps 1–3 in place this should fire rarely — it is the safety net, not the primary path.
- [x] **7.** Fix the unknown-model context fallback (§1, hole 1.5) — pattern matching so an unlisted model does not silently drop to 8,192 and shrink every cap in the table.

**Verification that this worked:** write a 1,500-line file from a single prompt
on `qwen2.5:3b-instruct` (per the project's small-model testing rule). Expect
6–18 successful calls, zero truncation refusals, and a file that parses. Today
that same prompt produces a truncated file or a refusal loop.
