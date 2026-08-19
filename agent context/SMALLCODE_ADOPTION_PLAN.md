# Adopting SmallCode's practices — plan

**Status: IMPLEMENTED. A-H all landed on branch `small-shamsu`, 2026-08-19.**
Written 2026-08-19.

This file is kept as the *reasoning*: what was worth taking from smallcode, and the number on
our own harness that justified each item. It is not the record of what was built.
For that - including the five places implementing it proved the plan wrong, and
the second round read from smallcode's source rather than from this summary of
it - see `SMALLCODE_IMPLEMENTATION_PLAN.md`.

One decision here was later REVERSED on evidence: two-stage tool routing is
listed below under "Explicitly NOT adopting" on the grounds that we send 6 tools.
The roster then grew to 19 (~2,100 tokens of schema per call) and the premise
no longer held, so it was adopted in `1d0444a` - gated on the context window,
the way smallcode gates it. See `shamsu/agents/simple_router.py`.

## What was cloned, and the rules around it

```
reference/smallcode/          # git clone --depth 1, 331 files, 25 MB
```

- **`reference/` is in `.gitignore`** (rule added *before* cloning and verified
  with `git check-ignore`; `git status` does not see it). It never reaches our
  GitHub.
- **Licence: MIT**, © 2026 Doorman11991. Reuse is permitted **with attribution**.
  So: we may lift ideas freely, and lift code with a credit line. Nothing gets
  vendored into `shamsu/` without that credit in the file header.
- Read it in place. Do not copy files into the package "temporarily".

## Why this repo is worth mining

Its stated premise is our exact situation:

> *"your model has maybe 8–32k context, it sometimes writes tool calls that
> aren't valid JSON, and it will forget what it was doing by step three of a
> five-step task. Every architectural decision flows from that constraint."*

Everything below is tied to a **number measured on our own harness today**, not
to admiration for their design.

---

## A. Ground-truth token accounting  — fixes a 9,500-token blind spot

**Them:** `src/session/tokens.js` reads `prompt_tokens` / `completion_tokens`
straight off the API response. Their own `estimateTokens` is `chars/4` and is
never trusted as truth.

**Us, measured today:**

```
SHAMSU thought the prompt was  21,381
the prompt really was         ~31,400     (tool_calls counted as ZERO)
result                        19 truncated generations in one session
```

**Do:** count `content + tool_calls + per-message overhead` in *both*
`_num_ctx()` and `select_for_budget()`, then **calibrate against
`prompt_eval_count`** returned by every Ollama response. Keep a per-model
correction factor so the estimate self-corrects instead of drifting.

**Verify:** estimate lands within a few percent of `prompt_eval_count` on a
replayed session; headroom returns to ~8,192.

**Effort:** small. **Priority: 1** — nothing else can be trusted until this is right.

---

## B. Thinking budget, and keep thinking OUT of history  — fixes the cut-off answer

**Them:** `src/model/thinking_budget.js`, whose header is a description of our bug:

> *"Without a budget, a small reasoning model can spend 8000 tokens 'thinking'
> about a trivial rename, blowing through context and adding minutes of latency."*

Three mechanisms:
- `applyThinkingBudget(body, budget)` — advise the provider of a soft cap
  (`SMALLCODE_THINKING_BUDGET=2000`), set defensively across provider field names.
- `SMALLCODE_THINKING_DISABLE=true` — thinking off entirely for repair passes.
- `truncateThinking(content, maxChars)` — emergency cap that replaces the
  **middle** of an oversized thinking block before it enters history.
- `extractThinking(content) -> { thinking, answer }` — **the answer goes into
  the next turn; the thinking is logged separately.**

**Us, measured today:** the model produced **95 tokens of thinking, zero content**,
cut mid-word. SHAMSU then used that fragment *as the answer* and appended it to
the conversation — so the truncated thought becomes context for every later turn.

**Do:**
1. Set an explicit thinking budget and a real `num_predict` (we send neither).
2. **Stop putting thinking into the conversation.** Log it, show it, do not
   replay it. Today's `model only reasoned; using its thinking as the answer`
   salvage should be a last resort that is clearly labelled, not a silent path.
3. Consider `think=False` for mechanical passes (verification, repair retries).

**Verify:** a reasoning model on a trivial request stops thinking near the
budget; a truncated turn is reported as truncated, never presented as an answer.

**Effort:** small–medium. **Priority: 2.**

---

## C. Patch-first editing  — kills the payload problem at its source

**Them (ARCHITECTURE.md):**

> *"The primary edit primitive is `patch`... Small models are unreliable at
> reproducing whole files: they truncate, hallucinate imports, drift in
> indentation. A surgical patch that touches 10 lines is orders of magnitude
> more reliable than rewriting 300 lines, and it's cheaper on context."*

**Us, measured today:** the two largest invisible messages in the session were
`write_file` payloads at **2,618** and **2,231** tokens. Every one of those is a
whole file the model retyped.

**Do:** bias hard toward `patch_file`. `write_file` stays for genuinely new
files; for an existing file above a small threshold the tool layer should
refuse and name `patch_file` (the error string is where a small model actually
takes correction — the standing prompt is not). This is *also* a correctness
win, not only a context win.

**Verify:** on a realistic edit session, `write_file` calls against existing
files drop to ~zero and the same edits still land.

**Effort:** small. **Priority: 3** — cheapest large win after A.

---

## D. Cap tool results, and evict them mid-turn  — 4.4× longer sessions

**Them:** tool results capped at 4k chars; old results dropped **within** a turn
as the window fills; bash output capped at 30,000 chars and truncated in the
**middle**, keeping both ends; reads default to 2,000 lines.

**Us, measured today** (real 130-message session, keeping the last 20 messages
verbatim and eliding older payloads):

```
today                44,833 tokens   ~13 turns before the window fills
payloads elided      10,476 tokens   ~57 turns          (4.4x, and LOSSLESS)
```

Lossless because every elided byte is still on disk and `read_file` fetches it
back.

**Do:** after a call returns, replace its payload with `path + diff summary`
(`patched frontend/game.js +3 -1` plus the changed lines). Keep the most recent
payload verbatim. Evict mid-turn, not only between turns.

**Verify:** prompt size on a write-heavy session drops ~75%; the model can still
name and re-read every file it touched.

**Effort:** medium. **Priority: 4** — biggest single reclaim.

---

## E. A context meter, and counters on compaction  — makes silent bugs visible

**Them:** `bin/token_monitor.js` — `contextMeter(window) -> { pct, used, window }`
driven by the last real prompt size, plus `compactions` and `evictions` counts.
The file's own header: *"Helps verify context compaction is working correctly."*

**Us:** we shipped a bug that re-compacted the same 23 messages **every turn**
for a whole session and nobody noticed — the user found it by reading the
scrollback. A counter would have shown it immediately.

**Do:** show `ctx 68% (22.3k/32.8k)` in the REPL status line, and track
compaction/eviction counts per session, surfaced by `/status`.

**Verify:** the meter tracks `prompt_eval_count`; a deliberately looping
compaction shows an obviously wrong counter.

**Effort:** small. **Priority: 5** — cheap, and it converts a whole class of
silent context bug into something you just look at.

---

## F. Budget by category, not one number

**Them:** `marrow/src/context/budget.ms`

```
TokenAllocation { system_prompt, working_memory, conversation, tool_results, available }
totalBudget() = model_context_length * max_budget_pct / 100
```

**Us:** one total, so eviction blindly drops the oldest messages regardless of
what is actually consuming the window — which today is tool payloads, not
conversation.

**Do:** track the four buckets and evict from the fattest one.

**Effort:** medium. **Priority: 6** — do it when D lands; they compose.

---

## G. Expand `@file` references before the model sees them

**Them:** pre-work before any tokens are sent — expands `@file` into real
content, injects a git diff when the message implies recent changes, and screens
vague requests with a **zero-token regex classifier** that asks for clarification
instead of guessing.

**Us:** the user typed `@ASTEROID_SHOOTER_SHAMSU_BUILD_SPEC.md` and simple mode
passed the literal string through. A `MentionResolver` already exists in
`repl.py` — it is simply not wired into the simple path.

**Do:** wire the existing resolver in. It costs one round-trip today (the model
has to call `read_file` itself) and sometimes costs a wrong guess.

**Effort:** small. **Priority: 7.**

---

## H. Two-tier memory: a working scratchpad beside the digest

**Them:** short-term working memory in the conversation (evicted under pressure)
plus long-term project memory in SQLite with FTS, keyed by type (decision,
workflow, gotcha, convention, context), loaded by keyword overlap. Separately,
`marrow/src/context/working_memory.ms` keeps a token-capped `.smallcode/memory.md`
scratchpad that survives turns — *"compensates for small models' limited internal
reasoning."*

**Us:** we already have SQLite memory. What we lack is the **scratchpad the model
writes itself**. It is not the same as compaction: a digest is our lossy summary
of what happened, a scratchpad is the model's own deliberate note of what matters.

**Do:** consider `.shamsu/memory.md`, token-capped, loaded into context, written
via a tool. Pairs with — not instead of — the per-turn digest.

**Effort:** medium. **Priority: 8** — evaluate after A–E; it is the least proven
for our workload.

---

## Explicitly NOT adopting

- **Cloud escalation** (`bin/escalation.js`, cloud pricing tables). Routes hard
  failures to Claude/GPT/DeepSeek. Directly against SHAMSU's prime directive:
  inference is local.
- ~~**Two-stage tool routing.**~~ **REVERSED — adopted in `1d0444a`.** The
  original reasoning: *"Real savings at 18–20 tools (~800 tokens when the
  category is 'respond'). We send **6** small schemas, so the payoff is minor and
  the cost is exactly the routing indirection simple mode was built to delete.
  Revisit only if the tool count grows."* The tool count grew, the same day: 19
  tools, 2,111 tokens of schema on every call, 6.4% of a 32k window. The part
  worth copying exactly turned out to be **when not to use it** — smallcode routes
  on the context window (two-stage at or below 16k, everything above it), which
  is better than the manual switch reached for first. See
  `shamsu/agents/simple_router.py`.
- **MarrowScript cognition layer.** Their own DSL and compiler. Interesting, not
  a fit.

---

## Order of work

```
1. A  ground-truth token accounting      (nothing is trustworthy until this)
2. B  thinking budget + thinking out of history
3. C  patch-first editing
4. D  cap + elide tool payloads, evict mid-turn
5. E  context meter + compaction counters
6. F  per-category buckets
7. G  @file expansion
8. H  working-memory scratchpad          (evaluate)
```

A–E are the ones tied to bugs measured on our own harness this week. F–H are
improvements rather than fixes.

**All eight landed in this order**, one commit each, every guard verified by
removing it and watching the right test fail. What shipped, what it cost, and
where the plan turned out to be wrong is recorded in
`SMALLCODE_IMPLEMENTATION_PLAN.md`.
