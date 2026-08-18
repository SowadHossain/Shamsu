# Context, truncation, and long chats — findings + plan

**Status: PLAN ONLY. No code changed.**
Written 2026-08-19 after a live session on `F:/Work/asteroid` where the model's
answer was cut off mid-word.

---

## 1. Why output "runs out of context" — the thing that feels wrong but isn't

> *"why will it run out of the context window on the output?? for output there
> should not be a limit right??"*

There is a limit, and it is unavoidable: **`num_ctx` is one buffer holding the
prompt AND the generated tokens together.** There is no separate output window.
The KV cache is allocated once for `num_ctx` entries; every token the model
generates is appended to the same sequence the prompt occupies. So:

```
room to answer  =  num_ctx  -  prompt tokens
```

Fill the prompt to 31,400 of 32,768 and the model has ~1,300 tokens to think
*and* answer in. It is not a policy we chose; it is how a fixed KV cache works.

**Proven from Ollama's own server log, this session:**

```
slot release: stop processing: n_tokens = 32767, truncated = 1     x19
slot operator(): new prompt, n_ctx_slot = 32768, n_keep = 4
```

`n_tokens = 32767` is the window completely full. `truncated = 1` is the server
saying it cut the generation. It happened **19 times**.

The only lever is: **keep the prompt smaller so there is room left to speak.**

---

## 2. Why the prompt got so large: the budget was measuring the wrong thing

The harness budgets against a number that is ~30% too small, so it never
trimmed. Two independent undercounts, both measured directly against Ollama:

### 2a. `tool_calls` cost ZERO

Both counters read only `m["content"]`:

- `shamsu/agents/simple_chat.py` — `_num_ctx()`
- `shamsu/agents/chat_state.py` — `select_for_budget()`

An assistant message with empty content but a `write_file` payload in
`tool_calls` is counted as nothing. Sent to Ollama directly:

```
one assistant message carrying a write_file tool_call
   SHAMSU counts :   0
   ollama sees   : 341
```

On the live session transcript:

```
tokens SHAMSU counts    :  33,986   (content only)
tokens actually sent    :  43,781   (content + tool_calls)
UNCOUNTED               :   9,795   = 22% of the prompt

biggest invisible messages:
    2,618  assistant  write_file
    2,231  assistant  write_file
    1,089  assistant  patch_file
      765  assistant  patch_file
```

### 2b. Chat-template overhead is not counted

Every message is wrapped in role markers and special tokens by the chat
template. Measured against `prompt_eval_count`:

```
1 message : SHAMSU  50   ollama  60    (+10)
3 messages: SHAMSU 152   ollama 170    (+18, ~6/msg)
6 messages: SHAMSU 305   ollama 332    (+27, ~4.5/msg)
```

At ~101 messages that is another ~500 tokens.

### 2c. The result, on the exact round that got cut

```
num_ctx                        32,768
SHAMSU thought the prompt was  21,381   <- what it budgeted against
the prompt really was         ~31,400
headroom left to answer in     ~1,300   <- the reserve was meant to be 8,192

thinking produced                  95 tokens, ending mid-word:
   ...'id spawner found" means `window.asteroid'
content                          empty
round wall time                  51s    (~1.9 tok/s -> almost all PREFILL)
```

**The harness did not truncate anything.** Verified end to end: `body =
result.final.strip()` then `console.print(Markdown(body))`, no clipping on the
display path, and `_TOOL_RESULT_LIMIT = 0`. What the user saw is exactly what
the model produced.

Also: the `...` after `window.asteroidSpawner...` is the **model's own**
ellipsis — it trails off and then writes "Wait, looking at the error:" to
correct itself. There was only ONE cut, at the very end.

### 2d. And then the harness presented the fragment as a finished answer

The model produced no `content`, so the loop's salvage path fired —
`model only reasoned; using its thinking as the answer` — and displayed a
truncated thought as the final answer **with no indication it was cut**. Ollama
returns `done_reason: "length"` for exactly this case and the harness ignores
it.

---

## 3. The other bug behind "it compacts every single turn"

> *"why is it compacting the last conversation every time?? the very next prompt
> it compacted 23 older messages again"*

`shamsu/agents/chat_state.py`, in `_restore_summary()`:

```python
self._summarized_upto = max(self._summarized_upto, 1)   # always 1
# "eviction accounting restarts from the hydrated messages"
```

A fresh `ChatState` is built **per user turn**, so this runs every turn.
Measured on the live session:

```
session.json on disk : summarized_upto = 24
load_summary()       : returns 24
after hydration      : 1          <- what the loop actually uses
```

So `newly_evicted()` returns everything from the start of history **every
turn**, and each turn spends an extra model call re-summarising the same
messages. It was deliberate (an absolute index is meaningless after a partial
hydration) but the cure throws away correct information in the normal case.

---

## 4. How Ollama itself handles long chats — the honest answer

**It doesn't do anything clever. It silently forgets.**

- `/api/chat` is **stateless**. The server keeps no conversation. Every request
  must carry the entire message list. There is no "remember this" option, which
  is why the system prompt is re-sent every call (that part is fine — a stable
  prefix is KV-cached and costs no time).
- When the prompt does not fit, llama.cpp/Ollama **drops tokens from the front**
  and keeps `n_keep` (observed here: `n_keep = 4`). The oldest turns are gone
  and nothing tells you.
- `truncated = 1` in the log is the server reporting it cut something.

So "plain Ollama chat handled long conversations fine" means **it quietly
dropped the beginning of the conversation**. Nothing was managing context; the
losses were just invisible. SHAMSU tries to do better (token budget + rolling
summary) and would be better — the budget was simply measuring the wrong
number.

Worth keeping in mind: silent forgetting *feels* smoother than an honest "I ran
out of room". Whatever we build should not trade correctness for that feeling,
but it should also not be noisy.

---

## 5. The user's proposal, assessed

> *"for every chat we should also request the model to create compacted info of
> that specific prompt's output, so we don't need to read the whole conversation
> and compact every time. So instead of sending the whole chat again we send the
> compacted version of the past conversations."*

**Yes — this is the right direction, and it fixes §3 as a side effect.** It is
the standard "progressive summarisation" shape: summarise each turn *once, when
it happens*, append it to a durable running digest, and send
`digest + recent verbatim turns` instead of the whole history.

Three caveats worth designing around:

1. **Recent turns must stay verbatim.** `patch_file` needs the exact text of a
   file. A summary saying "changed the bullet speed" cannot be patched against.
   So: digest for old turns, verbatim for the last N.
2. **The bulk is tool results, not prose.** In this session the largest items
   were `write_file`/`patch_file` payloads and file reads — thousands of tokens
   of source that the model can simply **re-read on demand**. Summarising those
   is wasted effort; *eliding* them ("[file contents omitted - call read_file]")
   is both cheaper and lossless, because the file is still on disk.
3. **Summarising costs a model call.** Doing it once per turn is fine. Doing it
   every turn over the same messages (§3) is what made it expensive.

---

## 5b. "Why hold the model's response in context at all? Save it to disk."

It **is** already on disk - `.shamsu/sessions/<id>/messages.jsonl`, written per
message, losslessly. Disk is not the problem.

The problem is that **the model is stateless**. Ollama keeps nothing between
calls; whatever we want it to know, we hand it again in the next request. Disk
helps *us* (audit, resume, this investigation); it does nothing for the model.

But that does NOT mean re-sending it *verbatim*. There are two different needs,
and conflating them is what makes the prompt huge:

- **Within one turn** (the read -> edit -> verify loop): the model MUST see what
  it just did, verbatim-ish. When it cannot, it repeats itself - that is exactly
  the 12 no-op patches and the 3 identical `list_files` calls found earlier.
- **Across turns**: it needs the *gist* - which files, what changed, what was
  decided. Not the bytes.

So the rule is: **verbatim inside the turn, digest across turns** - and the
bytes of any file are never worth keeping in either, because the file is on
disk and can be re-read on demand.

## 5c. "The code shouldn't show up in the chat - just the file and the diff"

Agreed, and this is the single biggest win available. Right now code enters the
context **twice**:

1. **`write_file` arguments** carry the entire new file inside `tool_calls`.
   Measured: one such message = **341 tokens** where SHAMSU counted 0; the two
   worst in the live session were **2,618** and **2,231** tokens.
2. **`read_file` results** carry the whole file body.

Both have already served their purpose the moment the call returns. What is
worth keeping afterwards is what you asked for:

```
patch_file frontend/game.js   +3 -1
    - if (asteroidArray.length === 0) {
    + if ((window.windowAsteroids || []).length === 0) {
```

SHAMSU already computes that diff (`_with_diff`, capped at `MAX_DIFF_LINES`).
What it does not do is **throw the payload away afterwards**. That is the fix.

## 5d. How other agents actually do it

Read from source at `F:/Work/opencode - ai` (Go), not from memory.

**OpenCode:**

- **An explicit output reserve.** `config.go` clamps the agent's `MaxTokens` to
  `ContextWindow / 2` and logs a warning when it has to:
  *"max tokens exceeds half the context window, adjusting"*. SHAMSU has a
  reserve in principle (`output_reserve`) but never enforced it against the
  REAL prompt size - which is this whole bug.
- **The edit tool returns a diff, never the file.**
  `diff, additions, removals := diff.GenerateDiff(...)` - the tool response
  carries `Diff`, `Additions`, `Removals`. Exactly the shape asked for above.
- **Hard caps on tool output**: bash output `MaxOutputLength = 30000` chars,
  truncated **in the middle** keeping both ends
  (`"... [N lines truncated] ..."`); file reads default to **2000 lines**,
  `MaxReadSize = 250KB`, `MaxLineLength = 2000`.
- **Compaction is MANUAL and forks the session.** `Summarize()` is called from
  exactly one place - a TUI command. It sends the whole history to a *separate
  summariser model* with "provide a detailed but concise summary... what we did,
  what we're doing, which files we're working on, what we're going to do next",
  then **creates a new session** seeded with that summary. There is no automatic
  trigger on approaching the context limit.

**Codex:** only the compiled binary is present here (`codex.exe`), so this is
not verified from source and should not be treated as fact. The generally
described pattern for that family of agents is the same three moves: cap tool
output, keep recent turns verbatim, and compact older history into a summary
when the window fills.

**What this tells us:** nobody has a clever trick. Everyone does the same three
things - **(a) reserve output room, (b) cap tool output hard, (c) summarise old
turns**. SHAMSU has all three in some form; (a) was measured against the wrong
number and (b) was never applied to `tool_calls` at all.

## 6. Plan

Re-ordered after reading OpenCode's source. Nothing here is implemented.

### P1 - Count what is actually sent  *(without this, everything else is guesswork)*

One helper used by **both** `_num_ctx()` and `select_for_budget()`:

```
message_tokens(message) = count_tokens(content)
                        + count_tokens(json(tool_calls))   if present
                        + PER_MESSAGE_OVERHEAD             (~8, measured)
```

**Verify:** replay the live session; the estimate must land within a few percent
of Ollama's `prompt_eval_count`, and headroom must return to ~8,192.

### P2 - Elide tool payloads once they have served their purpose  *(the big reclaim)*

This is §5c, and it is worth ~9,800 tokens on the measured session by itself.
Once a call has returned, rewrite what stays in the conversation:

```
write_file frontend/game.js          ->  wrote frontend/game.js (214 lines)  +214 -0
patch_file frontend/game.js          ->  patched frontend/game.js  +3 -1
                                             - if (asteroidArray.length === 0) {
                                             + if ((window.windowAsteroids || []).length === 0) {
read_file  frontend/game.js (17KB)   ->  read frontend/game.js (462 lines) [re-read if needed]
```

Lossless: every file is still on disk and `read_file` can fetch it again. This
is what OpenCode's edit tool does natively - it returns `Diff`, `Additions`,
`Removals` and never the file body.

Keep the **most recent** payload verbatim (the model is usually mid-edit on it);
elide the older ones.

### P3 - Guarantee output room explicitly

SHAMSU never sends `num_predict`, so generation is bounded only by whatever is
left of the window - which is how it ended up with ~1,300 tokens. Set it, and
enforce the reserve the way OpenCode does (`MaxTokens <= ContextWindow / 2`,
with a warning when it has to clamp).

**Verify:** with a deliberately huge prompt, the request is refused or trimmed
BEFORE sending, rather than producing a truncated answer.

### P4 - Never present a cut-off thought as a finished answer

Read `done_reason`. When it is `"length"`:

- say so plainly ("I ran out of room to answer - the conversation is long, try
  `/new`, or ask for a smaller piece"),
- keep the partial text but label it partial,
- optionally retry **once** with payloads elided (P2 usually frees enough).

**Verify:** force a tiny `num_ctx`; the user must get an explanation, not a
fragment presented as an answer.

### P5 - Summarise each turn ONCE, when it happens  *(the user's proposal)*

At the end of each user turn, ask for a 3-6 line digest of *that turn only*:
decisions, file paths, numbers, what changed. Append to a durable digest in
`session.json`. Prompts become:

```
system prompt  +  durable digest  +  last N verbatim turns  +  grounding  +  request
```

Note OpenCode compacts **manually** and forks into a new session. Doing it
per-turn is more ambitious; the risk to watch is a digest that drifts or
compounds errors. Mitigation: digest each turn independently from that turn's
own messages, never by re-summarising the previous digest.

**Verify:** turn 20 still recalls a fact set in turn 2, and prompt size stops
growing linearly with conversation length.

### P6 - Make eviction accounting survive rehydration  *(fixes §3)*

Anchor `summarized_upto` to the append-only transcript rather than the hydrated
list's indices, and translate on load.

**Verify:** two consecutive turns where nothing new falls out report **0**
newly-evicted on the second, and "compacted N older messages" stops appearing
every turn.

### Open questions for the user

1. P1 makes the effective verbatim history **shorter** - the hidden 30% becomes
   visible and older turns move into the digest. Correct, but it will feel like
   less raw memory. Accept?
2. On hitting the limit: explain only, or auto-retry once with payloads elided?
3. P5 per-turn digest (ambitious, keeps context flat) vs OpenCode's manual
   compact-and-fork (simpler, proven, but you have to ask for it). Which?

---

## 6b. Auto-compaction: can it be done, and is it a good idea?

**Can it be done: yes**, and most of the machinery already exists -
`_compact_if_needed()`, `_narrate()` (model-written digest), and a persisted
`summary` / `summarized_upto` in `session.json`. What is missing is a correct
trigger; what is broken is the counter that would drive it (§2, §3).

**Is it a good idea: yes, but as the LAST line of defence, not the first.**
Measured on the real 130-message session:

```
   keep verbatim | prompt tokens | vs today | turns before a 24k budget fills
   --------------|---------------|----------|--------------------------------
        nothing  |         7,569 |     16%  | ~79
        last 10  |         8,228 |     18%  | ~73
        last 20  |        10,476 |     23%  | ~57      <- the sensible setting
        last 30  |        11,350 |     25%  | ~53
     everything  |        44,833 |    100%  | ~13      <- today
```

Eliding tool payloads while keeping the **last ~20 messages verbatim** takes a
session from **~13 turns to ~57** - 4.4x longer - **with no information loss at
all**, because every elided byte is still on disk and `read_file` can fetch it.

Compaction, by contrast, is **lossy by definition**. So the order matters:

1. **Count correctly** (P1) - otherwise the trigger fires at the wrong moment.
2. **Elide payloads** (P2) - 4.4x more headroom, lossless.
3. **Then auto-compact** - for the sessions that still run long.

Doing auto-compaction *first*, on top of today's counting, would fire either too
early (throwing away detail that did not need to go) or never (which is the bug
we have).

### Design rules for auto-compaction

1. **Only at a turn boundary. Never mid-loop.** Compacting between round 3 and
   round 4 of an edit destroys the exact file text the model was about to patch
   against. This is the single biggest way to make it harmful.
2. **Compact in place; do not fork the session.** OpenCode forks to a new
   session, which suits its UX. SHAMSU has `/sessions` and resume, so forking
   would fragment the list and muddy "continue". Keep one thread and replace the
   old messages with the digest - the full transcript stays on disk either way.
3. **Trigger on the REAL count** (needs P1), at roughly **70%** of the prompt
   budget, so there is slack to compact *in*.
4. **Say what happened, and make it inspectable.** "Compacted 40 older messages
   - full transcript in `.shamsu/sessions/<id>/`". Silent loss is what made this
   feel unreliable in the first place; an honest line costs nothing.
5. **Deterministic facts + model narrative.** Keep the existing split: `_digest`
   records what was asked and which files were touched (exact, free, cannot
   hallucinate); `_narrate` adds the decisions. Never re-summarise a summary -
   digest each turn from that turn's own messages, or errors compound silently.
6. **Never compact on a failed or truncated turn.** If `done_reason == "length"`
   the model did not finish its thought; folding that into a digest bakes in a
   half-formed conclusion.

### What this changes in the plan

Nothing is removed. P1 -> P2 -> P4 stay first; auto-compaction is the natural
extension of P5, with the trigger from P1 and the rules above.

---

## 6c. SmallCode (github.com/Doorman11991/smallcode) - what to take

Read from source via `gh` (default branch is `master`, not `main`). Roughly five
files, not the whole repo - treat the rest as unverified.

**Yes, it is close to the same thing.** Self-described: *"a terminal-native
coding agent designed from the ground up to extract useful work from local
models (8B-35B) running on consumer hardware"*, *"87% benchmark with 4B-active
model"*. Same goal, same constraint, and it has already solved several of the
exact problems diagnosed above.

### Worth taking

**1. Ground truth from the API response, not an estimate.**
`src/session/tokens.js` - `extractUsage(response)` reads `prompt_tokens` /
`completion_tokens` straight off the response ("Adapted from OpenCode's getUsage
pattern"). Their own `estimateTokens` is a crude `chars/4` and is only ever an
estimate; the real number comes back from the model. This is exactly P2, and it
is the fix for the whole class of bug in §2 - **an estimate that nobody checks
against reality will drift, and drift silently.**

**2. A live context meter.** `bin/token_monitor.js` -
`contextMeter(window) -> { pct, used, window }`, driven by the most recent
prompt size. The user can *see* how full the window is. This is a new idea,
not in the plan above, and it directly answers "I have no idea what's going on".

**3. Budget tracked BY CATEGORY, not as one number.**
`marrow/src/context/budget.ms`:

```
TokenAllocation { system_prompt, working_memory, conversation, tool_results, available }
totalBudget() = model_context_length * max_budget_pct / 100
```

Four buckets instead of one total. That means you can see *which* bucket is
eating the window and evict from the right one - rather than blindly dropping
the oldest messages, which is what SHAMSU does today.

**4. Observability on compaction itself.** The token monitor counts
`compactions` and `evictions`, and the file's own header says it exists to
*"verify context compaction is working correctly"*. Given §3 (compacting the
same 23 messages every turn, unnoticed for a whole session), this is the
counter that would have caught it immediately.

**5. Tool results capped, and evicted MID-TURN.** Results capped at 4k chars;
old results dropped when the window is approached *within* a turn, not only
between turns. SHAMSU caps at `MAX_TOOL_RESULT_TOKENS = 8000` but never evicts
mid-turn - and never counted `tool_calls` at all.

**6. `summary_threshold`** - files above a line count get summarised instead of
pasted whole.

**7. Working memory: a persistent scratchpad.**
`marrow/src/context/working_memory.ms` - `.smallcode/memory.md`, token-capped,
loaded into context, survives across turns, *"compensates for small models'
limited internal reasoning"*. This is **not** the same as compaction: it is the
model's own notes, written deliberately, rather than a lossy digest of the
chat. Worth considering alongside P5 rather than instead of it.

### Deliberately NOT taking

- **Cloud escalation** (`bin/escalation.js`, and cloud pricing tables in
  `tokens.js`) - routes hard failures to Claude/GPT/DeepSeek. Directly against
  SHAMSU's prime directive: inference is local.
- **2-stage tool routing** (model picks a category, then gets only those
  schemas) - a real saving when you have 18 tools. SHAMSU sends 6 small schemas
  per call, so the payoff is small and the added indirection is exactly the
  routing complexity simple mode was built to remove.

### What this changes

It does not change the plan, it **confirms and sharpens** it:

- P1/P2 (count correctly, elide payloads) stay first - they have the same caps.
- P2's calibration is **promoted**: take the number from the response, do not
  merely correct an estimate with it.
- **Add: a context meter in the REPL** (pct of window used), plus compaction and
  eviction counters. Cheap, and it turns the whole class of "silent context bug"
  into something visible.
- **Consider: per-category budget buckets** rather than one total.
- **Consider: a working-memory scratchpad** as a complement to the digest.

---

## 7. Everything else found in this session

For the record, since these came out of the same investigation.

### Fixed and verified

| # | Bug | Impact |
|---|---|---|
| 1 | **`search_files` never worked** — schema said `pattern`, `grep_files` reads `query`, and the alias table mapped `query`→`pattern` (backwards) | 1 of 6 tools dead since birth; every search returned "Missing or placeholder query" |
| 2 | **Telegram ran the LEGACY loop** — `integrations/telegram/sessions.py` built `AgentChatLoop` unconditionally, and `get_or_create_latest()` picks the desktop's own session | wrote `project.inspect` / `file.read` / `code.search` / `test.run` into a simple-mode transcript; invisible from the desktop because Telegram writes no chat log |
| 3 | **`proceed` / a bare "yes" / pending plans bypassed the simple-mode guard** — slash commands dispatch in `main()` before `_handle_request` | one word could drop the session into the old orchestrator |
| 4 | **Foreign tool vocabulary was replayed to the model** | a model imitates its own transcript, so it kept calling tools it cannot execute |
| 5 | **Harness stop-messages replayed as the model's own answers** (`The model did not respond within 600s.`) | taught the model that stopping is how a turn ends |
| 6 | **Command timeout unenforceable** — `subprocess.run(capture_output=True, timeout=N)` kills the shell but a surviving grandchild holds the pipes, so `TimeoutExpired` never fires | a 28-minute hang against a 120s timeout; proven: asked for 5s, still blocked at 25s |
| 7 | **Approval prompt read the console from a WORKER thread** (`asyncio.to_thread`) | the run_in_executor+stdin trap; on Windows the input stack is main-thread-owned — this was the "it gets stuck" hang |
| 8 | **The 5s tool heartbeat painted over the approval prompt** | answering within 5s worked, pausing to think did not — self-inflicted, introduced the same day |
| 9 | **The Windows key reader looped in total silence** on any key that was not y/a/n, Enter included | a prompt that ignores you is indistinguishable from a hung one |
| 10 | **`num_ctx` 8192 vs 32768 across subsystems** — three `llm/manager.py` defaults | a ~6GB model reload on **every** call; 74–107s replies instead of 5–15s |
| 11 | **The volatile workspace block sat at message position 1** | it changes whenever a file does, invalidating the KV prefix cache and forcing a full re-prefill of the whole conversation |
| 12 | **"Review and plan" built instead of planning** — the prompt said, unconditionally, "Work in small steps: make one change, check it" | 24 rounds / 577s / 5 files written / no plan → now 4–8 rounds / 59–134s / no files / a real plan |
| 13 | **Dead code** — 14 paths / ~3,518 lines moved to `legacy-code/` | selected by three agreeing checks after a regex-based first attempt broke the CLI |

### Still open

| # | Item | Note |
|---|---|---|
| 14 | **`repl.py` split** | 451 functions, 18,780 lines; the CLI imports **271 of 292 modules** because of it. Gates ~47,000 lines of legacy removal. |
| 15 | **Prose-nudge false positive when planning** | `describes_an_unmade_edit` fired during a plan ("described a change ... without making it"). Describing a change *is* the deliverable when planning. Cost one round; did not derail the result. |
| 16 | **Ollama's `n_keep = 4`** | if a prompt ever does overflow, the server drops from the front and keeps almost nothing. Worth setting deliberately so the system prompt survives. |

### Method notes worth keeping

- **Verify a guard by removing it.** Every structural fix here was confirmed by
  reverting it and watching the test fail with the right message. Three
  would-be-vacuous tests were caught that way.
- **A test that pins a message INDEX fails for the wrong reason.** Three did
  when the grounding block moved; they now find it by content.
- **A probe that passes can still be the wrong probe.** In `cmd.exe`, `&` is a
  sequential separator, not backgrounding — so the first timeout probe "passed"
  in 8.5s while the real detached case took 120s.
- **Resource contention explains SLOW, never STUCK.** When the symptom is "it
  never returns", stop measuring throughput and find the blocking call.
