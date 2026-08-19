# SHAMSU — open issues and what we fixed

**The tracker.** One place for every known defect, with evidence, and what
happened to it. Opened 2026-08-19 on branch `small-shamsu`.

Rules for this file, because a tracker nobody trusts is worse than none:

* **An issue is OPEN until something proves it closed.** A commit is not proof;
  a test that fails when you remove the fix is.
* **Every claim carries its evidence** — a command, a number, a file and line.
  "Probably fine" is not a state.
* **Nothing gets deleted.** Closed issues move to the bottom with what fixed
  them, because the next person needs to know it was once broken.
* Severity is about **what a user loses**, not how hard the fix is.

Companion docs: `TRUNCATED_FILES_REPORT.md` (C1-C4, the truncation investigation),
`SMALL_MODEL_LIVE_RUNS.md` (the per-run log),
`CONTEXT_AND_TRUNCATION_PLAN.md` (the context investigation),
`SMALLCODE_TOOL_COMPARISON.md` (what was taken from smallcode and what was not).

---

## Status board

### Open

| # | Issue | Severity | Area |
|---|---|---|---|
| [C11](#c11) | Syntax checking covers 13 extensions; `.html`, `.php`, `.rb`, `.yaml`, `.toml`, `.cs` and more get no check at all | medium | verify |
| [C5](TRUNCATED_FILES_REPORT.md) | **Verbatim tail is 51% of the prompt** — 20 messages kept whole, one was 25,473 chars | **high** | context |
| [C12](#c12) | A stale assistant claim outlives the read behind it, and a re-read after a user correction is not marked as one | medium | context |
| [C9](TRUNCATED_FILES_REPORT.md) | Harness nudges recorded as "you asked" in the compaction digest | medium | context |
| [C4](TRUNCATED_FILES_REPORT.md) | Cut-off notice blames the window, is replayed 53x into later prompts | medium | context |
| [M1](#m1) | **Memory is only written if the model volunteers** — a real 2-turn run produced none | **high** | memory |
| [M2](#m2) | `memory.db` absence in simple mode is expected; `status.json` says otherwise | info | memory |
| [G1](#g1) | Code graph holds **239 projects**, mostly July eval scratch dirs; this repo is not among them | **high** | graph |
| [G2](#g2) | Graph indexes `reference/`, `other peoples work/`, `legacy-code/` and answers out of them | **high** | graph |
| [L3](#l3) | Tool schemas are **57–81% of every prompt**; two-stage routing is off at 32k | **high** | context |
| [W1](#w1) | `.shamsu/` records the same turn four times; `paths.py` governs 7 of its 13 entries | medium | structure |
| [W2](#w2) | **No session retention** — 127 sessions / 9.9 MB in one workspace, never pruned | medium | structure |
| [W3](#w3) | Session files carry conversation content with default permissions | low | security |
| [W0](#w0) | Global workspace tracking: neither project does it — answered, not a defect | info | structure |
| [G3](#g3) | Graph answers are truncated in arrival order, not ranked by relevance | medium | graph |
| [L2](#l2) | Token estimate runs **~15% heavy** on qwen2.5 | medium | context |
| [L4](#l4) | Small model thrashes — 8 rounds / 292s to write a one-line file | medium | agent loop |
| [L6](#l6) | A second process pinning VRAM produces silent 600s timeouts | medium | ops |
| [S1](#s1) | Clean full-suite run never completed | medium | test |
| [T1](#t1) | No read-loop detector — "reads forever, never produces output" is invisible | medium | agent loop |
| [T2](#t2) | No greeting-regression detector | low | agent loop |
| [T3](#t3) | Item G only half-landed: no git-diff injection, no vague-prompt screen | low | context |
| [R1](#r1) | `repl.py` is 19,041 lines and gates ~47,000 lines of legacy removal | medium | structure |
| [L7](#l7) | §D elision and §F eviction have **never run live** | — | unverified |

### Fixed this session

| # | Issue | Fixed by |
|---|---|---|
| [L1](#l1) | `think=` sent to models that cannot think → HTTP 400, **every turn dead** | `c5486ef` |
| [C3](TRUNCATED_FILES_REPORT.md) | `patch_file` 0/24 success — literal `
` in `old_string` could never match | `66fc252` |
| [C6](TRUNCATED_FILES_REPORT.md) | Identical failing patch retried 9x — stall counters reset every user turn | `4dfc17b` |
| [C8](TRUNCATED_FILES_REPORT.md) | Same patch error returned 29x unchanged, never escalated | `a342fd1` |
| [C7](TRUNCATED_FILES_REPORT.md) | **A turn ending on "let me fix this:" with no tool call was accepted as done** (14x) | `a7a5631` |
| [C10](TRUNCATED_FILES_REPORT.md) | **Elision deleted the file it read and kept the wrong conclusion** — 15 stubs vs 8 surviving false claims | `09f29ee` |
| [C1](TRUNCATED_FILES_REPORT.md) | **Truncated generations committed their writes** — 3 JS files cut mid-code | `b08d298` |
| [C2](TRUNCATED_FILES_REPORT.md) | **`_verify` reported "no syntax errors" for files it never opened** (572x in one session) | `dbbaaa1` |
| [F1](#f1) | Rolling summary dropped when hydration skipped what it described | `8b338d5` |
| [F2](#f2) | Context meter reported 42,440 tokens of tool results inside a 23,595-token prompt | `8b338d5` |
| [F3](#f3) | 11 ruff errors while docs claimed "Lint: passes" | `152fbb0` |
| [F4](#f4) | `/context meter` worked but appeared in no help text | `152fbb0` |
| [F5](#f5) | Ollama keeps 4 tokens on overflow — system prompt was first casualty | `5a55e6d` |
| [F6](#f6) | Prose-nudge told a *plan* to stop planning and write code | `049204f` |
| [F7](#f7) | Three docs described a state that had not been true for weeks | `c4dcad3` |

---

# OPEN

<a name="c12"></a>
## C12 — the other two thirds of RC10's fix · MEDIUM

`09f29ee` landed RC10 fix 1, the one the report calls sufficient on its own:
the current contents of a file survive elision. Fixes 2 and 3 did not, and
saying "C10 is fixed" without recording that would overstate it.

**Fix 2 — elide stale assistant claims too.** A conclusion is not more durable
than the observation it rests on. Assistant prose is currently not elidable at
any pressure, which is why eight restatements of one wrong diagnosis survived
while every read that contradicted them was dropped. If the read behind a claim
is gone, the claim should be summarised or dropped with it.

**Fix 3 — mark a read that follows a user correction.** *"Re-read after the user
said the previous diagnosis was wrong"* is one line, and it is exactly the
context a small model needs to override its own earlier statement. The user
saying *"the first error does not exist"* carried no more weight than any other
sentence in the transcript.

Both are lower severity than C10 was, because fix 1 breaks the loop: the model
can now see the file it read. These make it less likely to argue with it.

---

<a name="c11"></a>
## C11 — the checker roster stops short of "every major language" · MEDIUM

Opened out of C2. `dbbaaa1` made the verifier honest — it now says `skipped
(no checker for .html)` instead of certifying a file it never opened — but
honest is not the same as covered.

### What is checked today

`shamsu/agents/simple_verify.py`, after `dbbaaa1`:

| checker | extensions | needs |
|---|---|---|
| `compile()` | `.py` `.pyi` | stdlib |
| `json.loads` | `.json` | stdlib |
| `node --check` | `.js` `.mjs` `.cjs` | node on PATH |
| bracket scan | `.js` `.mjs` `.cjs` `.jsx` `.ts` `.tsx` `.css` `.scss` `.less` `.c` `.h` `.cpp` `.hpp` `.java` `.go` `.rs` | nothing |

Uncovered and common: `.html` `.xml` `.php` `.rb` `.cs` `.swift` `.kt` `.dart`
`.yaml` `.yml` `.toml` `.sh` `.sql` `.md`.

### For comparison, smallcode

`reference/smallcode/bin/model_client.js:183` (`runValidation`) covers **seven**
extensions and gates four of them on a project marker:

```
.ts/.tsx  tsc --noEmit        only if tsconfig.json exists
.py       python -m py_compile
.rs       cargo check         only if Cargo.toml exists
.go       go build ./...      only if go.mod exists
.js/.mjs  node --check
.json     JSON.parse
.bone     their own compiler
everything else -> return null
```

No `.jsx`, no `.cjs`, no `.css`, no `.html`, no `.java`, no `.c`. Their
`src/lsp/client.js` adds tsserver / pyright / rust-analyzer / gopls, but only
when the user has installed those servers.

So this is not a gap against them — the zero-dependency bracket scan already
covers more ground than their whole table on a machine with no toolchain
installed, which is the machine SHAMSU is built for. It is a gap against the
claim "all major languages".

### Fix

Two tiers, and the second must never become a requirement.

1. **Always on, zero-dependency.** Extend the bracket scan to `.php` `.cs`
   `.swift` `.kt` `.dart`; tag balance for `.html` and `.xml`; `tomllib` for
   `.toml` (stdlib since 3.11); `yaml.safe_load` for `.yml` when PyYAML
   imports; fence balance for `.md`. This is the tier that catches truncation,
   which is the failure that opened this whole investigation.
2. **When the toolchain happens to be present.** `ruby -c`, `php -l`,
   `gofmt -e`, `tsc --noEmit`, `cargo check` — behind the same `shutil.which`
   gate `node --check` already uses, falling back to tier 1 when absent.

Anything still unmatched keeps saying `skipped`. The escape stays.

---

<a name="m1"></a>
## M1 — memory only exists if the model volunteers, and it usually does not · **HIGH**

Reported from a real session: *"I am running the harness on this workspace but
it didn't create any memory.db files."*

### The measurement

`test-shamsu/test1`, prompt *"make me 3d asteroid game"*, model
**qwen3.5:9b-q4_K_M** (the out-of-box default), 2 turns:

```
REAL tool invocations          .shamsu/memory/ contents
  6  write_file                  status.json          <- and nothing else
  1  run_command                 (no notes/)
  1  read_file                   (no memory.db)
```

`memory_remember` was **never called.** The system prompt advertises it on every
turn (*"You remember this project: memory_remember keeps a decision or a
gotcha..."*), the schema is sent on every call, and the model still never
reached for it. So the session ended with no memory at all.

### smallcode, same shape of session, same machine

`F:\Work\smallcode\project\.memory\` — **six notes**, and not one of them
was written by the model:

```
# hi did it get stuck? have you finsihed mielstone 0??
**Type:** context
**Tags:** evidence, success
Task: hi did it get stuck? have you finsihed mielstone 0??
Files: backend/main.py
Successful steps:
- write_file backend/main.py
Duration: 122.3s
```

The title is the user's raw prompt — typos preserved (`mielstone`, `expained`,
`ruid`) across all six. The body is a structured record of what the turn *did*.
These are **harness output**, not model output.

### The mechanism, from their source

`src/memory/evidence.js`:

> *"Automated capture of 'what was tried, what worked, what failed' per task.
> **Distinct from manual memory (decisions/conventions): evidence is
> auto-derived from the trace recorder at task end.**"*

It extracts failed commands and their error tail, successful commands of value,
files created or edited, validation outcomes, and net duration — deliberately
**not** full traces ("those are 5-50KB each"; evidence is a 1-3KB digest).

**So smallcode has two writers and SHAMSU has one:**

| writer | smallcode | SHAMSU |
|---|---|---|
| model calls a memory tool (decisions, conventions) | yes | yes — `memory_remember` |
| **harness records evidence at task end** | **yes** | **no** |

`shamsu/agents/simple_chat.py:2451` is the *only* caller of `remember()` in
simple mode, and it sits inside the `memory_remember` tool handler.

### Why this is high severity

Item H was justified on the grounds that a scratchpad *"compensates for small
models' limited internal reasoning."* The models that most need it are exactly
the ones least likely to spend a tool call on it. A memory system that fires
only when the model volunteers is one that is empty when it matters — which is
what the run above shows.

It also means every finding in `SMALL_MODEL_LIVE_RUNS.md` about recall rests on
a run where **I explicitly prompted** *"remember this for later"*. That proved
the store works. It did not prove memory accumulates on its own, and it does not.

### Fix

**The raw material already exists.** Every turn already writes
`.shamsu/runs/<id>/`, `.shamsu/mutations/<id>/` and a turn log — the same facts
smallcode distils. Nothing needs to be captured; something needs to *summarise*.

Write an evidence note at turn end from data already on disk: the task, files
touched, tool calls that succeeded, tool calls that failed with their error
tail, duration. Type `context`, tag `evidence`, so `render_memory`'s existing
scoring loads it only when it bears on the request and it never becomes a
standing tax on the window.

Keep `memory_remember` as-is. Deliberate notes and automatic evidence are
different things, and their file says so.

---

<a name="m2"></a>
## M2 — `memory.db` is not the file to look for in simple mode · INFORMATIONAL

Recorded because it wasted time twice, once mine and once in the report that
opened M1.

SHAMSU has **two** memory subsystems, and simple mode uses only one:

| | store | written by | used by |
|---|---|---|---|
| typed notes | `.shamsu/memory/notes/*.md` + `index.json` | `simple_memory.py` | **simple mode** |
| SQLite service | `.shamsu/memory/memory.db` | `shamsu/memory/service.py` | legacy path only |

`grep` confirms simple mode never touches SQLite: no reference to
`MemoryService`, `SQLiteMemoryStore` or `memory.db` exists in `simple_chat.py`
or `simple_memory.py`. A `memory.db` in a workspace is a fossil of a legacy run,
and its **absence in a simple-mode workspace is correct, not a fault**.

So "no memory.db was created" is expected. The real symptom is the missing
`notes/` directory — see M1.

`status.json` is written regardless (memory service initialisation) and says
`"Graphiti disabled; local SQLite memory is authoritative"`, which is misleading
in a simple-mode workspace: the SQLite store is not authoritative there because
nothing in simple mode reads or writes it. Worth rewording.

---

<a name="g1"></a>
## G1 — the code graph holds 239 projects, and not this one · **HIGH**

```bash
codebase-memory-mcp cli index_status '{}'
→ {"error":"project not found or not indexed", ..., "count":239}
```

**239 indexed projects.** The ones visible in the listing are throwaway eval
scratchpad directories from 2026-07-23:

```
C-Users-...-Temp-claude-...-scratchpad-baseline_artifacts-eval_20260723_153126_...
  -ask_before_choosing_an_approach-sample_1
  -ask_before_choosing_an_approach-sample_2
  ...
```

**This repo is not indexed.** So every `graph_search` the model makes either
fails or resolves to something else — see G2 for what "something else" turned
out to be.

Root cause is architectural: the store is **global**, keyed by a mangled
absolute path, so every temp directory a test ever ran in accumulates forever
and nothing ever cleans up. Contrast smallcode's engine, which writes
`.code-graph/graph.db` **inside the project** — delete the folder, the index
goes with it, and cross-project contamination is structurally impossible.

**Fix:** purge the eval-scratch projects (`delete_project`), re-index this repo,
and add a cleanup path so temp-dir indexes cannot accumulate. Until then, treat
every graph answer as suspect.

---

<a name="g2"></a>
## G2 — the graph answers out of vendored third-party code · **HIGH**

Reproduced directly on this repo:

```
graph_search("message_tokens")
→ _estimate_emitted_message_tokens
   other peoples work/SmallCTL/src/smallctl/context/assembler.py
```

**Wrong function, wrong project.** The real `message_tokens` lives in
`shamsu/context/budget.py:145` and was missed entirely.

`.cbmignore`'s managed block excludes build and cache directories —
`node_modules/`, `.venv/`, `dist/`, `__pycache__/` and so on — but **not**:

* `reference/` — the smallcode clone
* `other peoples work/` — SmallCTL and others
* `legacy-code/` — the archived v1 tree

So the graph indexes other people's code and SHAMSU's own dead code, then
presents both as if they were current.

To its credit it *declared* the staleness (`"the code graph is out of date...
`/abstract refresh` rebuilds it"`), which smallcode's engine does not do. But an
honest wrong answer is still wrong, and this is the tool whose entire purpose is
to stop the model reading files and guessing.

**Fix:** add those three directories to `cbm_ignore_rules()` at
`shamsu/indexer/policy.py:232`; `ensure_cbm_ignore()` (line 238) regenerates
`.cbmignore` from it automatically. Then re-index.

---

<a name="l3"></a>
## L3 — tool schemas ARE the prompt · **HIGH**

Measured live, qwen2.5:3b-instruct, five turns:

```
turn   system  schemas  grounding  conversation  tool results   total
  1      308     2111        50           47            92      2,608
  2      308     2111        49          482           738      3,688
  5      308     2111        49           33            83      2,584
```

**Tool schemas are 57–81% of every prompt.** The conversation — everything §D's
elision machinery exists to protect — is 29 to 482 tokens.

`_elide_under_pressure` reports this correctly (`fattest() != "tool results"` →
*"eliding payloads will not help much here"*) and has nothing to offer instead.

Two-stage routing is the mechanism that fixes it, and it is **off**: it gates on
the context window (≤ 16k) and this runs at 32k. That gate is smallcode's, and
their reasoning holds for a full window — paying a round to save 2k out of 32k
is a bad trade. **It does not hold here**: the prompt is 2.6k, so the schemas are
not 6% of the window, they are 81% of the prompt.

Note this got worse as the roster grew 7 → 19. Schema cost is paid on **every
call** and it is the one bucket nothing currently reduces.

**Fix:** gate two-stage routing on the schemas' *share of the prompt*, not on
window size alone. See `shamsu/agents/simple_router.py`.

---

<a name="w1"></a>
## W1 — four stores for one turn, and a layout module that governs half of it · MEDIUM

Raised from a real comparison: *"they are not organized as we have — I like
their approach of minimal clutter on those files."*

### It is not file count

Measured on two comparable sessions on the same machine:

```
                        top-level entries   loose files   total state files
SHAMSU  .shamsu/                       13             5                  64
smallcode                               4             1                 112
  .smallcode/  sessions 3, snapshots 81, traces 15
  .memory/ 9   .code-graph/ 3
```

**smallcode writes almost twice as many files.** Their 81 snapshots alone
outnumber everything SHAMSU produced. So "minimal" is not about volume — it is
about whether a directory has a shape you can hold in your head.

One point in SHAMSU's favour, worth keeping: smallcode puts **three** dotted
directories in the project root (`.smallcode/`, `.memory/`, `.code-graph/`).
SHAMSU puts one. That part is already better here and should not be traded away.

### Where the clutter actually is

**1. Four directories record the same turn.** This session ran **two turns**
and produced 29 files across four stores, all keyed by the same session id
`20260819-074923-10d7`:

```
logs/<id>/agent-development-log.jsonl          1 file
chat-logs/<id>--make-me-3d-asteroid-game.md    2 files  (+ latest.md)
sessions/<id>/  events, messages, owner, report, session, state
runs/run_<ts>/.evidence/  context-preview, decisions, diagnostics,
                          events, final-output, manifest, ...   19 files
```

Three of those hold a rendering of *what the turn produced*
(`sessions/report.md`, `chat-logs/*.md`, `runs/*/.evidence/final-output.md`) and
three hold an *event stream* (`sessions/events.jsonl`,
`logs/*/agent-development-log.jsonl`, `runs/*/.evidence/events.jsonl`).

smallcode also has three stores — but they are three **different kinds of
thing**: `sessions/` is the conversation, `traces/` is tool execution,
`snapshots/` is file state for undo. No two of them tell the same story twice.
That is the difference worth copying, and it is a design difference, not a
tidiness one.

**2. Five loose files at the top level.**

```
SHAMSU      README.md  audit.jsonl  first-run-report.json
            model_tier.json  state.json
smallcode   tool_scores.json
```

Loose files are what make a directory unreadable, because nothing groups them
and nothing suggests where a sixth would go.

Note also that `audit/` is a *directory* in the documented layout while
`audit.jsonl` is a *file* at the top level — both concepts exist, and this
workspace only has the file.

**3. `paths.py` does not enforce `paths.py`.** The module opens by making
exactly this comparison against smallcode and states its own contract:

> *"A new location has to be added here to exist, which means the layout is
> reviewable in one diff."*

It is not true. Checked against what is on disk:

```
named in paths.py   cache config memory sessions runs audit plans mutations
                    trash abstract tools skills taskmaster documents
                    first-run-report.json
NOT named           chat-logs/  logs/  README.md  audit.jsonl
                    model_tier.json  state.json
```

**Six of the thirteen entries on disk bypass it**, including two whole
directories. So the file whose purpose is to make the layout reviewable does not
describe the layout, and nothing fails when a new path is added elsewhere.

This is the root cause of 1 and 2 rather than a separate issue: the module
documented an intent and never got the enforcement that would hold it.

### Fix, in the order that makes each next step safe

1. **Make `paths.py` true.** Add the six missing entries so it describes reality,
   then add a test that walks a real workspace after a run and fails on any
   top-level entry the module does not name. Without that test this regresses
   the next time somebody needs a file.
2. **Collapse the four record stores into three by kind**, matching smallcode's
   split rather than their file count: the conversation, tool execution, and
   recoverable file state. `chat-logs/` is a *rendering* of the session, not a
   separate store — it belongs under `sessions/<id>/`.
3. **Move the loose files into `config/` and `cache/`**, both of which exist and
   are already documented. `paths.py` has a `migrated()` helper for exactly this,
   so nothing is lost from existing workspaces.

Do **not** follow smallcode into three root directories. One `.shamsu/` is the
better call and is already ours.

---

<a name="w0"></a>
## W0 — neither project tracks workspaces globally · ANSWERED, NOT AN ISSUE

Checked because it was assumed the other way round: *"smallcode tracks all the
workspaces and their sessions at the home directory it was installed to."*

**It does not, and neither do we.**

| | smallcode | SHAMSU |
|---|---|---|
| sessions | `.smallcode/sessions/` **per project** | `.shamsu/sessions/` **per project** |
| home directory holds | config, skills, plugins, `mcp.json` | config, `bin/`, `cache/`, `tools/`, `runtime/` |
| global session registry | none | none |

`~/.smallcode` **does not exist on this machine at all** — their home usage is
`~/.config/smallcode/` for configuration and plugins, nothing per-session.

SHAMSU's `~/.shamsu/runtime/sessions/` looks like a registry and is not one: it
holds `<pid>.pid` files whose only job is deciding whether the exiting REPL is
the last one running, so Ollama can be unloaded. Dead entries are pruned by a
liveness check on read.

### The one thing that *is* global is ours, and it is the problem

The code graph. `codebase-memory-mcp` keeps a **single global store keyed by
mangled absolute path**, which is how it came to hold 239 projects including
every temp directory a test ever ran in — see [G1](#g1). smallcode's engine
writes `.code-graph/graph.db` inside the project, so it structurally cannot
accumulate that way.

So the intuition was right that something tracks every workspace from one place.
It is SHAMSU, it is only the graph, and it is unbounded.

### Also checked, and safe

`~/.shamsu/runtime/ollama-owner.json` holds `serve_pid 14152` from 2026-08-04
and **that process is dead**. The marker is what authorises SHAMSU to stop an
Ollama server it started, so a stale one pointing at a PID since reused would be
a real hazard. It is not: `runtime/ollama.py:237` checks `pid_alive(serve_pid)`
before acting. The file is never cleaned up when the PID dies — cosmetic only.

---

<a name="w2"></a>
## W2 — sessions accumulate forever · MEDIUM

Measured across the test workspaces:

```
sessions   workspace
     127   openbazaar-build      9.9 MB of sessions, 27 MB of .shamsu
       2   test1
       2   openbazaar-telegram-smoke
```

Oldest surviving session in that workspace is `20260802-213327-7f01` — **17 days
old and still on disk**. Nothing prunes.

smallcode caps it, in nine characters:

```js
const MAX_SESSIONS = 50;          // src/session/persistence.js
```

`shamsu/session/manager.py:1225` acknowledges the problem in a comment —
*"Disk growth is a retention problem"* — and there is no policy anywhere that
acts on it.

This compounds W1: a workspace already writes the same turn into four stores,
and then keeps every one of them indefinitely.

**Fix:** a session cap with pruning, the way theirs works. Prune oldest-first,
never touch the session currently open, and keep the append-only archive
guarantee intact for whatever survives — the archive is the thing
`history_search` reaches across, so the cap has to be a deliberate number rather
than a side effect.

---

<a name="w3"></a>
## W3 — session files are written with default permissions · LOW

smallcode sets mode `0o600` on its session directory and files, with the reason
in the line above it:

```js
// File mode 0o600: owner read/write only — sessions contain conversation
```

SHAMSU's session code contains no `chmod` and no mode argument. Its sessions
hold the same thing: full conversation content, file contents the model read,
and command output.

**Honest scope:** on Windows, where this was measured, POSIX modes are largely
inert and NTFS ACLs do the real work — so this is close to a no-op on the
machine in question. It matters on a shared POSIX host, and on any machine where
a workspace lives somewhere world-readable. Low severity, cheap fix, worth doing
when W2 is done since both live in the same module.

---

<a name="g3"></a>
## G3 — graph answers are truncated, not ranked · MEDIUM

`shamsu/agents/simple_graph.py:114`:

```python
for line in lines:                       # arrival order
    if used + cost > MAX_GRAPH_TOKENS:   # 1500
        kept.append(f"... [{len(lines) - len(kept)} more, ask something narrower]")
        break
```

That returns the **first** 1,500 tokens. Given L3 — where the whole prompt is
2.6k — it should return the **best** 1,500.

smallcode's engine does this properly: `graph_walk` starts at the anchor symbol,
walks outward along real call/import edges, and stops when the budget is hit, so
what survives is the most structurally connected code rather than whatever the
backend happened to emit first.

**Fix (cheap):** rank by connectivity before capping in `_capped`. This captures
most of the benefit without changing engines.

**Not recommended (yet):** swapping to `budget-aware-mcp`. It is the better
engine *for feeding a small model* — token budget on every query, per-project
storage, `check_scope` — but `codebase-memory-mcp` has `ingest_traces` and
`manage_adr` with no equivalent, and SHAMSU's adapter, `/abstract`, health checks
and tests are all built on it. Benchmark both on the same five questions before
touching this. Their indexer speed numbers are from their README, **not measured
here**.

---

<a name="l2"></a>
## L2 — the token estimate runs 15% heavy · MEDIUM

`prompt_eval_count / our raw estimate`, live, with the correction factor chasing it:

```
turn      1       2       3       4       5
drift   0.848   0.879   0.857   0.852   0.848
factor  0.944   0.879   0.861   0.856   0.853
```

**The calibration machinery is correct** — it found the true ratio in four turns,
exactly as item A intended. What is wrong is the *uncalibrated* counter beneath it.

Fails in the safe direction (we over-count, so the budget under-fills rather than
overflows), but 15% of a 32k window is **~4,900 tokens left unused**.

**Suspects, in order:** the vendored Qwen3 tokenizer counting a Qwen2.5 prompt;
`PER_MESSAGE_OVERHEAD` (8); the tool-schema estimate — and per L3 the schema
block is large enough that a small per-schema error would show up as exactly
this.

**Do not** tune a constant to 0.85. That fits one model, and the calibration
factor already handles per-model drift by design. Find which counter is wrong.

---

<a name="l4"></a>
## L4 — the small model thrashes, and no guard notices · MEDIUM

| turn | asked | rounds | tools | time |
|---|---|---|---|---|
| 1 | remember a fact | 2 | 1 | 132.3s |
| 2 | create a file that prints `hello` | **8** | **4** | **292.4s** |
| 3 | change it to print `goodbye` | 5 | 4 | 13.3s |
| 4 | run it, report output | 2 | 1 | 3.7s |
| 5 | recall the port | 2 | 1 | 1.4s |

Eight rounds and four tool calls for a one-line file.

**No guard fired, and each was right not to.** `MAX_UNPRODUCTIVE_EDITS` counts
edits that change nothing; `EDITS_PER_FILE_BEFORE_STOPPING` counts successful
edits to one file; `REPEATED_READS_BEFORE_WARNING` counts *identical* reads. This
model did none of those — it made progress every round, just very little of it.

Related to T1.

---

<a name="l6"></a>
## L6 — a second process pins the GPU and nothing says so · MEDIUM

Cost two live runs before diagnosis. A separate SHAMSU process held
`qwen3.5:9b-q4_K_M` (6.2 GB) resident on an 8 GB card. The 3B model could not get
VRAM, spilled, and **every call ran the full 600s timeout** with no indication why.
Indistinguishable from a hung model.

Diagnosis is one HTTP call:

```bash
curl -s http://localhost:11434/api/ps
```

Workaround: `SHAMSU_CHAT_MAX_CTX=8192` made it usable (20–65s/call) even with the
other model resident.

**Fix:** `/doctor` already checks Ollama is up. It could also report what is
*resident* and whether the requested model fits in what is left. Converts a
ten-minute mystery into one line. See also `OLLAMA_KV_CACHE_TYPE=q8_0`, which is
what makes 32k affordable at all.

---

<a name="s1"></a>
## S1 — no clean full-suite run has completed · MEDIUM

Last **complete** run: `1 failed, 2838 passed, 2 skipped in 3012.01s (50:12)`.

That failure was an **artifact**: it named `test_simple_mode_asks_for_the_wide_horizon`
but its traceback showed source from a different test, because the run overlapped
edits to `tests/test_simple_chat.py`. The named test passes in isolation.

A clean confirming run was started and **interrupted** (the GPU was needed). So
the true number is unconfirmed.

**Fix:** run `pytest tests/ -q` once, uninterrupted, with no editing. Budget ~50
minutes. **Do not edit test files while a suite runs** — the failure it invents
costs more than the wait.

---

<a name="t1"></a>
## T1 — no read-loop detector · MEDIUM

smallcode `src/governor/early_stop.js` tracks `_readOnlyStreak`: N read-only
calls with **no output produced yet**, soft nudge at 5, hard stop at 8.

> *"This is the 'endless review' failure mode: the model keeps gathering context
> because 'review X' has no clear terminal state."*

SHAMSU counts **identical** repeated reads (`REPEATED_READS_BEFORE_WARNING = 3`).
That is a different thing. A model that reads eight *different* files and never
writes anything is invisible to every guard we have — and SHAMSU has that exact
failure on record: 24 rounds / 577s / no plan, 2026-08-18.

---

<a name="t2"></a>
## T2 — no greeting-regression detector · LOW

smallcode watches for "how can I help", "what would you like" *mid-task* — the
model lost context and restarted the conversation. Given SHAMSU's documented
memory-horizon history, this is a cheap high-signal probe. ~15 lines.

---

<a name="t3"></a>
## T3 — item G only half-landed · LOW

Item G in the adoption plan had three parts. One shipped:

| part | state |
|---|---|
| expand `@file` before the model sees it | **done** (`1318018`) |
| inject a git diff when the message implies recent changes | **not done** |
| screen vague requests with a zero-token regex classifier | **not done** |

Both missing pieces exist in smallcode as small, self-contained files —
`src/session/git_context.js` (75 lines) and `src/session/clarify.js` (63 lines).
The clarifier is notable for what it refuses to fire on: greetings,
confirmations, `"go ahead"`, `"1 and 2"` — the list of false positives is longer
than the list of triggers.

---

<a name="r1"></a>
## R1 — `repl.py` is 19,041 lines · MEDIUM

Item 14 of `CONTEXT_AND_TRUNCATION_PLAN.md`, and the only one still open there.
451 functions. The CLI imports **271 of 292 modules** because of it, which gates
~47,000 lines of legacy removal. It **grew** during this branch (18,780 → 19,041).

---

<a name="l7"></a>
## L7 — what has never been exercised live · UNVERIFIED

Every turn of the only live run sat far inside the window:
`compactions=0`, `elisions=0`, `truncations=0`.

* **§D payload elision** — never triggered. Synthetic-only.
* **§F bucket eviction** — never triggered. Synthetic-only.
* **§B truncation handling** — `done_reason` was `stop` every time; the `length`
  path never ran.
* **The `think=True` path** — a reasoning-model run was started and interrupted.
  L1 proves we no longer *send* `think` to a 3B model; it does not prove the
  thinking path works.

**Next runs, in order:**

1. `qwen3.5:9b-q4_K_M` (the out-of-box default, a reasoning model) — same five
   turns. `scripts/live_run.py` now records every prompt sent and every thinking
   block received, and asserts no thought is ever replayed as conversation.
2. A **30+ turn** session on any model — the only way to reach compaction,
   elision and eviction, which is where §D and §F actually live.

---

# FIXED

<a name="l1"></a>
## L1 — `think=` to a model that cannot think · **CRITICAL** · `c5486ef`

```
ResponseError: "qwen2.5:3b-instruct" does not support thinking (status code: 400)
```

**All five turns of the first live run, dead before a token was generated.**
Ollama rejects `think=` outright for a model with no reasoning channel; simple
mode sent it unconditionally.

Blast radius: **every non-reasoning model was unusable** — qwen2.5:3b-instruct,
qwen2.5-coder:7b-instruct (the 8GB default), qwen2.5-coder:14b, mistral-nemo:12b,
gemma3:4b. Only the qwen3 / deepseek-r1 family worked.

The cookbook had `is_reasoning=False` recorded for all of them and
`runtime/models.py` exposed `model_is_reasoning()` for exactly this. Simple mode
never asked. Two questions existed; only the second was being asked:

```
can this model think at all    -> the cookbook, now consulted   (_should_think)
should it think on THIS call   -> unchanged        (_should_disable_thinking)
```

Unknown models default to **False**, because the failure is asymmetric: a
reasoning model asked not to think still answers; a plain model asked to think
returns 400 and nothing.

Guard proved by removal: `assert True is False` on two tests.

> **Why this file exists.** 2,838 unit tests pass. Not one could catch this,
> because the scripted test client accepts every keyword the real server rejects.
> **A fake more permissive than the real thing will certify a harness that cannot
> make a single call.**

---

<a name="f1"></a>
## F1 — summary dropped when hydration skipped what it described · `8b338d5`

`include_summary` asked *"did we evict anything THIS turn"*, which misses the case
the summary exists for. Long thread resumed: hydration loads the last 400 records
and skips the rest, those 400 fit the budget, nothing is evicted, `start_abs` is 1
— and the summary is dropped, though it was the only trace of everything hydration
never loaded.

Reproduced: 101 messages unloaded, the founding decision in the summary, and
neither the decision nor the summary anywhere in the prompt.

---

<a name="f2"></a>
## F2 — the meter reported more tool results than the whole prompt · `8b338d5`

`token_allocation` re-derived the prompt instead of measuring the one sent, and
reported **42,440 tokens of tool results inside a 23,595-token prompt**. Re-running
the selection was closer but still ~900 off, because `_messages` updates the
rolling summary while it builds, so the second run sees different state.

Now classifies the assembled message list. A meter that overstates is worse than
no meter, because it gets believed.

---

<a name="f3"></a>
## F3 — 11 ruff errors while the docs claimed lint passed · `152fbb0`

Unused imports, a redefinition, an ambiguous name, a dead local. All removals but
one rename: a `for contract in` loop in `repl.py` shadowed the
`shamsu.verify.contract` module imported at the top of the same file — harmless
today, a bug the moment someone calls `contract.derive()` inside the loop.

`CURRENT_STATE.md` had been asserting "Lint: passes" throughout.

---

<a name="f4"></a>
## F4 — `/context meter` was undiscoverable · `152fbb0`

Worked and tab-completed since it shipped, but appeared in neither `/help` nor the
usage line, both of which read `status|budget|inspect|compact|show`. The meter
exists so a silent context bug becomes something you look at; one nobody can find
does not do that.

---

<a name="f5"></a>
## F5 — Ollama keeps 4 tokens on overflow · `5a55e6d`

Item 16 of the context plan. If a prompt overflows, Ollama shifts context from the
front and keeps **four tokens** — so the first casualty is the system prompt, and
the model continues with no idea what it is or which tools it has.

`num_keep` is now sized to the system prompt (297 tokens) and clamped to
`num_ctx // 8` so a long prompt cannot starve the window it protects.

The budget is meant to make overflow impossible; this is the floor under an
estimate that was wrong by 9,500 tokens this week. Guard proved by removal:
`assert 4 >= 297`.

Confirmed live: `num_keep=308` on all five turns.

---

<a name="f6"></a>
## F6 — the prose-nudge told a plan to stop planning · `049204f`

Item 15. `describes_an_unmade_edit` reads prose + a code block + a real filename
as "answered the question, skipped the job". Asked to *"review and plan"* that is
backwards — describing the change **is** the deliverable, and the nudge told the
model to abandon it and start writing. Same presumption that cost 24 rounds, 577s
and five unwanted files on 2026-08-18.

`asks_only_for_words` gates it: a words-verb with **no** change-verb anywhere.
Word boundaries, because `_PRD_BUILD_NOUNS` once held `"it"` as a raw substring.

Deliberately asymmetric — *"review it and fix the bug"* still nudges. Skipping the
nudge wrongly means work silently never happens; nudging wrongly costs one round.
Guard proved by removal: planning goes 1 round → 2.

---

<a name="f7"></a>
## F7 — three docs described a state untrue for weeks · `c4dcad3`

* `SMALLCODE_ADOPTION_PLAN.md` said **"PLAN ONLY. No harness code changed."**
  after all eight items had landed — and still listed two-stage routing under
  "Explicitly NOT adopting" after it was adopted in `1d0444a`.
* `SMALLCODE_TOOL_COMPARISON.md` carried the same stale row.
* `CURRENT_STATE.md` described 2026-07-21 while presenting itself as the file to
  trust — claiming 1,449 tests, 202 modules, and passing lint, all wrong.

Items 15 and 16 moved out of the context plan's open list. Added
`SMALL_MODEL_LIVE_RUNS.md` and `scripts/live_run.py`.
