# Live runs on small models — what actually goes wrong

**The running log.** One section per live run against a real Ollama model on a
real workspace. Started 2026-08-19, because everything in the smallcode arc
(`SMALLCODE_IMPLEMENTATION_PLAN.md`) had been verified against a *scripted*
client and the first real call died instantly on something no unit test could
see.

The rule for this file: **a number here was measured, or it is not here.** If a
run was not done, say "not run" — never leave a gap that reads like a pass.

Reproduce any run with:

```powershell
cd F:\Work\PROJECTS\shamsu\Shamsu
.\.venv\Scripts\python.exe -u <live_run.py>          # 5 turns, fresh workspace
# knobs: LIVE_MODEL, LIVE_WS, LIVE_TIMEOUT, SHAMSU_CHAT_MAX_CTX
```

---

## Status board

| # | Finding | Severity | State |
|---|---|---|---|
| L1 | `think=` sent to models that cannot think → HTTP 400, every turn dead | **critical** | **FIXED** `c5486ef` |
| L2 | Token estimate runs ~15% heavy on qwen2.5 | medium | **OPEN** |
| L3 | Tool schemas are 57–81% of the prompt, and two-stage routing is off at 32k | **high** | **OPEN** |
| L4 | Small model thrashes: 8 rounds / 292s to write a one-line file | medium | **OPEN** |
| L5 | Cold start ~110s (model load + 32k KV) | low | accepted |
| L6 | A second SHAMSU process pins VRAM and every call times out at 600s | medium | **OPEN** (no detection) |
| L7 | §D elision and §F bucket eviction still never triggered live | — | **UNVERIFIED** |

---

## Run 1 — 2026-08-19 · qwen2.5:3b-instruct · 32k · **8/9**

Five turns, fresh workspace, a fresh `SimpleChatLoop` per turn rehydrating from
disk (which is what the REPL does per user message), `--approval allow`.

| Verdict | |
|---|---|
| A calibration recorded | PASS |
| A estimate within 15% of `prompt_eval_count` | **FAIL** → L2 |
| B `num_keep` covers the system prompt | PASS (308) |
| B no cut-off reply passed off as finished | PASS (`done_reason` = `stop`, all five) |
| C `server.py` created | PASS |
| C `server.py` later edited | PASS |
| **RECALL: the port survived four turns** | PASS |
| no turn stopped on a guard | PASS |
| all five turns completed | PASS |

### What passed, and why it matters

**The recall probe.** §D's own verification list admits that *"the model can
still name every file it touched"* cannot be tested through the prompt — the
always-fresh workspace listing names every file whether history remembers it or
not. So the probe used a fact **only the conversation carries**: a port number
stated in turn 1, asked back in turn 5, with two file writes and a command run
in between.

> **"The dev server was said to run on port 8731."**

It got there via `memory_load` — item H doing the job it was added for.

---

## L1 — `think=` to a model that cannot think · **CRITICAL** · FIXED

```
ResponseError: "qwen2.5:3b-instruct" does not support thinking (status code: 400)
```

**All five turns, dead before a single token was generated.** Simple mode sent
`think=` on every call. Ollama rejects it outright for a model with no
reasoning channel.

**Blast radius: every non-reasoning model was unusable.** That is most of the
roster —

| model | `is_reasoning` | worked before the fix? |
|---|---|---|
| qwen2.5:3b-instruct | False | **no** |
| qwen2.5-coder:7b-instruct | False | **no** ← the 8GB default |
| qwen2.5-coder:14b | False | **no** |
| mistral-nemo:12b | False | **no** |
| gemma3:4b | False | **no** |
| qwen3:8b / qwen3.5:9b / deepseek-r1 | True | yes |

The cookbook has recorded `is_reasoning=False` for these all along, and
`runtime/models.py` exposes `model_is_reasoning()` for exactly this question.
Simple mode never asked it. There were two questions and only the second was
ever being asked:

```
can this model think at all    -> the cookbook, now consulted   (_should_think)
should it think on THIS call   -> unchanged        (_should_disable_thinking)
```

Unknown models default to **False**, because the failure is asymmetric: a
reasoning model asked not to think still answers; a plain model asked to think
returns a 400 and nothing else.

> **The lesson, and the reason this file exists.** 2,838 unit tests pass. Not
> one of them could catch this, because the scripted client accepts every
> keyword the real server rejects. A fake that is more permissive than the
> real thing will certify a harness that cannot make a single call.

---

## L2 — the estimate runs 15% heavy · OPEN

`prompt_eval_count / our raw estimate`, and the correction factor chasing it:

```
turn      1       2       3       4       5
drift   0.848   0.879   0.857   0.852   0.848
factor  0.944   0.879   0.861   0.856   0.853
```

**The calibration machinery is correct** — the factor walked to the true ratio
in four turns, which is exactly what item A built it to do. What is wrong is the
*uncalibrated* estimate underneath it.

It fails in the safe direction: over-counting means the budget under-fills the
window rather than overflowing it. But 15% of a 32k window is **~4,900 tokens
left on the table** on this model.

**Likely cause:** the vendored Qwen3 tokenizer counting a Qwen2.5 prompt. Worth
confirming before changing anything — `PER_MESSAGE_OVERHEAD` (8) and the tool
schema estimate are the other two candidates, and the schema block is large
enough here that a small per-schema error would show up as exactly this.

**Do not "fix" it by tuning the constant to 0.85.** That would be fitting one
model, and the calibration factor already handles per-model drift by design.
Find which of the three counters is wrong.

---

## L3 — the tool schemas ARE the prompt · **HIGH** · OPEN

Per-turn buckets, live:

```
turn   system  schemas  grounding  conversation  tool results   total
  1      308     2111        50           47            92      2,608
  2      308     2111        49          482           738      3,688
  3      308     2111        49          159           569      3,196
  4      308     2111        49           29           156      2,653
  5      308     2111        49           33            83      2,584
```

**Tool schemas are 57–81% of every prompt.** The conversation — the thing all of
§D's elision machinery exists to protect — is 29 to 482 tokens.

`_elide_under_pressure` already reports this honestly: it checks
`fattest() != "tool results"` and says *"eliding payloads will not help much
here"*. It is right, and it has nothing to offer instead.

Two-stage routing is the mechanism that fixes it, and it is **off**, because it
gates on the context window (≤ 16k) and this session runs at 32k. That gate is
smallcode's and their reasoning holds for a full window — paying an extra round
to save 2k out of 32k is a bad trade. **It does not hold here**: the prompt is
2.6k, so the schemas are not 6% of the window, they are 81% of the prompt.

**Proposed change:** gate two-stage routing on the schema *share of the prompt*,
not on the window size alone. A 2,111-token schema block in front of a 47-token
conversation is precisely the trade the router exists to make, whatever the
window happens to be.

Note this got worse as the roster grew 7 → 19. The schema block is fixed cost
paid on every single call, and it is the one bucket nothing currently reduces.

---

## L4 — the small model thrashes · OPEN

| turn | asked | rounds | tool calls | time |
|---|---|---|---|---|
| 1 | remember a fact | 2 | 1 | 132.3s |
| 2 | create a file that prints `hello` | **8** | **4** | **292.4s** |
| 3 | change it to print `goodbye` | 5 | 4 | 13.3s |
| 4 | run it, report the output | 2 | 1 | 3.7s |
| 5 | recall the port | 2 | 1 | 1.4s |

Eight rounds and four tool calls to write a one-line file. Five rounds to change
one string.

**No guard fired**, and each guard was right not to: `MAX_UNPRODUCTIVE_EDITS`
counts edits that change nothing, `EDITS_PER_FILE_BEFORE_STOPPING` counts
successful edits to one file, `REPEATED_READS_BEFORE_WARNING` counts *identical*
reads. This model did none of those. It made *progress* each round — just a
tiny amount of it.

**The gap, and smallcode has the shape for it:** `src/governor/early_stop.js`
tracks a `_readOnlyStreak` — N read-only calls with **no output produced yet**,
nudging at 5 and hard at 8. That catches "gathering context forever", which is
different from SHAMSU's "reading the same thing twice". See
`SMALLCODE_TOOL_COMPARISON.md`.

---

## L5 — cold start · accepted

First call ≈110s (model load + 32k KV allocation). Turns 4 and 5 were 3.7s and
1.4s. On a five-turn session the cold start is most of the wall clock, which is
worth remembering before reading any single-run timing as a regression.

---

## L6 — a second process pins the GPU, and nothing says so · OPEN

Two runs were lost to this before it was diagnosed. A separate SHAMSU process
held `qwen3.5:9b-q4_K_M` (6.2 GB) resident on an 8 GB card. The 3B model could
not get VRAM, spilled, and **every call ran to the full 600s timeout** with no
indication of why.

Symptom is indistinguishable from a hung model. `ollama ps` is the diagnosis:

```bash
curl -s http://localhost:11434/api/ps
```

Dropping `SHAMSU_CHAT_MAX_CTX` to 8192 made it usable again (20–65s/call) even
with the other model resident — small windows are the workaround.

**Worth building:** `/doctor` already checks that Ollama is up. It could also
report what is *resident* and whether the requested model fits in what is left.
The information is one HTTP call away, and it converts a ten-minute mystery
hang into a line of output. See also `shamsu-context-window-vram-cliff` —
`OLLAMA_KV_CACHE_TYPE=q8_0` is what makes 32k affordable at all.

---

## L7 — what this run did NOT test

Every turn sat far inside the window: `compactions=0`, `elisions=0`,
`truncations=0` throughout. So:

* **§D payload elision** — never triggered. Synthetic-only.
* **§F bucket eviction** — never triggered. Synthetic-only.
* **§B truncation handling** — `done_reason` was `stop` every time; the
  `length` path never ran live.
* **The `think=` path itself** — the fix proves we no longer *send* it to a 3B
  model, but a reasoning model's thinking channel has still not been exercised
  live in simple mode.

**Next runs, in order:**

1. **qwen3:8b, same five turns** — exercises `think=True`, which run 1 could not
   reach by construction.
2. **A long session (30+ turns) on any model** — the only way to reach
   compaction, elision and eviction, which is where §D and §F actually live.

---

## Not a finding: the suite failure seen on 2026-08-19

A full-suite run reported `1 failed, 2838 passed` —
`test_simple_mode_asks_for_the_wide_horizon`, with a traceback whose source
lines belonged to a *different* test. That run overlapped edits to
`tests/test_simple_chat.py`, so pytest read the file mid-write and reported one
test's failure against another's source. The test passes cleanly on its own.

Recorded here only so nobody re-investigates it. **Do not edit test files while
a suite is running** — the failure it invents is more expensive than the wait.
