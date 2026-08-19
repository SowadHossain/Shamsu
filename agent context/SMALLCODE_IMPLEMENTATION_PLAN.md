# Implementing SmallCode's approach in SHAMSU — code-level plan

**Status: IMPLEMENTED on branch `small-shamsu`, 2026-08-19. A-H all landed.**
Written 2026-08-19. Source read locally at `reference/smallcode` (MIT, ©2026
Doorman11991 — reuse permitted **with attribution**; anything lifted verbatim
carries a credit line in the file header).

Companion docs: `CONTEXT_AND_TRUNCATION_PLAN.md` (the measurements),
`SMALLCODE_ADOPTION_PLAN.md` (what to take and why).

---

## A. Ground-truth token accounting

### What they do

`src/session/tokens.js`:

```js
function extractUsage(response) {
  const usage = response?.usage || {};
  return {
    inputTokens:  usage.prompt_tokens || 0,
    outputTokens: usage.completion_tokens || 0,
    totalTokens:  usage.total_tokens || ...,
  };
}
function estimateTokens(text) { return Math.ceil(text.length / 4); }  // estimate ONLY
```

The estimate is deliberately crude. **Truth comes from the response.**
`bin/token_monitor.js` then records every call and exposes the last real prompt
size.

### What we do

**Files:** `shamsu/context/budget.py`, `shamsu/agents/simple_chat.py`,
`shamsu/agents/chat_state.py`

1. **One counter that counts everything.** New in `budget.py`:

   ```python
   PER_MESSAGE_OVERHEAD = 8          # measured: ~10 at 1 msg, ~4.5 at 6

   def message_tokens(message) -> int:
       total = count_tokens(str(message.get("content") or ""))
       if message.get("tool_calls"):
           total += count_tokens(json.dumps(message["tool_calls"]))
       return total + PER_MESSAGE_OVERHEAD
   ```

   Use it in **both** `_num_ctx()` (simple_chat.py) and `select_for_budget()`
   (chat_state.py). Today both read only `content` — which is the whole bug.

2. **Calibrate against the response.** Ollama returns `prompt_eval_count` on
   every `/api/chat` reply — the real number. Store a per-model correction:

   ```python
   factor = observed_prompt_eval_count / our_estimate_for_that_prompt
   # smooth it, clamp to something sane (e.g. 0.8 .. 1.6), persist per model
   ```

   Apply the factor when budgeting. The estimate then self-corrects instead of
   drifting — which is exactly how we got 21,381 vs ~31,400.

3. **Record it** on the loop: `last_prompt_tokens`, `last_completion_tokens`.
   Feeds E.

### Verify

- Replay the 130-message session: estimate within a few percent of
  `prompt_eval_count`.
- Headroom returns to ~8,192 instead of ~1,300.
- A deliberately broken estimator (halve it) is corrected within ~2 turns.

**Effort: small. Do first — nothing below can be trusted until this is right.**

---

## B. Thinking budget, and thinking OUT of history

### What they do

`src/model/thinking_budget.js`. Header states our exact bug:

> *"Without a budget, a small reasoning model can spend 8000 tokens 'thinking'
> about a trivial rename, blowing through context and adding minutes of latency."*

```js
const DEFAULT_BUDGET_TOKENS = env.SMALLCODE_THINKING_BUDGET || 2000;
const HARD_CAP_CHARS        = env.SMALLCODE_THINKING_HARD_CAP || 32000;

applyThinkingBudget(body, {tokens, disable, baseUrl})   // advise the provider
extractThinking(content) -> { thinking, answer }        // split them
truncateThinking(content, maxChars)                     // emergency middle-out cap
```

`truncateThinking` keeps the **first 60% and last 30%** of an oversized thinking
block and replaces the middle with
`[...thinking truncated: N chars omitted...]`, re-wrapping in the original tag.

And `bin/model_client.js` sets a hard `max_tokens: 4096` on every request.

### What we do

**Files:** `shamsu/agents/simple_chat.py` (`_call_model`, the salvage path)

1. **Send `num_predict`.** We currently send only `temperature` and `num_ctx`,
   so generation is bounded solely by whatever is left of the window — which is
   how it ended up with ~1,300 tokens. Set:

   ```python
   "options": {
       "temperature": ...,
       "num_ctx": ceiling,
       "num_predict": output_reserve(ceiling),   # never larger than the reserve
   }
   ```

2. **Stop replaying thinking.** Today `parse_model_turn` returns `.thinking`,
   and the "model only reasoned" path does
   `self.state.append_assistant(turn.thinking)` — so a **truncated thought
   becomes permanent conversation**. Instead:
   - keep thinking in the turn log and on screen,
   - never `append_assistant()` it,
   - if there is no content, that is an *incomplete turn* (see §B3), not an answer.

3. **Detect truncation honestly.** Ollama returns `done_reason`. When it is
   `"length"`:
   - label the output partial,
   - say why: *"I ran out of room to answer — the conversation is long; try
     `/new`, or ask for a smaller piece."*,
   - optionally retry **once** after D's elision, which usually frees enough.

4. **`think=False` for mechanical passes** (verification, repair retries) —
   their `SMALLCODE_THINKING_DISABLE` equivalent.

### Verify

- A trivial request stops thinking near the budget rather than at the window.
- A forced-tiny `num_ctx` produces an explanation, never a fragment presented as
  an answer.
- Thinking never appears in a later prompt: assert no hydrated message content
  equals a previous turn's thinking.

**Effort: small–medium.**

---

## C. Patch-first editing

### What they do

ARCHITECTURE.md:

> *"The primary edit primitive is `patch` — search-and-replace where the
> `old_str` has to match exactly one location. Small models are unreliable at
> reproducing whole files: they truncate, hallucinate imports, drift in
> indentation."*

`bin/executor.js:247` — `content.split(args.old_str).length - 1` must be exactly
1, else it refuses (with a semantic-merge recovery attempt).

`src/tools/read_tracker.js` enforces read-before-write with a message that tells
the model what to do next:

> *"Refused: write_file would overwrite existing 'X' you haven't read. Call
> read_file first to see its current content, OR if you intend to fully replace
> it, retry — second attempt is allowed."*

Note the escape hatch: **a deliberate second attempt is allowed**, so the guard
cannot deadlock (this is the same class of bug as our partial-read guard that
blocked writes forever).

### What we do

**Files:** `shamsu/agents/simple_chat.py` (`_execute`, `SIMPLE_TOOL_SCHEMAS`)

1. **Refuse `write_file` on an existing file above a small threshold**, naming
   `patch_file` in the error. We already have `_refuse_blind_overwrite`; extend
   it from "haven't read it" to "this file exists and is big — patch it".
2. **Keep an explicit escape**: a second consecutive `write_file` for the same
   path is honoured (full rewrite is sometimes right). Mirrors their
   "second attempt is allowed" and prevents a guard with no exit.
3. **Correct in the error string, not the system prompt.** A small model acts on
   the message it just received; a standing prohibition dilutes the prompt.

### Verify

- On a realistic edit session, `write_file` against existing files drops to ~0
  and the same edits still land.
- A deliberate full rewrite still succeeds on the second attempt.
- Our two worst payloads (2,618 / 2,231 tokens) do not recur.

**Effort: small. Cheapest large win after A.**

---

## D. Cap and elide tool payloads, evict mid-turn

### What they do

`bin/smallcode.js:976` — checked **every 3 tool calls**, when
`estimateHistoryTokens > detected_window * 0.6`:

**Pass 1 — shrink tool_call ARGUMENTS in OLD assistant messages:**

```js
// "After the tool result has been received, the model doesn't need the
//  full write_file content in arguments anymore."
const parsed = JSON.parse(tc.function.arguments);
const minimal = {};
for (const [k, v] of Object.entries(parsed)) {
  minimal[k] = (typeof v === 'string' && v.length > 100) ? v.slice(0, 80) + '…' : v;
}
tc.function.arguments = JSON.stringify(minimal);
```

Keys are **kept**, long values shortened — so the model still sees
*"write_file(filepath=frontend/game.js)"*, not a hole. The most recent
tool-calling message is left untouched. Invalid JSON falls back to `{}`.

**Pass 2 — evict tool RESULTS** down to `maxBudget * 0.7`:

```js
conversationHistory[i].content = `[evicted: ${len} tokens]`;
```

with a **critical safety rule**: a tool result is only evicted if its owning
assistant message is in the first half of history, and an *orphaned* tool result
(owner already gone) is spliced out entirely — never leave a `tool_call_id`
pointing at nothing.

Then `tokenMonitor.recordEviction()`.

### What we do

**Files:** `shamsu/agents/simple_chat.py` (`_run_tools`), `chat_state.py`

1. **Elide payloads after the call returns.** Replace the stored `tool_calls`
   arguments and the tool result with:

   ```
   patched frontend/game.js  +3 -1
       - if (asteroidArray.length === 0) {
       + if ((window.windowAsteroids || []).length === 0) {
   ```

   We already compute that diff (`_with_diff`); we simply never throw the
   payload away. Keep the **most recent** payload verbatim.

2. **Evict mid-turn**, on the same cadence idea — check every N tool calls
   against a fraction of the budget, not only between turns.

3. **Carry their safety rule over exactly**: never orphan a tool result from its
   `tool_call_id`. Elide *content*, keep the pairing. (Same invariant already
   handled in the hydration filter.)

### Measured payoff (our own session, 130 messages)

```
today                44,833 tokens   ~13 turns before the window fills
payloads elided      10,476 tokens   ~57 turns      (4.4x, LOSSLESS)
```

Lossless because every elided byte is still on disk and `read_file` gets it back.

### Verify

- Write-heavy session: prompt drops ~75%.
- The model can still name and re-read every file it touched.
- No orphaned tool results: assert every `tool_call_id` in history has an owner.

**Effort: medium. Biggest single reclaim.**

---

## E. Context meter and compaction counters

### What they do

`bin/token_monitor.js` — header: *"Helps verify context compaction is working
correctly."*

```js
contextMeter(window) -> { pct, used, window }   // used = last REAL prompt size
recordCompaction(); recordEviction();
```

Surfaced in `bin/commands.js`:
`Compacts: N | Evictions: M`

### What we do

**Files:** `shamsu/agents/simple_chat.py`, `shamsu/cli/repl.py`

1. **A meter in the status line**, driven by A's real number:
   `ctx 68% (22.3k/32.8k)`.
2. **Counters per session** — compactions, evictions, truncated generations —
   shown by `/status`.
3. **Warn on the way up**, not at the wall: at ~80% say so once.

We shipped a bug that re-compacted the same 23 messages **every turn** for a
whole session, and it was found by reading scrollback. A counter shows it
immediately.

### Verify

- Meter tracks `prompt_eval_count` within a few percent.
- A deliberately looping compaction produces an obviously wrong counter.

**Effort: small. Cheap, and it makes a whole bug class visible.**

---

## F. Per-category budget buckets

### What they do

`marrow/src/context/budget.ms`:

```
TokenAllocation { system_prompt, working_memory, conversation, tool_results, available }
totalBudget() = model_context_length * max_budget_pct / 100
available()   = totalBudget - sum(usage)
```

### What we do

Track the same four buckets and **evict from the fattest**, instead of blindly
dropping oldest. Today's fattest is tool payloads, not conversation — so
dropping oldest evicts the wrong thing.

**Verify:** on the measured session the tool_results bucket is the majority, and
eviction targets it first.

**Effort: medium. Do after D — they compose.**

---

## G. Expand `@file` before the model sees it

### What they do

Pre-work before any tokens are sent: expand `@file` into real content, inject a
git diff when the message implies recent changes, and screen vague requests with
a **zero-token regex classifier** that asks for clarification rather than
guessing.

### What we do

**File:** `shamsu/cli/repl.py` (`_run_simple_chat`)

`MentionResolver` already exists and is used by the legacy path only. Wire it
into simple mode. The user typed `@ASTEROID_SHOOTER_SHAMSU_BUILD_SPEC.md` and
the literal string was passed through; the model had to spend a round calling
`read_file` — and sometimes guesses instead.

Cap expanded content (reuse D's elision) so a huge `@file` cannot blow the window.

**Verify:** `@file` arrives as content; the first round no longer spends a
`read_file` on it.

**Effort: small.**

---

## H. Working-memory scratchpad  *(evaluate, do not assume)*

### What they do

`marrow/src/context/working_memory.ms` — `.smallcode/memory.md`, token-capped,
loaded into context, survives turns, *"compensates for small models' limited
internal reasoning."* Plus long-term project memory in SQLite with FTS keyed by
type (decision, workflow, gotcha, convention, context).

### What we do

We already have SQLite memory. What is missing is the scratchpad **the model
writes itself** — distinct from a digest, which is *our* lossy summary of what
happened. Consider `.shamsu/memory.md` with a `remember` tool.

**Do not build this until A–E are in and measured** — it is the least proven for
our workload, and it adds a permanent block to every prompt.

**Effort: medium.**

---

## Order, and what each one buys

```
1. A  ground-truth accounting     fixes 9,500-token blind spot, 19 truncations
2. B  thinking budget + no replay fixes the cut-off answer becoming context
3. C  patch-first                 kills 2,618/2,231-token payloads at source
4. D  cap + elide + mid-turn      44,833 -> 10,476 tokens; 13 -> 57 turns
5. E  meter + counters            makes silent context bugs visible
6. F  per-category buckets        evict the right thing
7. G  @file expansion             saves a round, avoids a guess
8. H  working memory              evaluate after A-E
```

A–E fix bugs measured on our own harness this week. F–H are improvements.

## Rules for the work itself

- **Attribution.** Anything lifted verbatim from `reference/smallcode` carries a
  credit line naming the file and the MIT licence. Ideas need no credit; code does.
- **One item per change, with its verification**, in the order above.
- **Every guard gets an exit.** Their write_file refusal allows a deliberate
  second attempt. Our partial-read guard once blocked writes forever because it
  had none. A guard without an exit is a deadlock waiting for a user.
- **Correct in the tool's error string**, not the standing prompt. A small model
  acts on the message it just received.
- **Prove each guard by removing it** and watching the test fail. Three
  would-be-vacuous tests were caught that way this week.

**Superseded by the implementation notes at the end of this document.**

---

## What actually shipped — 2026-08-19, branch `small-shamsu`

All eight items, one commit each, every guard verified by removing it and
watching the right test fail. Suite green throughout.

| Item | Commit | Note |
|---|---|---|
| A | `24cda8a` | + three uncounted overheads the plan missed (below) |
| B | `24cda8a` | gated on `done_reason`, not blanket (below) |
| C | `4575ac7` | 400-token threshold, escape on second attempt |
| D | `37ca22a` | at **hydration**, not in-memory (below); **2x history retained** |
| E | `30a161b` | + the re-compaction bug **fixed**, not just counted |
| F | `ee4af6f` | five buckets; eviction picks by fattest |
| G | `1318018` | `MentionResolver` wired; sent, not persisted |
| H | `affa3a7` | `remember` tool, capped **and charged** |

### Five corrections found while implementing

1. **D as written would have saved nothing across turns.** A fresh
   `SimpleChatLoop`, and so a fresh `ChatState`, is built per user message, and
   hydration reloads the transcript from disk with every payload intact. The
   44,833 → 10,476 measurement was taken on exactly that cross-turn case, so
   elision had to run at hydration, not on what the current turn produced.

2. **A's own acceptance criterion would have failed.** Charging `tool_calls`
   still leaves the tool schemas (~630 tokens, every call), the grounding block
   (`_messages` appends it *after* the budget is spent), and the rolling summary
   (up to 2,048) uncounted — about 3,900 tokens. Headroom would have returned as
   ~5,300 against the ~8,192 the plan predicted. There is now a test that builds
   a real prompt and measures.

3. **The existing calibration was fed its own output.** `calibrate_from_response`
   received the already-corrected estimate, so the EMA settled on the *square
   root* of the true ratio and left over half of any undercount in place. Fixed
   in `ContextBudgetManager`, which also means item A was mostly wiring, not
   building — the machinery already existed and simple mode was the one caller
   never feeding it.

4. **Shell output is not lossless.** D's justification — "every elided byte is
   still on disk and `read_file` gets it back" — is true for file reads and
   writes and false for a test failure or a stack trace. Those are compacted
   head-and-tail instead.

5. **B's blanket "never use thinking as the answer" would produce a false
   error.** Reasoning models genuinely end turns with a complete thought and no
   content; telling that user they ran out of room is a lie. `done_reason`
   separates the two cases and both have tests.

### Also fixed: the bug item E only made visible

`_restore_summary` reset the compaction watermark to 1 unconditionally, so every
turn re-summarised the whole history — one wasted model call per turn, forever
(§3 of `CONTEXT_AND_TRUNCATION_PLAN.md`, dropped from this document). The
watermark is now transcript-absolute on both the save and the load side.

### The live run — 2026-08-19, qwen2.5:3b-instruct, 8/9

This section used to read *"No live run yet. Everything above is unit-tested
against a scripted client."* It has now been run: five turns, fresh workspace,
a fresh `SimpleChatLoop` per turn rehydrating from disk, `--approval allow`.

| | |
|---|---|
| A calibration recorded | **PASS** |
| A estimate within 15% of `prompt_eval_count` | **FAIL** — see below |
| B `num_keep` covers the system prompt | **PASS** (308) |
| B no reply cut off and passed off as finished | **PASS** (`done_reason` `stop` on all five) |
| C `server.py` created | **PASS** |
| C `server.py` later edited | **PASS** |
| **RECALL: the port survived four turns** | **PASS** |
| no turn stopped on a guard | **PASS** |
| all five turns completed | **PASS** |

#### It found a bug on the first call, and the bug was total

```
ResponseError: "qwen2.5:3b-instruct" does not support thinking (status code: 400)
```

Every turn, dead before a token was generated. Simple mode sent `think=`
unconditionally; Ollama rejects it outright for a model with no reasoning
channel. **Every non-reasoning model was unusable** — which is most of the
roster, qwen2.5-coder:7b-instruct included, the default on an 8GB machine. The
cookbook had `is_reasoning=False` recorded for all of them and
`runtime/models.py` exposes `model_is_reasoning()` for exactly this question;
simple mode never asked it. Fixed in `c5486ef`.

Nothing in 2,800 unit tests could have caught this. That is the argument for
the live run, made by the live run.

#### The recall probe passed — the one the plan said the prompt cannot test

§D's verification list says *"the model can still name every file it
touched"* cannot be tested through the prompt, because the always-fresh
workspace listing names every file whether history remembers it or not. So the
probe used a fact only the conversation carries: a port number stated in turn
1, asked back in turn 5, with two file writes and a command run in between.

> **"The dev server was said to run on port 8731."**

It reached it via `memory_load` — item H, doing the job it was added for.

#### A: the calibration works; the raw estimator is 15% heavy

Drift (`prompt_eval_count / our estimate`) was flat across all five turns, and
the correction factor walked straight to it:

```
turn      1       2       3       4       5
drift   0.848   0.879   0.857   0.852   0.848
factor  0.944   0.879   0.861   0.856   0.853
```

The acceptance line failed, and it failed in the *safe* direction: we
over-count, so the budget under-fills the window rather than overflowing it.
But 15% of a 32k window is ~4,900 tokens left on the table on this model. The
machinery is right — the factor found the truth in four turns, which is
exactly what it was built to do. What is wrong is the uncalibrated estimate,
and the likely cause is the vendored Qwen3 tokenizer counting a Qwen2.5 prompt.

#### The real finding: tool schemas ARE the prompt

Per-turn buckets, live:

```
turn   system  schemas  grounding  conversation  tool results   total
  1      308     2111        50           47            92      2,608
  2      308     2111        49          482           738      3,688
  5      308     2111        49           33            83      2,584
```

**The tool schemas are 57–81% of every prompt.** The conversation — the thing
all of §D's elision machinery protects — is 29 to 482 tokens.

`_elide_under_pressure` already reports this correctly: it checks
`fattest() != "tool results"` and says *"eliding payloads will not help much
here"*. It is right, and it has nothing to offer instead.

Two-stage routing is the mechanism that would fix it, and it is **off**, because
it gates on the context window (≤ 16k) and this session runs at 32k. That gate
is smallcode's and the reasoning holds for a full window — paying a round to
save 2k out of 32k is a bad trade. It does not hold here: the prompt is 2.6k,
so the schemas are not 6% of the window, they are 81% of the prompt.

**Recommended next change:** gate two-stage routing on the schema *share of the
prompt*, not on the window size alone. A 2,111-token schema block in front of a
477-token conversation is the trade the router exists to make, whatever the
window happens to be.

#### Also observed, not yet actioned

* **The small model thrashes.** Turn 2 — "create server.py that prints hello" —
  took 292s, 8 rounds and 4 tool calls. Turn 3 took 5 rounds and 4 tool calls to
  change one string. No guard fired, because none of them was unproductive by
  the counters' definition. Worth a look before the round budget is trusted.
* **The first call costs ~110s** (model load plus 32k KV allocation); turns 4
  and 5 took 3.7s and 1.4s. Cold start dominates a short session.

### Still not done

* A second live run on a **reasoning** model (qwen3:8b), to exercise the `think=`
  path the 3B model cannot reach.
* A long-session live run — every turn above sat far inside the window, so
  compaction, elision and eviction were never triggered (`compactions=0`,
  `elisions=0` throughout). §D and §F remain **synthetic-only**.

### Measured end to end — and a correction to how D should be stated

A 30-turn write-heavy session, scripted client, same workspace, elision on and
off:

```
config             msgs in prompt    tokens   decisions kept   oldest kept
WITHOUT elision                35    28,815            30/30        turn 1
WITH elision                   71    28,525            30/30        turn 1
```

**The prompt does not get smaller, and it was never going to.** `select_for_budget`
caps it either way — that is its job. What changes is what fits inside the cap:
**twice as much verbatim conversation** (35 messages against 71) for the same
28.5k tokens.

So the honest statement of D is *"the same window now holds twice the history"*,
not *"the prompt drops 75%"*. The 5.3x figure — 184,520 tokens verbatim against
34,904 elided on a 130-message session — is real, but it measures what sending
everything **would** cost, and the budget was already truncating instead. The
plan's own framing ("~13 turns → ~57 before the window fills") is the right
shape; the "prompt drops ~75%" verification line in §D is not, and would have
been recorded as a pass that never happened.

Both configurations kept all 30 decisions here, because the deterministic digest
carries them independently. A longer or more payload-heavy session is where the
retained-history difference starts to cost recall; that needs a live run.

Also worth noting for anyone reading §D's verification list: "the model can still
name every file it touched" cannot be tested through the prompt alone — the
always-fresh workspace listing names every file whether history remembers it or
not. It has to be probed with a fact only the conversation carries.

---

## Round two — read from smallcode's source, not from this plan's summary of it

Everything above was built from §A–H, which describe smallcode. Reading
`reference/smallcode` directly turned up four methods worth taking properly and
one weakness in theirs worth fixing. Commit `b675488`.

### 1. Hybrid search — the biggest gap this plan never mentioned

`src/tools/hybrid_search.js` + `src/rag/index_store.js`. Their framing: "grep on
steroids" — one call that fuses exact regex matching with meaning-based ranking
over symbol-aware chunks.

SHAMSU's `search_files` → `grep_files` matched with `query in line`. **A literal
substring — not even a regex.** Measured on this repo:

```
QUERY: "the function that decides how many tokens a message costs"
  BEFORE  0 match(es)
  AFTER  10, top hits: token_allocation, messages_tokens, message_tokens

QUERY: def _refuse_\w+   (regex)
  BEFORE  0 match(es)
  AFTER   2, both exact: _refuse_blind_overwrite, _refuse_unwritable_rewrite
```

The insight worth stealing is that their "embedding" is **feature hashing** —
tokens hashed into 1024 buckets, normalised. No model, no service, pure stdlib.
That matters here specifically: `retriever/semantic.py` needs a 274MB
`nomic-embed-text` in Ollama and `retriever/search.py` needs the MCP server up,
and both degrade to "no hits" when the dependency is missing — which on a fresh
machine is always. This *meets* the "no embedding model, no vector DB"
constraint rather than working around it.

**Improved on theirs: a light stemmer.** Their tokenizer has none, so "validates
tokens" and `validateAuthToken` are different words scoring zero — precisely the
gap the hybrid exists to close. Guarded so `class` does not become `clas`.

### 2. `shouldDisableThinking` — `src/model/thinking_budget.js`

Their reasoning, which matches what was measured here: on a retry the model
"already overthought the original solution. A fast, low-creativity retry is
better." Thinking is now off once a turn is repairing rather than progressing.
Note their `applyThinkingBudget` is mostly provider-detection that deliberately
sends *nothing* to Ollama; SHAMSU's native `think=` parameter is the better
lever and needs none of it.

### 3. Evict to a TARGET, not past a cutoff — `bin/smallcode.js`

They stop at `maxBudget * 0.7`. My first pass elided everything older than a
fixed 20-message tail whether or not that was necessary. Re-measured after
changing it: the 30-turn retention benefit is **unchanged** (71 messages against
35) while short sessions now elide **nothing at all**. Strictly better.

### 4. Efficiency ratio — `bin/token_monitor.js`

Completion tokens per 100 prompt tokens. The single number that says whether the
context work is paying off: a session whose efficiency falls as it runs is one
filling its window with things the model is not using. `/context meter` shows it
alongside cumulative totals.

### Deliberately still not taken

- **Cloud escalation** (`bin/escalation.js`) — against the prime directive.
- **Two-stage tool routing** — a real saving at 18 tools; SHAMSU sends 7 small
  schemas, so the payoff is small and the indirection is the routing complexity
  simple mode exists to remove.
- **Curated external repos** (`src/rag/curated_repos.json`) — their RAG indexes
  Flask, Django, Express and so on as *examples*. Interesting, but it is a
  different feature from searching the user's own project, and it wants a
  deliberate decision rather than being smuggled in with a search fix.
