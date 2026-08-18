# Plan — sessions, threads, context, and concurrency

Status: **P1, P2, P3.1, P3b, P4, P5 and P6 implemented 2026-08-18** and verified
live (build across turns -> kill the process -> evict the model from VRAM ->
resume -> recall held, and the re-grounding named the file edited while away).
Remaining: P3.2 (model-written digest), P3.3/3.4 (`/compact`).

Every number here was measured on the dev machine (8188 MiB,
`qwen3.5:9b-q4_K_M`), not estimated. Where something is an assumption it says so.

**Two bugs the LIVE run caught that the unit tests did not**, both the same
shape - a timestamp read after the thing that changes it:

1. `resume_or_start` reported "resumed" with no age, because `log()` bumps
   `updated_at` before the age was measured.
2. `files_changed_since_last_activity` always returned `[]` - which reads
   exactly like "nothing changed" - for the same reason. Fixed by capturing
   `resumed_from` BEFORE the resume touches anything, on both resume paths.

---

## Why this exists

Five separate problems surfaced in one session, and they share two roots:

1. **Budgets that do not scale with the window.** A constant sized for an 8k
   context silently starves something at 32k. This caused the empty replies,
   and it caused a 4,170-char file to reach the model as 24% of itself.
2. **State that is recomputed instead of persisted.** The rolling summary is
   rebuilt from scratch every turn and never saved, so compaction cannot
   accumulate.

Both are cheap to fix and neither needs new architecture.

---

## What already works — do not rebuild these

Verified by reading the code and by live runs today.

| Capability | Where | State |
|---|---|---|
| Sessions: list/new/current/show/resume/rename/close/export | `cli/repl.py`, `session/manager.py` | works |
| Session index: `FileLock` + atomic tmp-file writes | `session/manager.py:343-354` | works |
| Resume rehydrates the conversation from `messages.jsonl` | `chat_state.py:_hydrate_from_session` | works |
| Sliding window by token budget | `chat_state.select_for_budget` | works |
| Compaction of evicted turns into a rolling digest | `simple_chat._digest` | works, but thin — see P3 |
| OOM degradation (window steps down, then evicts other models) | `simple_chat._shrink_for_oom` | works |
| 32k context at 100% GPU | `OLLAMA_KV_CACHE_TYPE=q8_0` | works |

**Two workspaces do NOT collide on sessions.** Each workspace has its own
`.shamsu/sessions/`. The session-collision risk is only two windows on the *same*
workspace (P2).

---

## Measurements this plan relies on

Concurrency, one GPU, two simultaneous requests, same model:

```
solo                    7.9s    38.3 tok/s
two concurrent          4.9s / 2.6s, 36.1 & 38.7 tok/s      wall 4.9s
resident                ONE model, 6.2 GB, 100% GPU, ctx 32768
GPU free                296 MiB -> 152 MiB during
```

Generation rate is **unaffected** by a second concurrent job; the second costs
~150 MiB, not a second copy of the model.

Model switching, `OLLAMA_MAX_LOADED_MODELS=1`:

```
same model, warm        0.7s
alternating models      7.2s / 6.0s / 4.2s / 6.0s
```

**Conclusion: concurrency is fine if both workspaces use the same model, and
expensive if they do not.** ~2 concurrent requests is the ceiling on 8 GB at 32k.

Context window vs KV cache precision:

```
f16  (default)   16384 -> 47.5s, spilled to CPU     32768 -> OOM
q8_0 + flash      8192 -> 10.3s   16384 -> 7.4s     32768 -> 7.5s, 100% GPU
```

---

## P1 — Session-scoped chat logs  *(small, high value)*

**Problem.** `SimpleTurnLog` writes **one markdown file per user turn** into
`.shamsu/chat-logs/`, and the turn counter is `number of files in the directory`.
So filenames are workspace-global, carry no session id, two sessions interleave
in one folder, and `latest.md` is whichever turn ran last in *any* session. This
is what produced `turn-001 … turn-011` with no way to tell them apart.

**Fix.** One file per session, appended per turn:

```
.shamsu/chat-logs/<session-id>--<slug>.md
```

- turn numbers scoped to the session, from session metadata not a file count
- keep the existing per-round content (full prompt, raw response, thinking,
  tool calls, results) — that part is good and was verified live
- `latest.md` becomes a pointer to the *current session's* file

**Acceptance.** Two sessions in one workspace produce exactly two markdown
files. Resuming a session appends to its existing file rather than starting a
new one.

**Touches** `agents/simple_log.py`, `agents/simple_chat.py` (pass the session id).

---

## P2 — Session identity and ownership  *(small, unblocks concurrency)*

**Problem A — invisible.** The prompt is `shamsu>` with no indication of which
thread you are in. `/sessions current` exists but nobody runs it every turn.

**Problem B — unclaimed.** `latest_active()` returns the newest active session
with no ownership marker. Two windows on the same workspace both attach to it,
both append to one transcript, and each one's hydration pulls in the other's
turns.

**Problem C — coming back tomorrow silently starts over.** VERIFIED 2026-08-18:
a conversation survives process exit AND full model eviction from VRAM (process
A stored a fact, the model was unloaded to 6997 MiB free, a fresh process B
resumed the session and recalled it). Nothing conversational lives in VRAM - the
KV cache is a speed optimisation, and losing it costs a re-prefill, not data.

The failure is the auto-resume rule, not persistence:

```
_SESSION_MAX_AGE_SECONDS = 8 * 3600   -> 8 hours
_SESSION_MAX_MESSAGES    = 200
```

Return after 8 hours, or with 200+ messages, and SHAMSU starts a NEW session.
The old thread is intact and resumable via `/sessions resume <id>`, but the only
signal is one dim line that is easy to miss. The user reads this as "everything
was lost". **The gap against Ollama's chat here is discoverability, not
storage** - it shows a sidebar of past chats; we hide ours behind a command you
must already know.

Fix: when auto-resume declines, LIST the recent sessions with their titles and
how to resume one. And raise the 8-hour default - overnight should not break a
thread, and unrelated-context bleed is what compaction (P3) is for.

**Fix.**
- show the session in the prompt: `shamsu (asteroids)>`
- claim the session with a pid + heartbeat file; if it is already claimed by a
  live process, create a new session and say so

**Acceptance.** Two REPLs in one workspace land in two different sessions, and
each prompt names its own.

**Touches** `cli/repl.py`, `session/manager.py`.

---

## P3 — Compaction that persists and carries decisions  *(the big one)*

**Problem A — the digest records questions, not answers.** `_digest` collects
user prompts truncated to 120 chars plus changed filenames, capped at 14 lines.
It remembers *"you asked to slow the ship"* and never *"we set maxSpeed to 4.5"*.
The decision is the part a later turn needs.

**Problem B — compaction is ephemeral.** `_rolling_summary` starts as `""` on
every `ChatState`, and `_hydrate_from_session` restores messages but **not the
summary**. A fresh loop is built per user message, so the digest is recomputed
from scratch every turn and never saved. Inside the 400-message hydration
horizon (~60k tokens) that is invisible; past it the early turns are gone
permanently, because the digest only ever existed in memory. That is a cliff,
not a graceful decline.

### Where the summary lives — and why NOT in `messages.jsonl`

It is tempting to append the compaction to the transcript so it "loads
naturally" with the history. **That re-introduces the bug it is meant to fix.**
`messages.jsonl` is append-only and hydration reads the LAST N; a summary
written 600 messages ago falls outside the window and vanishes again.

Session metadata always loads regardless of hydration depth, so that is the
authoritative home. Appending a human-readable marker to the transcript for
audit is fine - just never rely on the log to carry it.

### What reloads, and what is deliberately rebuilt

| | On resume |
|---|---|
| user messages, assistant responses, tool results, tool calls | **restored from disk** |
| rolling summary | **restored from metadata** (P3.1) |
| system prompt, workspace file listing, code-graph brief | **rebuilt fresh** — must not go stale |
| assistant thinking channel | not stored; only the visible text |

Structurally complete; fidelity is limited by the disk clipping in **P6**.

**Fix, in order.**

1. **Persist the rolling summary in session metadata.** Highest value, smallest
   change. Turns compaction into something that accumulates and survives a
   restart — and it is what makes "resume yesterday's thread" genuinely resume.
2. **Hybrid digest.** Keep the deterministic facts (files touched, commands run
   — exact and free) and add a **model-written summary** generated when
   compaction triggers. The current code avoids a model call on the grounds that
   "a digest that costs a round-trip gets skipped under pressure"; that was
   right for a small window where compaction fired constantly, but at 32k it
   fires rarely and the round-trip is affordable.
3. **Replace the 14-line cap with a token budget** (~5-8% of the window).
4. **`/compact` and `/compact show`** — force compaction, and let the user read
   what the model is being told about earlier work.

**Acceptance.** A conversation driven past the hydration horizon still answers a
question about a decision made in its first few turns. Test as a live probe, not
a unit test — see "How we verify" below.

**Touches** `agents/chat_state.py`, `agents/simple_chat.py`, `session/manager.py`,
`cli/repl.py`.

---

## P3b — Unlimited session length, and re-grounding on resume

Design decision 2026-08-18: **a session should never be capped, and resume
should never be refused.** Length is handled by compaction, not by cutting the
thread off. Staleness is handled by re-grounding, not by starting over.

### Order matters — this DEPENDS on P3.1

The 8-hour / 200-message limits are currently a crude guard against a real hole:
hydration caps at 400 messages and the digest is not persisted, so a
1000-message session resumed today loads 400 and **silently loses 600 with no
summary of them**. Lifting the limits before P3.1 lands trades a visible "started
a new session" for an invisible memory hole - strictly worse.

**Sequence: persist compaction (P3.1) -> then remove the limits.** Not the
reverse.

### Then

- delete `_SESSION_MAX_AGE_SECONDS` / `_SESSION_MAX_MESSAGES` as *blockers*
- keep them, if at all, only as a prompt: "this thread is 3 weeks old, resume it
  or start fresh?" - a question, never a silent decision
- compaction keeps a long thread affordable; the transcript on disk stays whole

### Re-grounding a resumed session

Already fresh, every round - do not duplicate:

- the workspace file listing (`workspace_files`, recomputed per round)
- the code-graph brief (`codebase_brief`, per user message)

What actually goes stale is **file CONTENT quoted in old messages**: a
`read_file` result from last week may describe a file that has since changed,
and the model will happily act on it.

Cheap, precise fix - on resume, diff file mtimes against `metadata.updated_at`:

```
Resuming a session last active 6 days ago.
Changed in the workspace since then: frontend/game.js, backend/api.py
Re-read those before editing them; anything quoted earlier may be out of date.
```

The tree walk already happens for `workspace_files`, so this costs a `stat` per
file and nothing else. It tells the model exactly which of its memories to
distrust, rather than vaguely warning that time has passed.

### Implementation note

`SessionMetadata` already carries `summary_updated_at` but has **no `summary`
field** - this was half-built. `from_dict` filters to known fields and tolerates
missing ones, so adding `summary: str = ""` and `summarized_upto: int = 1` is
backward-compatible with every existing session on disk.

**Acceptance.** A session with 1000+ messages resumes, answers a question about
its first few turns, and names the files that changed while it was away.

---

## P4 — Steer large edits to `patch_file`  *(fixes the 18-minute turn)*

**Problem.** Measured live 2026-08-18: one turn took **1071s over 18 rounds**
because the model rewrote whole files. Emitting a 14k-char file is ~3,500 tokens
at ~35 tok/s ≈ 100s **per write**, and it did that repeatedly. There is also a
hard ceiling: the reply reserve is ~8,192 tokens (~32k chars), so a file bigger
than that **cannot** be rewritten in one turn at all, no matter how well it was
read.

`patch_file` cost is independent of file size, and it already has fuzzy fallback
for whitespace and line-ending mismatches.

**Fix (shipped).**
- `_refuse_unwritable_rewrite`: `write_file` on an existing file larger than
  `output_reserve(num_ctx) * 4` chars is refused, because that write would be
  cut off mid-file. Not a preference - a physical limit.
- tool descriptions point at `patch_file` for existing files
- **the truncation message names the EXACT next call**

**The last one turned out to be the whole game.** Measured on a 49,579-char /
1400-line file, identical prompt both times:

| truncation message | result |
|---|---|
| "call read_file with start_line/end_line to read the rest" | **674s, FAILED** - read twice, never tried patch_file, gave up |
| "showing lines 1-2701 of 6000. For the rest call read_file(start_line=2702, end_line=6000). To CHANGE this file use patch_file" | **42s, SUCCEEDED** - read, read, patch_file |

16x faster and it actually completes. Vague guidance the model cannot act on is
worth nothing; naming the concrete next call is worth everything. Worth
remembering the next time a hint "tells the model what to do".

**Acceptance: met.** Function added, original 1400 lines preserved, file grew by
exactly the insertion (49,579 -> 49,617).

---

## P6 — Stop truncating the transcript on disk  *(ACTIVE BUG, not an improvement)*

Found 2026-08-18 reading `session/manager.py`. Messages are clipped on the way
to disk, in two places:

| Path | Cap | Constant |
|---|---|---|
| message `content` | 16,000 chars | `MESSAGE_PREVIEW_CHARS` |
| `tool_calls` arguments | **4,000 chars** | `MAX_STRING_CHARS`, via `sanitize_payload` -> `_truncate` |

Two consequences:

1. **A `write_file` call is recorded at 4,000 chars.** The file on disk is fine,
   but the model's memory of what it wrote is a fragment. On resume it cannot
   trust its own history and must re-read to be sure.

2. **In-memory and on-disk budgets now contradict each other.** Raising
   `MAX_TOOL_RESULT_TOKENS` to 8000 tokens (~32,000 chars) today - so file reads
   reach the model whole - left the disk cap at 16,000. A large read is now FULL
   in memory and HALVED on disk, so resuming hands the model half of what it
   originally saw. Same silent-degradation class as the `_compact_value` bug,
   and introduced by today's fix.

### The principle: the ARCHIVE is complete, the PROMPT is budgeted

These are two different jobs and the code currently conflates them:

| | Purpose | Size rule |
|---|---|---|
| **Archive** — what is on disk | history, audit, "show me the whole chat" | **complete, never truncated** |
| **Prompt** — what the model sees | one call, bounded by the window | necessarily budgeted |

Today `messages.jsonl` serves both, so the archive inherited the prompt's
budgets and became lossy. Splitting the concerns fixes it with one rule:

> **Truncate at READ, never at WRITE.**
> A complete record can always be narrowed. A truncated one can never be widened.

Compaction, hydration limits and `_budgeted` all stay - they operate at read
time, on the way into a prompt. Nothing clips on the way to disk except
`redact()`, which removes secrets and is not truncation.

### All three stores are currently lossy

| Store | Cap | Purpose |
|---|---|---|
| `messages.jsonl` | 16,000 content / **4,000 tool_calls** | hydration + archive |
| `events.jsonl` | 20,000 (`_clip`) | audit trail |
| `.shamsu/chat-logs/*.md` | **4,000** tool results (`_TOOL_RESULT_LIMIT`) | human-readable view |

So there is no complete record of a conversation anywhere. At least one store
must be lossless, and `messages.jsonl` is the natural one.

### The caps are not buying anything

Measured 2026-08-18 on the real asteroid project:

```
all sessions for the project      684 KB
the big all-day session           141 KB
```

Text is cheap. Even a session heavy with full-file reads lands in single-digit
MB. Disk growth is a *retention* problem - prune or archive OLD sessions - not a
reason to mutilate the session someone is still using.

**Fix.**
- `messages.jsonl` becomes the lossless archive: no clipping at write, only
  `redact()`
- apply budgets in `read_messages` / hydration instead, where the window
  actually constrains things
- raise `_TOOL_RESULT_LIMIT` in the markdown log, or make that log a rendered
  VIEW of the archive rather than a second, lossier copy
- handle disk growth by pruning old sessions, not by clipping live ones
- **invariant worth a test: on-disk fidelity >= in-memory fidelity, always**

**Acceptance.** Write a 30k-char file in one turn, resume the session in a new
process, and the transcript still shows the whole call. Print the full history
and it matches what actually happened, verbatim.

---

## P5 — Concurrency setup  *(configuration, not code)*

### Decision: queue by default, parallel slots only for autonomous work

An open question first, and it matters: two concurrent jobs cost only **+150
MiB**, not a second 6 GB KV cache. Ollama did something cheaper than allocating
a second full window and **we did not verify what**. Either

- it **splits** `num_ctx` across slots, so each session silently gets HALF the
  window - the exact silent degradation this document exists to remove; or
- it allocates dynamically and both keep full context.

**Measure this before relying on concurrency:** send a >16k-token prompt to two
concurrent sessions and see whether either truncates or errors.

Until then, prefer the queue:

| | Queue (`NUM_PARALLEL=1`) | Parallel slots |
|---|---|---|
| Context per session | **full 32k, guaranteed** | possibly halved (unverified) |
| VRAM | predictable | +150 MiB/slot, OOM risk |
| Latency | waits for the other's current REQUEST | both feel responsive |
| Complexity | trivial | needs a VRAM budget at 3+ sessions |

**Why the queue wins here:** interactive sessions are mostly IDLE, waiting for a
human to type. Real contention is rare and brief, so serialising costs almost
nothing and guarantees the full window.

**Its one real disadvantage:** while session A runs a long autonomous build,
every request from session B waits behind A's current generation. That is
per-REQUEST (seconds to ~100s), not per-task, so B still progresses - just
sluggishly. If two autonomous agents run at once routinely, parallel slots win.

### Configuration

1. **Same model in every workspace.** Biggest single lever — 0.7s vs 4-7s per
   call. Do not let one workspace pin a different model.
2. **Keep `OLLAMA_MAX_LOADED_MODELS=1`** — correct *because* they share a model.
3. **Set `OLLAMA_NUM_PARALLEL=1`** — the queue. Raise it only after measuring
   whether slots split the window.
4. **Two concurrent workspaces max** on 8 GB at 32k. A third starts shrinking
   windows via `_shrink_for_oom` — degraded, not broken.
5. **Never two agents on one repo.** That is a merge problem, not a session
   problem, and nothing here protects against it.

**Not planned:** cross-process VRAM budgeting. Ollama arbitrates well enough at
two, and the OOM path catches the rest. Revisit only if three-plus concurrent
workspaces become a real requirement.

---

## Ordering

| Step | Effort | Value | Note |
|---|---|---|---|
| Step | State | Shipped as |
|---|---|---|
| P3.1 persist the summary | **DONE** | `SessionMetadata.summary`, `save_summary`/`load_summary`, bounded by `summary_budget` |
| P6 disk truncation | **DONE** | `redact_payload` (lossless archive), `_TOOL_RESULT_LIMIT = 0` |
| P1 session-scoped logs | **DONE** | one `<session-id>--<slug>.md` per thread; `latest.md` is a pointer |
| P2 prompt + ownership + resume list | **DONE** | `owner.json` claim, `_session_prompt_label`, `_announce_other_threads` |
| P3b unlimited length + re-ground | **DONE** | age no longer blocks; `resumed_from` + `files_changed_since_last_activity` |
| P4 steer to patch_file | **DONE** | `_refuse_unwritable_rewrite`, tool descriptions |
| P5 concurrency config | **DONE** | `OLLAMA_NUM_PARALLEL=1` (queue) |
| P3.2 model-written digest | open | needs P3.1 (done), the digest is still deterministic |
| P3.3/3.4 `/compact` command | open | polish |

**Still open and worth knowing:**

- **P3.2** - the digest still records *questions and filenames*, not decisions.
  It says "you asked to slow the ship", never "we set maxSpeed to 4.5". Recall
  works today because the decision survives in a recent message or in the
  bounded summary's head; a model-written summary would make it robust.
- **The `NUM_PARALLEL` split question** (see P5) is still unmeasured. The queue
  is set precisely because it does not depend on the answer.

---

## How we verify

**Not by unit tests alone.** Today's lesson, twice over: 56 unit tests passed on
a build that had silently stopped writing files, and the suite was green while a
4,170-char file was reaching the model as a fragment. Both were caught only by
running the thing.

Every item above lands with a **live probe**:

- **P1/P2** — open two sessions, confirm two log files and two prompts.
- **P3** — drive a conversation past the hydration horizon, then ask about a
  decision from the first few turns. This is the existing 10-turn memory probe,
  extended.
- **P4** — re-run the asteroid integration turn and compare wall clock against
  the 1071s baseline.
- **P5** — two workspaces, same model, confirm both progress and neither OOMs.

---

## P7 — Context bucketing removed  *(done, and it is a simplification)*

`_num_ctx` used to size the window to the prompt. That was right at f16, where
asking 32k for an 8k prompt spilled the KV cache to system RAM. With a quantized
cache it is a false economy:

```
8192 -> 6506 MiB    16384 -> 6702 MiB    32768 -> 6891 MiB
```

The whole range is **385 MiB**, while bucketing costs:

- **a model reload on every change.** Live 2026-08-18 a compaction call at 8192
  followed by chat at 16384 produced 290s and 282s rounds.
- **half the reply reserve.** `output_reserve` is a share of the window, so the
  16384 bucket gave 4096 tokens for thinking AND answer - the starvation that
  ends a turn in empty replies.

Now: one window per session, always the ceiling. Prefill is charged on the
actual prompt, not the window, so a big window is free in time.
`_shrink_for_oom` still steps down if a GPU genuinely refuses.

**The lesson worth keeping: a measurement can invalidate an earlier one.** The
bucketing was correct when it was written and wrong by the afternoon, and
nothing flagged it - the test that covered it still passed, because it asserted
the old behaviour.

---

## P8 — A transcript that is no longer JSONL  *(done)*

Live 2026-08-18: a 99 KB `messages.jsonl` had been reformatted into indented
JSON (an editor opening a `.jsonl` does this). 655 of 657 lines failed to parse.
`read_messages` skipped them **silently** and returned ONE message. That session
hydrated with almost no history, the agent floundered for 15 minutes and gave
up, and nothing anywhere said why - it looked like a model problem.

**Fixed:** line-by-line first (the normal path), and when more lines fail than
parse, re-read the file as a stream of JSON objects. The real session recovered
**59 messages instead of 1**. `recovered_message_count` reports it and the REPL
prints a warning, because recovering silently is how the original bug hid.

**The lesson: `except: continue` on parse errors turns corruption into an
invisible, gradual loss.** Prefer recovery; failing that, prefer a loud error.

---

## P9 — No-progress detection, and the guard that became a trap  *(done)*

Both found in one live run 2026-08-18: 24 rounds, ~25 minutes, nothing fixed.

### The trap: range reads blocked writing forever

The partial-read guard marked a file "only partly seen" on ANY range read and
cleared it only on a whole-file read. So a model doing exactly what the
truncation message told it - read a large file with `start_line`/`end_line` -
read the file COMPLETELY in pieces and was still refused a write, permanently:

```
write_file -> FAILED
"You have only seen part of frontend/asteroids.js..."
```

Fixed with coverage tracking (`_seen_ranges` + `_covers`): ranges accumulate,
and once they span 1..total_lines the block clears. A GAP still counts as
partial.

Note the shape: a guard that is correct in isolation became a dead end because
the advice elsewhere in the system drove the model into it. **Guards need an
exit that the rest of the system actually leads to.**

### No-progress detection

Counted from that run's log:

```
12  no-op patches   ("old_string and new_string are identical; nothing to change")
 5  failed patches  ("old_string not found")
```

17 mutations that changed nothing, every one executed without comment. Only
`max_rounds` stopped it, and the user was told "I stopped after 24 steps" rather
than what went wrong.

Now `MAX_UNPRODUCTIVE_EDITS = 4` consecutive no-change mutations ends the turn
with what would actually help - the exact text to match, or the lines around the
problem. A successful edit resets the counter so long sessions do not trip it.

**This was item 4 in the FIRST analysis of the day and went unimplemented**,
which is what cost the 25 minutes. Bounded loops are not the same as noticing
you are not getting anywhere.

---

## Non-goals

- Rewriting the session layer. It is sound: locked index, atomic writes,
  per-session transcripts, working resume.
- A second orchestrator. Simple mode stays one loop.
- Cross-process GPU scheduling (see P5).
- Line-range *writes*. Content-anchored `patch_file` is immune to shifting line
  numbers and never leaves a half-written file on disk; range writes are neither.
