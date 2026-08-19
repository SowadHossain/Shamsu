# SHAMSU vs SmallCode, tool by tool — and what was taken

Read from `reference/smallcode` (MIT, © 2026 Doorman11991), not from either
project's README. Written 2026-08-19 on branch `small-shamsu`.

The rule applied throughout: **where SmallCode is better, take their shape;
where SHAMSU already has the better backend, keep it and expose it.**

---

## The roster

| SmallCode | SHAMSU before | SHAMSU now | Verdict |
|---|---|---|---|
| `read_file` | `read_file` | `read_file` | even |
| `write_file` | `write_file` | `write_file` | even |
| `patch` | `patch_file` | `patch_file` | **SHAMSU better** — returns a real diff, not a line count |
| `bash` | `run_command` | `run_command` | **SHAMSU better** — risk classifier + approval gate |
| `search` (ripgrep) | `search_files` *(literal substring!)* | `search_files` (hybrid) | **taken** |
| `hybrid_search` | — | folded into `search_files` | **taken** |
| `find_files` | — | `find_files` | **taken** |
| `append_file` | — | `append_file` | **taken — the important one** |
| `graph_search` | — *(graph existed, unreachable)* | `graph_search` | **taken, better backend** |
| `explain_symbol` | — | `explain_symbol` | **taken, better backend** |
| `memory_remember` | — | `memory_remember` | **taken** |
| `memory_load` | — | `memory_load` | **taken** |
| `memory_list` | — | `memory_list` | **taken** |
| `memory_forget` | — | `memory_forget` | **taken** |
| — | — | `history_search` | **SHAMSU only** — neither project had it |
| `list_files` (n/a) | `list_files` | `list_files` | SHAMSU only |
| `find_and_read`, `search_and_read`, `read_and_patch`, `create_and_run` | — | **not taken** | see below |
| `run_tests` | — | not taken | `run_command` covers it; a second path to the same thing |
| `web_search`, `web_fetch` | exists, unexposed | not exposed | deliberate — needs a network policy decision |
| `use_skill` | `skills/` exists, unexposed | not exposed | same |
| `spawn_agent` | — | not taken | against simple mode's whole premise |
| `tdd_loop`, `contract_*` | `verify/` gate | not taken | SHAMSU verifies after every write already |
| `bone_compile`, `bone_check` | — | not taken | their DSL |
| cloud escalation | — | **refused** | against the prime directive: inference is local |
| two-stage tool routing | — | not taken | real at 18 tools; the indirection simple mode exists to remove |

**7 tools → 15.**

---

## Where SmallCode was genuinely better

**1. `append_file` — the one that mattered most.**
SHAMSU could *refuse* a whole-file rewrite (`_refuse_unwritable_rewrite`) and
could *patch* an existing snippet. Between those two there was no way to **grow**
a file. A model told "that file is too large to rewrite" had no next move except
a patch against text it was guessing at. SmallCode says it plainly in the tool
description — *"write_file for the first 50 lines, then append_file for each
subsequent section"* — and caps `write_file` at 60 lines to force the habit.
Giving the model a third option beats refusing it a second.

**2. Hybrid search.** `grep_files` matched with `query in line` — a literal
substring, not even a regex. Two representative queries went from **0 matches**
to correct answers. Their insight is that the "embedding" is feature hashing:
no model, no service. That fits SHAMSU's own "no embedding model, no vector DB"
constraint better than SHAMSU's `retriever/semantic.py`, which needs a 274MB
model pulled into Ollama.

**3. Typed memory with scored recall.** My first pass loaded every note into
every prompt. Theirs stores hundreds and retrieves the five that score against
the task. That is the difference between memory and a permanent tax on the
window.

**4. `shouldDisableThinking` on retries.** *"The model already overthought the
original solution."*

**5. Evict to a target, not past a cutoff.** They stop at `maxBudget * 0.7`.

**6. The efficiency ratio.** Completion tokens per 100 prompt tokens — the one
number that says whether the context work is paying off.

---

## Where SHAMSU was already better, and stayed

- **The edit result.** `patch_file` returns an actual unified diff of what
  changed; SmallCode returns a success string. A model that cannot see its own
  edit patches the same file seven times, which is measured behaviour here.
- **Shell safety.** `run_command` classifies risk and gates on approval.
  `bash` runs.
- **The code graph.** SmallCode's `graph_search` is backed by their local
  index. SHAMSU has a real code-memory graph — 161k nodes on this repo — with
  callers, impact and symbol resolution. It was simply never exposed to the
  model. Now it is, **and it declares when it is stale**, which theirs does not.
- **The archive.** `messages.jsonl` is append-only and lossless by design.

---

## What neither of them had: `history_search`

Both projects lose the conversation, in mirror-image ways.

SmallCode and OpenCode **compact by forking** into a new session seeded with a
summary, with no link back — the old conversation still exists and nothing can
find it. SHAMSU **compacts in place**, which avoids the fragmentation by
overwriting detail instead: older turns survive as a few summary lines, so a
decision from turn three is gone the moment the window moves past it.

The archive was never the problem. Reach was. So:

- `SessionManager.fork()` records a **parent pointer**, making history a tree
  rather than a list. The child starts on a clean window with the parent's
  summary; the parent keeps every byte.
- `history_search` searches the **entire ancestry** with the same hashed BM25
  used for code, so a plain-English question reaches a turn that never used
  those words, across any number of forks.
- `/sessions fork`, `/sessions history <query>`, `/sessions tree`.

Verified end to end: a decision made in session one, buried under 240 later
turns, then forked away from — recovered by asking *"which port does the dev
server use?"* in the child session. Forking now costs **nothing** in recall,
which is the only reason it is safe to offer.

---

## The composite tools, and why they are not here yet

`find_and_read`, `search_and_read`, `read_and_patch`, `create_and_run` collapse
two round trips into one. On a 24-round budget at ~100s a round that is a real
saving and the idea is sound.

Not taken **yet**, deliberately: each one doubles the number of ways a call can
half-fail, and the failure modes are the interesting part — what should
`read_and_patch` report when the read works and the patch does not match? That
wants designing rather than porting, and it should be measured against a live
model before it earns four more schemas in every prompt. Noted here so it is a
decision rather than an oversight.
