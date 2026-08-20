# SHAMSU vs SmallCode — agent roles, chat-history/web-portal gaps, and live corrections

**Date:** 2026-08-20
**Scope:** this document covers what [`SMALLCODE_GAP_ANALYSIS.md`](SMALLCODE_GAP_ANALYSIS.md) (same day, earlier) does **not**:
persona/team agent structure, and session persistence / web-portal behavior. It also folds in
corrections given live by the user after reading a draft of this analysis, plus a real failure
trace pasted from an actual SHAMSU session that lands squarely on a gap the other document already
named. Analysis only — nothing here has been coded yet.

Where this document's findings overlap the other doc's "Issue 2" (patch-spiral / no third option),
that's treated as **confirmation**, not new content — see §4.

---

## 0. Corrections from live testing (read this first)

Three claims in the first draft of this analysis were wrong or incomplete. Recorded here so they
don't get re-asserted later.

1. **"smallcode has no web view" was too strong.** It has no *self-hosted* web server (verified —
   no `express`/`http.createServer`/`ws` usage anywhere in `src/` or `bin/`; `express` sits unused
   in `package.json`). What it *does* have is `src/session/share.js::exportToGist()` — one command
   exports a session to Markdown and publishes it via the `gh` CLI to a **GitHub Gist**, returning a
   URL you open in a browser. That's the "browser view with a link they share" — it's a link to
   *GitHub's* renderer, not a page smallcode itself serves. Cheap to imitate (no server needed), not
   proof that smallcode solved session-viewing better than SHAMSU's approach — it sidesteps the
   problem by not hosting a viewer at all.

2. **SHAMSU's web portal is broken beyond the two root causes below.** The earlier research
   identified two specific bugs (workspace registration, non-persistent thread) as sufficient
   explanation. User report from actual use: it's "not ready at all, many errors" — i.e. broader
   than those two causes. Treat the two root causes below as **confirmed real and worth fixing
   regardless**, but not a complete account of "why the web UI doesn't work." The web portal needs
   its own dedicated audit pass (live-driving `shamsu web` against a real workspace, reading actual
   errors) before anyone claims a fix. Not attempted in this session.

3. **Chat history "not saved properly" is unresolved, not fine.** The earlier research read a real
   on-disk session (`messages.jsonl`, `activity.jsonl` for workspace `live-player`) and found it
   complete and well-formed, and concluded storage itself works. The user directly disputes this
   from lived experience. Both can be true in different ways — e.g. it could look fine on disk but
   fail specifically when *resumed*, or fail only in some code path (headless vs. interactive vs.
   Telegram), or the specific session inspected was a lucky case. **Do not trust either claim without
   a fresh, targeted repro**: start a session, exit uncleanly (Ctrl-C, kill, crash), reopen, and see
   what's actually recoverable — including via `/sessions` in the CLI, not just by eyeballing the
   JSONL files. This is the highest-priority open question in this whole document.

---

## 1. Gap: no persona / team agent system (confirmed absent, not previously documented)

SmallCode ships 11 role-specific agent prompts under `agents/*.md` — `code-engineer`, `critic`,
`debugger`, `documenter`, `general-purpose`, `librarian`, `oracle`, `planner`, `qa-tester`,
`red-team`, `scout`. Each is:

- YAML frontmatter: `name`, `description`, `model` (tier: fast/default/medium/strong), `tools`
  (a whitelist).
- A Markdown body, hard-capped at 1600 chars at load time (`BODY_CAP`,
  `src/plugins/agent_runner.js`), several using a strict fenced "Output Format" contract (e.g.
  `critic.md` must emit `VERDICT: OKAY / REJECT`; `red-team.md` must emit severity + file:line +
  repro).
- Run in an **isolated, bounded sub-loop** (`AgentRunner.run`): fresh history (parent conversation
  is *not* passed in), tool set narrowed to the frontmatter whitelist, `MAX_STEPS = 15`, designed to
  never throw.

Teams (`teams/*.yaml`, 3 lines each) chain agents **strictly sequentially** — agent A's full text
output becomes agent B's input task, no shared state, no parallelism (explicit design choice, the
file's own comment calls parallel local inference "a performance trap"). Two entry points: a
model-callable `spawn_agent` tool, and user commands `/agent <name> <task>` / `/team <name> <task>`.

**SHAMSU has none of this in the default path.** `SimpleChatLoop` (`agents/simple_chat.py`) is one
prompt, one tool roster, one model, no delegation. The legacy `orchestrator.py`/`planner.py`
(`SHAMSU_LEGACY_ROUTING=1`, off by default) is a different shape entirely — a persistent
router → planner → phase-gated task-object state machine, not a persona library or agent pipeline.

**Why this might matter concretely:** the live failure trace in §4 is exactly the kind of task
(`critic`/`debugger`-shaped: "find and fix the syntax error") that smallcode would hand to a
narrow, single-purpose persona with a tight tool set and an output-format contract, rather than
running through the same generalist loop and prompt used for everything else. Worth weighing
whether a `debugger`-style persona (read-only diagnosis first, fenced
`SYMPTOM/EVIDENCE/ROOT CAUSE/FIX/VERIFICATION` output, *then* handed to the normal edit loop) would
have avoided the blind patch-guessing seen in §4 — separate from the recovery-path fixes already
planned in the other document.

**Porting cost is low relative to value**: it's markdown files + a loader + a narrowed-tool
sub-loop, not a rewrite of `SimpleChatLoop`. Does not conflict with anything in
`SMALLCODE_GAP_ANALYSIS.md`'s Tier 0–3 plan; could be added as a new tier there, or tracked
separately.

---

## 2. Gap: session / web-portal (confirmed absent, not previously documented)

*(See §0.2 and §0.3 for corrections — the summary below is the mechanism, not a claim it's
sufficient.)*

**Storage mechanism** (from the earlier pass, still believed accurate as far as it goes):
workspace-local `<workspace>/.shamsu/sessions/<id>/`, managed by `SessionLogger`
(`shamsu/session/manager.py`) — `messages.jsonl` (append-only, one record per message),
`activity.jsonl` (turn-stream telemetry), `session.json` (metadata + rolling summary),
`events.jsonl`, `state.json`, `owner.json` (live-process lease).

**Two specific, verified-in-code root causes for "invisible in the web UI":**

1. `shamsu/cli/noninteractive.py` — the headless `shamsu run --prompt ...` path — never calls
   `remember_workspace()`. Only the interactive REPL and the `/web` command register a workspace
   into `~/.shamsu/workspaces.json`, which is the *only* list the web portal's sidebar reads from.
   Confirmed on this machine: a workspace (`live-player`) with a complete, well-formed session on
   disk from the same day was absent from that registry.

2. The in-REPL `/web` command (`webui/local.py`) runs as **a daemon thread inside the current REPL
   process** — its own module docstring calls this "a workaround, not a design," done only because
   run-state is a module-level dict. It dies the instant the CLI exits; its access token is printed
   once and never persisted anywhere retrievable. A separate, correctly-built standalone command
   (`shamsu web`, `webui/cli.py::serve()`) reads from disk and survives process boundaries — but
   nothing starts it automatically, and per §0.2, "many errors" suggests it has its own unresolved
   problems beyond just needing to be started manually.

**smallcode has no equivalent to compare against** — per §0.1, it has no web portal at all, only a
Gist-export one-shot share and a CLI `/sessions` list. So this is not a "port X from smallcode" gap;
it's a SHAMSU-specific defect that needs its own investigation, informed by §0.2/§0.3 rather than
closed on the strength of this analysis.

---

## 3. What NOT to re-derive: patch-spiral / recovery-path gap already fully documented

`SMALLCODE_GAP_ANALYSIS.md` §2 ("Issue 2") and §3.3 (items 1–4, 8, 9) already give a complete,
code-cited account of exactly the loop-health gaps this session's research surfaced independently:
context-aware read guard, semantic-merge-on-patch-failure, patch-spiral → forced rewrite, decompose
task, quality monitor, per-tool trust decay. Its Tier 0 plan (items 1–8) already targets this
directly. Don't re-plan this — read that document's §2–§5 instead. §4 below is new evidence for
that existing plan, not a new plan.

---

## 4. Live evidence: a real syntax-fix session hit exactly this gap

User-provided transcript, task: *"review the files and fix the syntax errors... some functions
where a closing `}` or `;` is missing."* Two files attempted, `js/main.js` (343 lines) and
`js/GameState.js` (lines 64–156 specifically called out). Both runs ended the same way:

```
read_file js/main.js          → "343 lines; sent its outline instead of the body"
read_file js/main.js          → (repeated)
read_file js/main.js          → (repeated) — "context is filling; eliding older tool payloads"
read_file js/main.js
patch_file js/main.js
read_file js/main.js          → "context is filling; eliding older tool payloads"
read_file js/main.js          → repeated identical read, asked to move on
patch_file js/main.js
patch_file js/main.js         → "context is filling; eliding older tool payloads"
patch_file js/main.js
→ "I tried 4 edits in a row that changed nothing... I have stopped rather than keep guessing.
   It would help to tell me the exact text to look for."
```

`GameState.js` (explicit line range 64–156 given by the user) produced the identical pattern and
identical stop message in fewer steps.

This is a **textbook instance of the already-documented "Issue 2" dead-end chain** (read cap → patch
miss → retry → no third option → stop), and it adds two things the existing document didn't have:

**New observation A — outline-vs-body is a trap specifically for syntax-repair tasks.**
`SMALLCODE_GAP_ANALYSIS.md` frames Link 1 as a *size* problem (fixed 24,000-char cut, not
context-aware). The trace above shows a *file that isn't even that large* (343 lines) still getting
outlined instead of body-read. That's consistent with memory
[`shamsu-read-path-and-smallcode-tools.md`]'s "outline-first reads" design — which likely assumes
the file **parses**, so it can produce a symbol/outline view cheaply. A file with a genuine syntax
error (unbalanced `}`, missing `;`) may not parse cleanly, which could be pushing the outline path to
degrade or fall back in a way that still reports "outline instead of body" rather than falling
through to a raw read. If so, this is a second, independent reason the read layer failed here, on
top of the already-known size-cap issue — worth checking whether `read_file`'s outline path has a
parse-failure fallback to raw content, specifically for this task shape. Not confirmed against code
in this pass — flagged as a targeted follow-up, not a verified root cause.

**New observation B — CONFIRMED. Two known bugs compounded this failure, not one.**
User has since confirmed and fully diagnosed the mechanism (see §6.3 for the full writeup):
`_shared_console_approval.ask()` calls `asyncio.run()` from inside a thread that already has an
event loop running, which throws on every call — the "coroutine never awaited" warnings in the
user's own transcript are direct proof — and silently falls back to a path that hardcodes
`offer_remember=False`. Pressing `a` ("always allow") then doesn't match any recognized answer and
is treated as unrecognized input, which defaults to **deny**. Nothing is ever persisted, so it
repeats on every single call. In the session this document analyzes, this denied 20 of 22
`node --check` verification calls even after the user explicitly chose "always allow" every time.
So: the syntax-fix failure in this section was not purely a read/patch dead-end — the model's
attempts at deterministic verification were being silently thrown away by a completely separate,
already-known bug, on top of the read/outline and patch-spiral issues. See §5 for the other two
confirmed bugs from the same report.

**Net:** no new gap category here — this is a real, live confirmation that
`SMALLCODE_GAP_ANALYSIS.md`'s Tier 0 items (especially #5 context-aware read guard and #7
patch-spiral escalation) are not hypothetical, plus two specific follow-up questions worth
answering before or during that work.

---

## 5. Confirmed bugs (user-supplied, 2026-08-20) — three separate root causes, not one

These were diagnosed directly by the user from a live transcript, after the analysis above was
written. All three are precise enough to act on; none has been coded yet.

### 6.1 Compaction never runs mid-turn

Two mechanisms both get called "compaction," only one can run mid-turn:

- `_elide_under_pressure()` (`simple_chat.py:2876`) — cheap, in-memory, no model call. Runs every
  `ELIDE_EVERY_N_TOOL_CALLS` tool calls *inside* a turn — this is what prints `"context is filling;
  eliding older tool payloads"`. Its own docstring admits it "reclaims almost nothing" once the fat
  bucket is conversation/tool_calls rather than raw tool-result text.
- `_compact_if_needed()` (`simple_chat.py:2519`) — the real thing: an actual LLM call (`_narrate`)
  that summarizes evicted turns and persists via `ChatState.update_rolling_summary` →
  `session_logger.save_summary`. Called from `simple_chat.py:1935`, **once, at the very start of
  handling a new user message, before the round loop begins** — it does not run again until the
  next user prompt.

So a long multi-round turn (the `js/main.js` write/patch session in §4 is exactly this shape — the
same `run_command` ran 22 times without a single `_compact_if_needed()` firing mid-turn) only ever
gets the cheap elide for its whole duration, no matter how full the window gets.

`/compact` cannot help here — it was never a trigger. `_handle_compact` (`repl.py:18494`) only
*displays* the persisted summary or clears it (`/compact clear`); there is no code path that lets a
user force a real compaction pass on demand mid-stall.

Full mechanism, fix sketch, and related links: [`shamsu-elide-vs-compact-mid-turn.md`] (project
memory).

### 6.2 The 8000-char write cap's chunked-write fallback is known-incomplete

`MAX_WRITE_CHARS` / `max_write_chars()` + `write_budget_is_unworkable()` (`simple_chat.py`, shipped
2026-08-20, `SMALLCODE_GAP_ANALYSIS.md` §6.8 items 1–7) is working as designed — a deliberate,
tested clamp, not a bug; suite is 3218 pass / 2 skip. `_refuse_oversized_write` /
`_refuse_cut_off_content` sit next to `_refuse_truncated_write`, wired in `_run_tools` before
dispatch. `_verify` now has four outcomes (checked / skipped / **unfinished** / problems), with
`_settle_unfinished()` failing an unfinished file at turn end so partial progress can't pass as done.

Known-incomplete from that same shipping session, per live runs on `qwen2.5:3b-instruct`: the
repeated-edit ceiling read a legitimate chunked BUILD as a repair loop — the model chunked exactly
as instructed but carried each section with `write_file` instead of `append_file`, and five
verified-clean growing writes in a row triggered "5 blind edits I cannot confirm," stopping the
turn. Partially addressed by `_extended_the_file` (net-line-growth exemption), but the acceptance
bar (6–18 successful calls reaching 1,500 lines) is still only 2 of 3 met on a 3B — it can stop to
ask a clarifying question or narrate the file back in prose instead of calling the tool, when
neither was needed. This is a narrower, more specific instance of the general "no third option"
pattern in `SMALLCODE_GAP_ANALYSIS.md` §2 — the cap itself is sound, the recovery path off of it is
not, and hasn't yet been validated on a 7B.

Full mechanism and fix history: [`shamsu-write-cap-implementation.md`] (project memory).

### 6.3 The real mechanism behind the approval "always allow" bug

Traced through `shamsu/safety/approval.py` + `shamsu/control/console.py` + `shamsu/cli/repl.py`:

1. `_shared_console_approval.ask()` (`repl.py:4921`) tries the shared/ControlStore async path
   first: `asyncio.run(ask_here_or_anywhere(...))`.
2. `asyncio.run()` cannot be called from a thread that already has a running event loop — and
   Simple Mode's tool-call loop *is* an async loop running in this thread. This throws immediately.
   The transcript's own two `RuntimeWarning: coroutine ... was never awaited` (at `approval.py:252`
   and `repl.py:4961`) are the fingerprint that this is happening live, not theoretical.
3. The exception is swallowed by a broad `except Exception:` and falls back to
   `ask_approval(request, console=console)`, which **hardcodes `offer_remember=False`**.
4. The low-level key reader `_read_windows_console_answer` (`approval.py:303`) still always prints
   "a to always allow" regardless of that flag — advertising an option the fallback path doesn't
   support.
5. In `ask_approval_menu` (`approval.py:142-147`): with `offer_remember=False`, answer `"a"` doesn't
   match `{"y","yes"}`, so it falls through to "anything unrecognized is treated as 'no' — the safe
   default" → **the "always allow" keypress becomes an active DENY**, not a no-op.
6. Nothing is ever persisted (no remember-scope write happens), so every subsequent identical
   command re-enters the same broken chain and gets denied again, for the rest of the session.

Concretely, in the session behind §4's transcript, this denied **20 of 22 `node --check`**
verification calls, even though the user chose "always allow" every time it was offered. Combined
with §4's "New observation B" (now confirmed, not speculative): the model's attempts at
deterministic syntax verification for the `main.js`/`GameState.js` task were being thrown away by
this bug, on top of the read/outline and patch-spiral issues already documented.

**Scoping note**: this is a **different code path** than the approval fix already shipped on
2026-08-08/09 ([`shamsu-plan-mode-and-approval.md`]), which fixed `WAIT_APPROVAL` / the `Approver`
Protocol in the **legacy orchestrator's** `runtime/session.py`. Simple Mode's `run_command` uses
`shamsu/safety/approval.py` + `shamsu/control/console.py` instead, which that prior fix never
touched. Don't assume the 08-08/09 fix covers this — it doesn't, and re-testing against the legacy
path would give a false all-clear.

Full mechanism, cosmetic double-panel symptom, and fix sketch:
[`shamsu-approval-always-allow-bug.md`] (project memory).

---

## 6. Open questions to resolve before coding (do not skip)

1. Reproduce the chat-history-not-saved complaint directly — pick a session, kill the process
   uncleanly, check what `/sessions` and the raw JSONL files show afterward. Don't fix storage code
   based on this document; fix it based on a fresh repro.
2. Live-drive `shamsu web` (the standalone command, not `/web`) against a real workspace and collect
   the actual errors — §0.2 says it's broadly broken; find out concretely how before deciding
   whether the two root causes in §2 are the main fix or a side item.
3. Check whether a syntax-check tool call was attempted and silently denied in the pasted
   transcript's underlying log (§4, New observation B) — quick to check, materially changes how
   urgent the approval-bug fix is relative to the read/patch fixes already planned.
4. Decide whether the persona/team system (§1) is worth building now, or whether it's better
   sequenced after the Tier 0 recovery-path fixes in `SMALLCODE_GAP_ANALYSIS.md` land, since a
   `debugger` persona would just inherit the same broken read/patch primitives until those are
   fixed.

---

## Appendix A — full SmallCode research agent report (verbatim)

Raw output of the research agent tasked with mapping smallcode's master prompts, agent roles, agent
loop, and session persistence. §§1–4 above are a synthesis and gap comparison built from this; this
appendix is the primary source material, kept for reference so nothing gets lost in the summary.

> Investigated at `F:\Work\PROJECTS\shamsu\Shamsu\reference\smallcode` (Node.js, package
> `smallcode` v1.6.0, entry `bin/smallcode.js`, `main`: `src/api/index.js`). Docs read first:
> `ARCHITECTURE.md`, `COMPARISON.md`, `README.md`; claims below were checked against actual source.
> One overarching finding up front: **the docs overclaim in at least two places** — see §4.

### 1. Master Prompts / System Prompts

SmallCode has two distinct prompt systems: a **compact main-loop system prompt** built at runtime,
and **11 static sub-agent persona files** under `agents/`.

**1a. Main agent system prompt — `bin/smallcode.js`, function `buildCompactSystemPrompt` (lines
2060-2116)**

This is the prompt sent for the primary, non-delegated agent loop. It's deliberately tiny and
conditional — built fresh per turn, but only including sections relevant to `taskType`, to save
tokens for small context windows. Core always-included text (lines 2080-2085):

```
You are SmallCode, a coding agent. Working directory: <cwd>
OS: <os><osHint>

Rules: Use patch for edits (not full rewrites). Prefer compound tools. Be concise.
ACT immediately — do not ask for confirmation unless the task is genuinely ambiguous...

CRITICAL — large file rule: write_file calls are limited to 60 lines / ~8KB.
llama.cpp's JSON parser crashes on larger tool calls. For any file over 60 lines:
(1) write_file with just the skeleton..., then (2) use multiple patch calls...
```

Conditionally appended: a tool-use hint (graph_search/explain_symbol/list_projects) unless
`taskType === 'explanation'`; a BoneScript hint for `taskType === 'backend'`; a web-research block
gated on `SMALLCODE_WEB_BROWSE=true`.

There's an important architectural technique here called **cache-split** (`SMALLCODE_CACHE_SPLIT`,
default true): the system prompt is kept **stable across turns** (identity/OS/rules/plan/plugin
text only) so llama.cpp's KV cache isn't invalidated; all *dynamic* content (memory recall, skills,
knowledge snippets, RAG hits) is moved into a separate `<sc:context>...</sc:context>` block injected
into the latest **user** message instead of the system message (`buildDynamicContext`, lines
2121-2141). This is explicitly commented as a fix for "erased invalidated context checkpoint" loops
in llama.cpp.

A second, older/simpler system prompt exists for the no-tools streaming path, `sendToModel` (line
2861):
```
You are SmallCode, a coding assistant. You help users by reading, editing, and creating code files.
Rules:
- Read files before editing them.
- Use search-and-replace for edits. Never rewrite entire files.
- Keep responses concise and focused.
- If a task is complex, break it into steps.
```

Structural technique: no XML tags in the main prompt (aside from the `<sc:context>` wrapper), no
few-shot examples, no numbered rule lists — it's short prose with a couple of hard constraints (the
60-line write cap is the most load-bearing rule, directly tied to a known llama.cpp crash).

**1b. Sub-agent persona prompts — `agents/*.md`**

Eleven files, each 21-33 lines, YAML frontmatter + Markdown body. Frontmatter declares `name`,
`description`, `model` (tier: `fast`/`default`/`medium`/`strong`), and `tools` (a whitelist array).
At runtime the body is hard-capped to **1600 chars** (`BODY_CAP` in `src/plugins/agent_runner.js`)
and a tool-list line is appended, targeting ≤600 tokens total (`buildSubAgentPrompt`,
agent_runner.js:52-59).

Roles found, with structural technique per file:

| Agent | Role | Notable structure |
|---|---|---|
| `code-engineer.md` | Primary implementer | "Operating Principles" bullets + "Code Quality Non-Negotiables" + numbered 5-step Workflow + "When to Escalate" delegation map |
| `critic.md` | Post-implementation gate, read-only + run checks | Explicit fenced **Output Format** template with `VERDICT: OKAY / REJECT`; "Rejection Triggers" list; instructs "Never approve with reservations" |
| `debugger.md` | Root-cause diagnosis | Scientific-method numbered steps (reproduce→hypothesize→test→fix→verify); fenced Output Format: `SYMPTOM/EVIDENCE/ROOT CAUSE/FIX/VERIFICATION` |
| `documenter.md` | Docs writer | "Style Rules" enforcing tone-matching; explicit "no placeholder text" ban |
| `general-purpose.md` | Catch-all / content authoring | Explicitly told to treat a named `prompts/` file or spec as authoritative and follow it "exactly" |
| `librarian.md` | External docs/library research | Numbered workflow + explicit **Stop Conditions** (2 independent sources confirm, or 2 failed search iterations) — a rare example of a defined loop-termination rule for a sub-agent |
| `oracle.md` | Read-only architecture advisor, high-reasoning tier | Explicit invocation triggers ("after 2+ failed attempts by other agents"); fenced Output Format (Summary/Analysis/Recommendation/Risks/Next steps); ends with "You are READ-ONLY" |
| `planner.md` | Read-only strategic planner | The most structured prompt: **4 named phases** (Clarify → Research → Plan Generation → Clearance Check), each a `###` subheading; enforces scope discipline ("must not exceed that verb") |
| `qa-tester.md` | Test writing | "Testing Principles" + "Gap Warning Triggers" bullet list |
| `red-team.md` | Adversarial security review, read-only | "What You Look For" vulnerability taxonomy (injection, secrets, auth, SSRF...); Output Format requires severity (`CRITICAL/HIGH/MEDIUM/LOW`) + file:line + repro scenario |
| `scout.md` | Fast read-only recon | Shortest prompt (21 lines); explicitly instructs "Parallelize: run independent searches simultaneously" |

Techniques used across the set: numbered workflows, fenced "Output Format" contracts (used by
critic/debugger/oracle/red-team), a delegation section ("When to Escalate") in several to route to
other named agents, and hard read-only/no-file-write framing repeated verbatim ("You are READ-ONLY",
"Read-only probing") for oracle/scout/red-team/planner/librarian.

### 2. Agent Roles / Team Structure

**Loading** (`src/plugins/agent_loader.js`): `AgentLoader` reads bundled `agents/*.md` first, then
project-level `.smallcode/agents/*.md` (same name overwrites bundled). A `drafts/` subdirectory is
explicitly quarantined from auto-loading — comments describe this as a future promotion path for
agent-authoring (`/evolve promote-agent`, not yet built). Same pattern for teams (`TeamLoader`,
`.smallcode/teams/`).

**Team format** (`teams/*.yaml`, 3 lines each — a hand-rolled mini-YAML parser, no `yaml` npm
dependency, `src/plugins/team_loader.js`):
```yaml
name: build
description: Full build pipeline — recon, plan, implement, verify.
agents: [scout, planner, code-engineer, critic]
```
Three bundled teams: `build.yaml` (scout→planner→code-engineer→critic), `debug.yaml`
(debugger→oracle), `review.yaml` (critic→red-team→qa-tester).

**Execution model — strictly sequential pipeline, not a graph/DAG.** `src/plugins/team_runner.js`
(`runTeam`, 50 lines): for each agent name in `teamDef.agents`, it instantiates a fresh
`AgentRunner`, runs it, and **feeds that agent's full output string as the next agent's input**
(line 43-44: `currentTask = result.output`). There is no shared scratch state, no parallel branches
— the file's own header comment says "No parallelism — local inference performance trap." If an
agent name doesn't resolve, the pipeline doesn't abort; it prepends an `[error from X: ...]` string
to the next task and continues.

**Sub-agent isolation** (`src/plugins/agent_runner.js`, `AgentRunner.run`): each sub-agent run is a
bounded, fully isolated sub-conversation:
- History starts as `[{role:'user', content: task}]` only — **parent conversation history is never
  passed in**.
- Tools are narrowed to the intersection of the agent's frontmatter `tools` list and the canonical
  tool registry, always including `read_file` (`buildNarrowedTools`).
- Hard caps: `MAX_STEPS = 15`, token budget `min(8000, contextWindow * 0.3)`.
- No MCP, no plugins, no nested repair-call logic, non-streaming direct fetch to the model endpoint.
- `run()` is designed to **never throw** — always returns `{output, steps, tokens, error?}`.

**Two hand-off mechanisms into this system from the main loop:**
1. A model-callable tool `spawn_agent` (`bin/executor.js` line 874) — the primary agent itself can
   decide mid-turn to delegate to a named sub-agent and get its output back as a tool result.
2. User-driven slash commands in `bin/commands.js`: `/agent <name> <task>` (constructs an
   `AgentRunner` directly, ~line 895) and `/team <name> <task>` (calls `runTeam`, ~line 947).

There is no planner/executor split at the architecture level in the sense of a persistent
orchestrator process — "planning" is instead handled inline in the main loop by the plan-tracker
(§3), and separately by the `planner` sub-agent persona when explicitly invoked via team/agent
dispatch.

### 3. Agent Loop / Workflow Design

Core loop: **`runAgentLoop(userMessage, config)`** in `bin/smallcode.js`, roughly lines 587-2017
(~1,430 lines for one function — this is the heart of the whole program). Companion pieces:
`bin/executor.js` (tool dispatch), `bin/governor.js` (tool scoring/verification),
`bin/model_client.js`, `src/session/*.js` (plan tracker, snapshot, contract, dependency graph, read
guard).

**Turn structure, in the order it actually executes:**

1. **Pre-flight, zero/near-zero-cost gates** before any model call:
   - Regex-based vague-message clarifier (`src/session/clarify.js` / compiled
     `features_adapter.checkNeedsClarification`) — only runs on messages <80 chars, with several
     exclusion heuristics (looks like a path, looks like "option 2", looks like an affirmation, or
     the assistant's last turn ended in a question).
   - `@file` reference expansion (`resolveReferences`), dropped-image detection
     (`src/session/images.js`), auto git-diff injection when the message implies recent changes.
   - Plan-tracker activation heuristic (`src/session/plan_tracker.js`, `shouldPlan`) — for
     multi-step-looking messages, injects a one-shot "emit a numbered plan first" system
     instruction, later stripped from history once a plan is captured. The plan format re-injected
     every subsequent turn:
     ```
     ACTIVE PLAN (step 3 of 5):
     ✓ 1. Read the existing auth module
     ✓ 2. Identify the JWT validation function
     → 3. Add the refresh token handler
       4. Update the route middleware
       5. Run tests
     ```
   - Snapshot checkpoint opened (`src/session/snapshot.js`) — every write/patch in the turn records
     pre-edit content for possible rollback.
   - Task-type classification (`classifyTaskAsync`, governor) and a **separate, deterministic
     tool-category router** (`src/compiled/tool_router.js`, `classifyToolCategory`) — a zero-LLM-call
     weighted regex scorer over 8 categories (read/write/search/run/plan/code-intelligence/web/respond)
     that decides which tool schemas get sent this turn. An "affirmation guard" prevents "yes"/"ok"
     replies from collapsing the tool set to none mid-task.
   - Auto-context retrieval via code graph (zero LLM calls, keyword walk).
   - Model-tier routing (fast/default/medium/strong) if `config.models` is set.
   - **Auto-compaction**: if estimated tokens exceed 80% of `detected_window * max_budget_pct` OR
     history >30 messages, it first tries an LLM-based semantic summary of older messages
     (`compressHistoryCompiled`, keeping the most recent 6 intact), falling back to blind
     oldest-message eviction if that's unavailable or fails.

2. **Inner tool-calling loop**, bounded by `MAX_TOOL_CALLS = 500` (line 562):
   - Every 3 tool calls, a **mid-turn eviction pass** checks token budget again: first truncates
     large `tool_calls.arguments` strings in old (already-executed) assistant messages, then evicts
     oldest tool-result/assistant pairs (only if both halves of a pair can be removed together, to
     avoid orphaning), replacing evicted content with `[evicted: N tokens]`.
   - Model response handling includes several defensive-recovery passes before anything else
     touches the message: (a) pull tool calls that leaked into free-text `content` back into
     structured `tool_calls` (`src/tools/tool_call_extractor.js`, for GGUF models that don't reliably
     use structured calling); (b) promote `reasoning_content` to `content` when a reasoning-model
     server left `content` empty; (c) strip `<think>...</think>` blocks from history (only shown in
     TUI if `SMALLCODE_SHOW_THINKING=true`), with a hard 500-token cap against runaway thinking
     loops.
   - **Tool name normalization** (`normalizeToolCall`) maps Claude/OpenAI-style names (`Read`,
     `Edit`, `Bash`) onto SmallCode's real tool names before dispatch.
   - A **quality monitor** (`src/governor/quality_monitor.js`, "itsy port") inspects each turn for
     empty turns, blank/hallucinated tool names, and exact cross-turn repeats; on a hit it injects a
     corrective steer and `continue`s, backing off after 3 consecutive corrections.
   - Malformed JSON tool args get a repair cascade: LLM-based `repairToolCall` (compiled feature) →
     regex extraction of `"path"`/`"content"` for `write_file` specifically → give up to `{}`.
   - Tool results are capped (default 8000 chars unless the detected context window ≥131072, or
     overridden via `SMALLCODE_MAX_TOOL_RESULT_CHARS`), passed through a **read guard**
     (`src/session/read_guard.js`) that does a smarter head/tail trim with a "re-read a smaller
     range" hint rather than blind truncation.
   - **"Poisoned history" fix**: if every tool call in a turn failed validation, the loop reverts
     `conversationHistory` to its pre-turn length and injects one clean correction message, rather
     than letting the model see and imitate its own malformed output.

3. **Improvement / verification loop** (this is SmallCode's closest thing to a Definition-of-Done
   gate for individual writes): after any `write_file`/`patch` that didn't error,
   `runValidation(filePath)` (delegated to `model_client.js`, uses `execFileSync` with arg arrays —
   explicitly hardened against shell injection) runs language-appropriate checks. On failure:
   - Attempts 1-2: inject `[AUTO-VALIDATE]` with the exact errors + prior-attempt history, asking
     the model to fix without repeating the same approach.
   - After `MAX_IMPROVE_ITERATIONS = 2` (line 563) fails: **DECOMPOSE** — either an LLM-based
     `decomposeTask` or a regex-based `pickDecomposeStrategy` (governor.js) picks a new strategy and
     re-prompts.
   - After the decompose strategy itself fails twice: **ESCALATE**, if a cloud key is configured
     (`bin/escalation.js` — Anthropic first, then OpenAI, then DeepSeek preference order; converts
     the whole history into the target provider's native tool-call format; session cap of 5
     escalations by default; `canEscalate()` returns false with no key, making the whole path
     dormant).
   - If escalation itself fails: optional **auto-rollback** to the pre-turn snapshot
     (`SMALLCODE_SNAPSHOT_AUTO_ROLLBACK=true`) reverts all writes made in the turn.
   - A parallel improvement loop exists for failing `bash`/`run` commands (same decompose→escalate
     ladder, capped error text at 800 chars).

4. **Early-stop detectors** (`src/governor/early_stop.js` / `bin/smallcode.js`): patch-spiral
   detection (model stuck repeatedly patching the same broken file → forces a full rewrite
   instruction), read-loop detection (soft nudge at 5 consecutive reads with no write, hard break at
   8), a "greeting after tool failures" detector (catches context-loss where the model suddenly says
   hello mid-task), and a post-decompose "gave up" detector that offers escalation.

5. **Definition-of-Done / hard-fail gate — the "contract" system** (`src/session/contract.js`,
   `contract_guard.js`, `contract_store.js`, `contract_tools.js`): a contract is a set of testable
   assertions the model commits to up front, each in state `pending|passed|failed|skipped`,
   persisted per-project under `.smallcode/contracts/<id>/{state.json, contract.md, assertions.md,
   log.jsonl}` (`state.json` is authoritative; the `.md` files are just rendered views).
   `checkDoneGuard(message.content, cwd)` runs on every candidate final assistant message: if the
   text matches "done"-shaped regexes (`DONE_PATTERNS` — "task is complete", "successfully
   implemented", "ready to ship", etc.) AND there's an active contract with unresolved assertions,
   the loop does **not** let that response through — it pushes a `[CONTRACT-GUARD]` system message
   listing the specific blockers and `continue`s the loop instead of `break`ing. This is
   opt-in/heuristic (disabled via `SMALLCODE_CONTRACT=false`) and explicitly designed to fail closed
   only on a clear completion claim, to minimize false positives.

6. **End of turn**: tool-call count summary, optional git auto-commit (via `execFileSync`, commit
   message from an LLM feature or truncated fallback), trace recording stopped and converted into a
   searchable "evidence" memory entry, an async **reviewer** critique of edited files awaited with a
   5s timeout (`src/model/reviewer.js`), and the snapshot checkpoint committed (discarded) since the
   turn completed without a forced rollback.

**Other loop-adjacent design points worth flagging for the SHAMSU comparison:**
- `src/plugins/agent_runner.js` runs an entirely separate, much simpler bounded loop (15 steps, no
  repair, no MCP) for sub-agents — a deliberately different/smaller state machine than the main
  loop, not a recursive reuse of `runAgentLoop`.
- `src/api/index.js` (`class SmallCode extends EventEmitter`) exposes a third, even simpler
  programmatic loop (`this.run(prompt)`) for library consumers — bounded by `maxToolCalls` (default
  50), used for scripting/embedding, not for the interactive CLI.
- Governor's `ToolScorer` (`bin/governor.js` lines 8-68) is a simple Beta-ish confidence estimator
  (`(success+1)/(total+2)`) per `tool:taskType` pair, persisted to `.smallcode/tool_scores.json`;
  `shouldAvoid()` flags a tool once it has ≥3 calls and confidence <0.35. This is the concrete
  mechanism behind the docs' "Governor (Bayesian tool scoring)" claim — real, but a much simpler
  smoothing formula than "Bayesian learning" implies.
- Two-stage tool routing (`src/tools/two_stage_router.js`): for context windows ≤16k, the model
  first picks a category (read/write/search/run/plan) from ~200 tokens of descriptions, then gets
  that category's full tool schemas in a second call — trading a round-trip for a much smaller
  first-call prompt.

### 4. Chat / Session History Persistence

**Format: plain JSON files, one per session, no database.** `src/session/persistence.js`, `class
SessionStore`:
- Directory: `.smallcode/sessions/` under the project root (`SESSIONS_DIR` constant, line 13).
- One file per session: `<id>.json`, containing `{id, title, model, messages[], tokens, cost,
  toolCalls, createdAt, updatedAt}`.
- **Session IDs are time-descending** (`(MAX_SAFE_INTEGER - Date.now()).toString(36)` + random hex
  suffix) specifically so lexicographic filename sort puts the newest session first with no extra
  index needed.
- **Atomic writes**: write to `<id>.json.tmp.<pid>.<timestamp>`, then `fs.renameSync` over the real
  path — survives a crash mid-write (`_save`, lines 162-174).
- **Permissions hardening**: files `0o600`, directory `0o700` (best-effort on Windows via
  `chmodSync`, wrapped in try/catch since POSIX perms don't map there).
- **Path-traversal guard**: `load(id)`/`remove(id)` validate the id against `/^[A-Za-z0-9_-]{1,64}$/`
  and also check the resolved path still starts with the sessions dir before touching disk.
- **Secret redaction before write**: `redactValue(session)` (from `src/security/sanitize.js`) strips
  things that look like API keys/tokens out of the session JSON before it's ever persisted.
- Retention: `prune()` keeps the newest `MAX_SESSIONS = 50`, deleting older ones.
- **This does survive process restart** — `resume()` loads the most recent session file from disk,
  and the `/sessions` slash command (`bin/commands.js` ~line 752) lists and resumes by id (including
  partial-id prefix match).

**No SQLite anywhere in the codebase.** I grepped the whole tree for `sqlite`/`SQLite`/`FTS5`/
`better-sqlite3` and it only turns up in prose comments, never an actual dependency or `require`.
`package.json` has no sqlite-family dependency. The actual "long-term memory" implementation
(`bin/memory.js`, `class MemoryStore`) is a **JSON index file** at `.smallcode/memory/index.json`,
with typed objects (`decision|workflow|gotcha|convention|context|source`) and `loadForTask()` doing
**keyword-overlap scoring**, not full-text search. **This directly contradicts both
`ARCHITECTURE.md` §7 ("long-term project memory lives in a SQLite database with full-text search")
and `COMPARISON.md`'s feature table ("Memory (persistent): ✓ SQLite + FTS5, typed")** — the real
mechanism is a flat JSON file with a hand-rolled relevance scorer, not a database with FTS5.

**Snapshots** (rollback mechanism, distinct from session history): `src/session/snapshot.js`,
metadata under `.smallcode/snapshots/` — pre-edit file content recorded per write/patch call within
a turn, used for the auto-rollback path in the improvement loop (§3). This is a "did this turn's
edits work" mechanism, not a chat-history viewer.

**Contracts** (Definition-of-Done state, §3): `.smallcode/contracts/<id>/state.json` +
human-readable `.md` mirrors — another disk-persisted, JSON-first store, separate from both sessions
and memory.

**"Multi-session" claim is also inconsistent with the code.** `COMPARISON.md`'s feature table marks
"Multi-session: ✗ ... SmallCode is single-session" — but `src/session/multi.js` (`class
MultiSessionManager`, wired to a real `/session list|new|switch|kill` command set in
`bin/commands.js` line 699) exists and is functional in the sense that it tracks multiple named
conversation states with an "active" pointer. However it's genuinely thin: `sessions` is an
in-memory `Map` only (no persistence to disk), and although it imports Node's `fork` from
`child_process`, nothing in the file actually spawns a child process — each "session" is just a
separate in-memory message array inside the same process, not a true parallel agent. So the doc's
"✗" is roughly directionally fair (there's no real OS-level parallelism) but the code claim of a
working `/session` subsystem is real and more than the table admits.

**No web UI / server / portal for browsing sessions**, at the time of this pass — I searched for
`express`/`app.listen`/`http.createServer`/`portal`/`dashboard` across the whole tree and for any
`web`/`server`/`ui`/`portal` directories. `express` is listed in `package.json` dependencies but is
never `require()`'d anywhere in `src/` or `bin/` — it appears to be an unused/transitive dependency
(possibly pulled in for something inside `bonescript-compiler` or `budget-aware-mcp`). There is no
`server/`, `web/`, `portal/`, or `ui/` directory; the only UI is the terminal (`src/tui/`,
`marrow/src/tui/`, `bin/tui.js`). Everything else matching "web"/"ui" in a directory search was
either the terminal UI (`tui`), a test file (`test/web_prompt.test.js`, `test/live_tui.test.js`), or
BoneScript/liquid-tool-parser code unrelated to session viewing. *(Corrected in §0.1 above: while
there is no self-hosted server, `src/session/share.js::exportToGist()` does produce a
browser-viewable link — an external GitHub Gist, not a page smallcode itself serves. The paragraph
above is the original, more absolute finding; §0.1 is the corrected, more precise one.)*

**Session "sharing" is export-to-file, not a viewer.** `src/session/share.js`:
`exportToMarkdown(session, outputPath)` renders a session (redacted) as a `.md` transcript;
`exportToGist(session)` shells out to the `gh` CLI to publish that markdown as a GitHub Gist (writes
to the OS temp dir, not project dir, and cleans up after). This is the entirety of "session
sharing" — there is no in-app session browser beyond the CLI's `/sessions` list/resume text output,
and no persistent server component of any kind.

**Bottom line for the gap analysis**: session persistence is real, functional,
restart-survivable, and reasonably hardened (atomic writes, 0600 perms, path-traversal checks,
secret redaction) — but it is flat JSON files on disk, not a database, and there is no web
portal or dashboard of any kind, contradicting nothing in the docs (the docs don't claim a web
UI) but the memory-layer SQLite/FTS5 claim in `ARCHITECTURE.md`/`COMPARISON.md` is not backed by
the code.

---

## Appendix B — full SHAMSU research agent report (verbatim)

Raw output of the research agent tasked with mapping SHAMSU's current master prompts, agent roles,
agent loop, and session persistence, for direct comparison against Appendix A. §§1–4 above are a
synthesis built from this; this appendix is the primary source material.

> **Orientation note (important):** the task brief said "the v2 rebuild lives under
> `Shamsu\src\shamsu\`." That is factually wrong on the current checkout — `Shamsu\src\shamsu\` is a
> completely empty directory tree (14 subfolders, **0 files**, last touched Aug 18). The real,
> actively developed package (309 `.py` files, commits today) is `Shamsu\shamsu\` (no `src/`
> prefix), confirmed by `pyproject.toml` (`name = "shamsu"`, package root at repo top level) and by
> `CLAUDE.md`'s own layer map. Checked-out branch is `small-shamsu` (63 commits ahead of `main`,
> unmerged), latest commit today (2026-08-20). All paths below are relative to `Shamsu\` and refer
> to `Shamsu\shamsu\...` unless stated otherwise.
>
> Also note: SHAMSU's own memory file (`shamsu-repo-layout.md`) is itself stale on this exact point
> — it says the package is `Shamsu/src/shamsu/`. Flagging this since a gap-analysis reader may
> otherwise trust that note. *(This has since been corrected in project memory — see the
> `shamsu-repo-layout` memory entry.)*

### 1. Master prompts / system prompts

**The "cut down to ~144 tokens" memory claim is confirmed true and is the live default.** The system
prompt lives in one markdown file, not in Python:

- `shamsu/agents/prompts/simple_system.md` — the actual words sent to the model.
- `shamsu/agents/simple_prompt.py` — the loader/policy (parses the markdown into named `## section`
  blocks, decides which sections go out each turn, has a small hardcoded `_FALLBACK` dict in case
  the file is missing).

Structure: YAML frontmatter declares `sections: [base, act, symbols, done, recall, big_read,
big_file, graph]`. Each `## section` is HTML-comment-annotated with the *reasoning* for why it
exists (these comments are stripped before sending — `_COMMENT` regex in `simple_prompt.py`). `base`
+ `act` are always sent; the rest are conditional:
- `symbols`, `done`, `recall`, `big_read`, `big_file` — sent every turn (cheap, always relevant).
- `graph` — only if `CodebaseMemoryAdapter().is_available(workspace)` returns true for that
  workspace.
- a dynamically generated skill index (`_skill_index`) — only if the workspace has bundled/custom
  skills.

Representative excerpt (the `base` section, i.e. the persona):
> "You are SHAMSU, a coding assistant working in {workspace}. You can read, search and change files
> in that folder, and run commands there... When someone asks you to review, explain, or plan, the
> answer IS the work... You are talking to one person over time. Earlier messages in this
> conversation are real: refer back to them..."

And the `act` section (added specifically to stop models that ask instead of act):
> "Act on what you were asked. If a task has several parts, carry on through them and say what you
> did at the end - ask only when the request is genuinely ambiguous and a wrong guess would waste
> real work."

**Notable structural techniques**, all explicit in the comments:
1. **Positive framing over prohibition.** The doc explicitly states the legacy prompt sent "49
   bullet rules... with 'do not claim complete' repeated four times," and the observed effect was a
   model that "inspected, read, re-read, and never wrote." The rewrite states what TO do, never what
   not to do.
2. **Capability-naming discipline** ("smallcode's issue #58," cited by name): "a capability not
   named here is one it will not use, and one named here that does not work is a wasted round." This
   is why sections like `graph` are gated on actual availability rather than always advertised.
3. **Done-as-state, not done-as-prohibition.** Instead of repeating "don't claim complete," the
   `done` section hands the model a `contract_create`/`contract_assert_pass` tool contract mechanism
   — "You cannot report the task finished while a claim is unchecked." (Detailed in §3.)
4. **Live-tuned via dogfooding**, with each rule's origin story kept in-file: e.g. the `act` section
   cites a live run on `qwen2.5:3b` where the model wrote 39 of 1,500 requested lines and stopped to
   ask a question nobody needed answered.

**Legacy prompt still exists in the codebase** (not deleted, just bypassed by default):
`shamsu/agents/prompting.py` (254 lines) and prompt-construction logic scattered through
`shamsu/agents/chat_loop.py` (`_system_prompt` at line 4991, `_regrounding_block`,
`_native_tool_schema_payload`) build the older, larger, router/phase-aware prompt used only when
`SHAMSU_LEGACY_ROUTING=1` is set (gate at `shamsu/agents/simple_chat.py:275`: `simple_mode_enabled()`
returns `not os.environ.get("SHAMSU_LEGACY_ROUTING", "").strip()`, i.e. simple mode is on unless that
env var is set).

**Other prompt-like assets found** (not agent system prompts, but worth noting for comparison
purposes):
- `shamsu/templates/game-2d/master_prompt.md` and `shamsu/templates/multiplayer-game/master_prompt.md`
  — scaffold-generation prompts for the (currently disabled-by-default) template system.
- `shamsu/skills/bundled/*/SKILL.md` (8 files: mcp-tools, prd-planner, react-vite, sql-databases,
  sqlite-persistence, testing, ui-designer, developer, large-file-surgery) — these are
  Claude-Skill-style knowledge docs the model can pull via a `use_skill` tool, not system prompts;
  they're advertised in the `recall`-adjacent `_skill_index` block only when relevant to the
  workspace.
- `shamsu/repair/prompt.py` — a separate, narrow prompt used only by the deterministic repair/verify
  loop (`verify/gate.py`'s `LLMProposer`), for proposing a fix to a failing check. Not a general
  agent persona.

### 2. Agent roles / team structure

**In the current default (SIMPLE MODE), there is one generalist loop, one persona, no
planner/executor split.** `shamsu/agents/simple_chat.py`'s `SimpleChatLoop` class (spans lines
1654–5114 of a 6,108-line file) is the entire agent: one system prompt, one tool roster, one model,
one loop. There is no council, no sub-agent spawning, no role routing inside simple mode.

**Multiple specialized "workflows" still exist in the codebase as separate modules**, but they
belong to the legacy/orchestrator path (`SHAMSU_LEGACY_ROUTING=1`), not the default:
`shamsu/agents/qa_workflow.py`, `bugfix_workflow.py`, `code_edit_workflow.py`,
`test_generation_workflow.py`, `doc_workflow.py`, `audit_workflow.py`, `task_execution_workflow.py`,
`freeform_generator.py`, `scaffold_pipeline.py`, `full_pipeline.py`, plus `orchestrator.py` (409
lines) and `planner.py` (458 lines) implementing a router → planner → phase-gated task object model.
`orchestrator.py`/`planner.py` are the closest thing to a "planner/executor split" that exists
anywhere in the repo, but per `CURRENT_STATE.md`'s banner, this whole router/planner/phase-object
path "is no longer the one described here" as default behavior — it only runs opted-in.

**"Council mode"** (`shamsu/agents/` — referenced in `CURRENT_STATE.md` as "sequential draft →
critique → reconcile") is a real built feature, gated by low routing confidence / destructive action
/ security-sensitive path, and wired into bug-fix and code-edit workflows — i.e. it's part of the
legacy orchestrator's toolkit, not simple mode.

**Net finding for the gap analysis:** today's shipped default has *zero* multi-agent structure. It
is one prompt, one loop, one set of tools, exactly like the "simple mode" description in memory. The
multi-role machinery (planner, specialist workflows, council) is real code, still present, still
tested, but off by default and reachable only via an env var most users won't set.

### 3. Agent loop / workflow design

**Two parallel loop implementations coexist; only one runs by default.**

**Default: `SimpleChatLoop` — `shamsu/agents/simple_chat.py`**
Constructed per-turn in `shamsu/cli/repl.py` around line 4843 (`_run_simple_chat`), wired with a
`client` (ollama AsyncClient), `tools`, `session_logger`, `action_ledger`, and `emit=stream.publish`
(a `TurnStream`, see §4).

- **Turn shape**: `run(user_input)` → `_run_turn(user_input)`. System prompt (from
  `simple_prompt.py`) + hydrated history (via `ChatState`, `shamsu/agents/chat_state.py`) + tool
  schemas are assembled, `_call_model()` invokes the model, `_run_tools(calls)` executes any tool
  calls returned, results feed back in, loop continues until a final text answer or a round/timeout
  ceiling is hit.
- **Tool calling**: tool schemas built by `build_simple_tools()` (line 6063) /
  `active_tool_schemas()` (1205); `_execute(name, arguments)` (3536) dispatches to individual tool
  handlers (`_replace_symbol`, `_read_symbol`, `_append_file`, `_use_skill`, `_run_tests`,
  `_history_search`, `_memory_tool`, `_graph_tool`, `_hybrid_search`, the DoD contract tools, etc.).
  Small-model tool-call salvage (messy JSON, truncated calls) is handled centrally in
  `shamsu/llm/output.py::parse_model_turn`, used by both loops.
- **Definition of done — two layers, both real but different in kind**:
  1. *Self-reported contract* — `_contract_tool` (simple_chat.py:4049) implements five tools
     (`contract_create`, `contract_status`, `contract_assert_pass`, `contract_assert_fail`,
     `contract_assert_skip`) over an on-disk contract object (`shamsu/agents/simple_contract.py`).
     The model writes down checkable assertions, then must mark each as passed/failed/skipped *with
     evidence text* before it's allowed to claim "done." This is model-driven, not
     harness-enforced.
  2. *Harness-owned deterministic verification* — `shamsu/verify/gate.py` (1,370 lines) still exists
     and is fully built: discovers a `VerificationPlan` of executable checks from project metadata,
     runs them in stable order, produces a `VerifyOutcome` (verified/failed/unverifiable), and can
     invoke `shamsu/repair/loop.py`'s `RepairLoop` for bounded auto-repair. **However, this module is
     used only by the legacy path** — `chat_loop.py`, `cli/repl.py`'s PRD-build flow,
     `freeform_generator.py`, `verification/verifier.py`. `simple_chat.py` does **not** import
     `verify/gate.py` at all. Instead, `SimpleChatLoop._verify()` (line 4848) runs a much lighter,
     purely syntactic per-write check (`check_file`) plus "still being built" heuristics for
     chunked writes — no project-level test/build execution, no repair loop.
- **Context window / compaction**: `TokenAllocation` (dataclass, line 1458) tracks token spend by
  bucket (system_prompt, tool_schemas, grounding, conversation, tool_results) rather than one flat
  number — explicitly designed because "the majority bucket was tool results, not conversation," so
  naive oldest-first eviction was wrong. `_compact_if_needed()` (2519) triggers `_narrate()` (2564),
  which is the "digest" mechanism: it produces a rolling summary (`_digest(previous, evicted)`, in
  the module-level helpers ~5461) of evicted turns, persisted via `session_logger.save_summary()` so
  it survives process restart (see §4). `_elide_payloads()` (2739) shrinks old
  tool-call/tool-result payloads in memory (never on disk) rather than dropping messages outright —
  lossless because the underlying file is still readable via `read_file`. `_shrink_for_oom()` (2666)
  and `_evict_other_models()` (2690) handle actual VRAM pressure. Per memory notes, real LLM-driven
  compaction happens once per user turn via this `_compact_if_needed`/`_narrate` pair — confirmed
  present in code.
- **History hydration**: `ChatState` (`shamsu/agents/chat_state.py`) rebuilds conversation from
  `messages.jsonl` on each new `ChatState` (i.e., each user turn constructs a fresh one), capped at
  `HYDRATE_MAX_MESSAGES = 24` records as a hard horizon *before* token budgeting, with older content
  represented only by the persisted rolling summary. It also filters out harness-authored status
  text (`_HARNESS_STATUS_PREFIXES`/`_HARNESS_STATUS_PATTERNS`) so the model never re-ingests its own
  timeout/stop messages as if they were normal turns — this was a fixed bug ("a session accumulated
  ten identical... turns and then wrote nothing for the rest of its life").
- **Errors/retries**: `_RetryEscalation` class (396) tracks malformed tool-call attempts and
  escalates sampling params; specific correction-message builders exist for common small-model
  failure modes (`_unparseable_tool_call_correction`, `_truncated_write_correction`,
  `_write_failure_correction`, `_edit_failure_correction`, `_discovery_failure_correction`,
  `_repetition_correction`) — each returns a targeted, specific nudge rather than a generic retry.
- **No phases/milestones/sub-agent spawning inside SimpleChatLoop itself.** It is a flat ReAct-style
  loop. Milestone/phase objects (`MilestoneTask`/`TaskStep`, `shamsu/tasks/`) exist and are used by
  `/build` (PRD-driven builds) and the legacy orchestrator, not by ordinary chat.

**Legacy: `AgentChatLoop` — `shamsu/agents/chat_loop.py`**
5,136 lines, opt-in via `SHAMSU_LEGACY_ROUTING=1`. Has the router/planner/phase-object machinery,
its own compaction (`_structured_compact`, `_summarize_evicted`, `_hard_trim_messages`,
`_messages_within_budget`), its own `_maybe_verify`/`_attempt_repair` that *does* call
`verify/gate.py`, step controllers (`_start_step_controller`, `run_step`), and a much larger
`_run_inner` (2186–3351, ~1,165 lines for one method) driving the full task-state machine. This is
the path CURRENT_STATE.md says "no longer runs by default."

### 4. Chat / session history persistence — the most important section

**Where it's written, and confirmed genuinely working on disk**

Sessions are **workspace-local**, at `<workspace>/.shamsu/sessions/<session_id>/`, managed by
`SessionManager`/`SessionLogger` in `shamsu/session/manager.py` (1,711 lines). Per session
directory:
- `messages.jsonl` — lossless, append-only, one JSON record per message
  (`SessionLogger.append_message`, manager.py:736), written **synchronously on every call**, not
  batched or exit-triggered. Explicitly designed to survive a malformed/reformatted file
  (`read_messages` recovers pretty-printed JSON if line-parsing mostly fails — this was a real prior
  bug: "655 of 657 lines failed to parse" after an editor reformatted the file).
- `activity.jsonl` — the "turn stream" (`shamsu/runtime/turn_stream.py`, `TurnStream`/`TurnEvent`),
  high-frequency UI telemetry (turn.start, tool.call, tool.result, assistant, turn.end, status,
  error), written durably-first-then-fanned-out-to-live-renderers via `TurnStream.publish()`/
  `_write()`.
- `session.json` (metadata: title, workspace, timestamps, rolling `summary`, `summarized_upto`),
  `events.jsonl` (structured event log via `SessionLogger.log()`), `state.json`, `owner.json`
  (live-process claim/lease), `report.md`.

**I verified this is genuinely working**, not just claimed: found a real session on disk from
earlier today (`test-shamsu/live-player/.shamsu/sessions/20260820-034524-05a1/`) with 24 real
`messages.jsonl` records (actual user prompt, tool calls, tool results, model's final answer) and
34 real `activity.jsonl` events (turn.start → tool.call/tool.result pairs → assistant → turn.end
with `changed_files`). Compaction/summary persistence, session forking with parent pointers
(`shamsu/session/history.py`), and cross-session `history_search` (BM25 + cosine over the whole fork
ancestry) are all real, non-stub code. *(The user has since reported chat history is not saved
properly in their actual experience — see §0.3 above. This finding stands as-is: a specific,
verified-good session on disk, not a blanket claim that the mechanism never fails. Treat as
unresolved pending a fresh repro, not as settled either way.)*

**So raw persistence is not the bug.** The problem is on the *read/discovery* side, and I found two
concrete, verifiable root causes:

**Root cause A — headless runs never register their workspace with the portal (verified)**

`shamsu/runtime/workspaces.py` maintains a global registry (`~/.shamsu/workspaces.json`, via
`remember_workspace()`/`known_workspaces()`) of "every workspace SHAMSU has opened" — and this is
the *only* list the web portal's sidebar (`WebPortal.workspaces()` in `webui/server.py`,
`workspaces_payload()` in `webui/api.py`) draws from. `remember_workspace()` is called in exactly
two places in `cli/repl.py`: interactive REPL startup (line 18606, "Record it the moment a REPL
opens here") and inside the `/web`/`shamsu web` code paths themselves. **`shamsu/cli/noninteractive.py`,
which implements headless `shamsu run --prompt ...`** (the exact command the project's own
`CURRENT_STATE.md` calls out as "what makes dogfooding measurable," and the command that produced
the live-player session above) **contains zero calls to `remember_workspace`.** I confirmed this by
grep — no match. I then confirmed the *symptom*: `~/.shamsu/workspaces.json` on this machine lists
`webdemo`, `shamsu`, `Shamsu`, `openbazaar-build`, `openbazaar-telegram-smoke`, `test1`,
`asteroid-shamsu`, `demo2` — but **not** `live-player`, despite `live-player` having a real,
complete, correctly-saved session from earlier today. That session is 100% present and correct on
disk and 100% invisible to the web portal's workspace picker unless the user manually runs
`/workspace <path>` or `shamsu web --scan <dir>`.

**Root cause B — the web portal is not a standing service; it's ephemeral and manual, by design, in
two different ways**

1. `/web` (the in-REPL command, `shamsu/webui/local.py`) starts a `WebPortal` as **a daemon thread
   inside the currently-running REPL process**. The module docstring says this outright: *"That
   shape is a workaround, not a design: it exists because `run_control._RUNS` is a module-level
   dict, so sharing the live run state means sharing the process."* Consequence: **the moment you
   exit the CLI, this portal dies.** There is no background/persistent server left running. The
   access token is printed exactly once and never again — "recovered by restarting the portal,
   which mints a new one."
2. There **is** a proper standalone alternative — `shamsu web` (`shamsu/webui/cli.py::serve()`) —
   which correctly reads `activity.jsonl` from disk rather than in-memory state, so it works across
   process boundaries and doesn't require a REPL to be open. It's fully wired through argparse
   (`shamsu/cli/arguments.py`, `choices=("run","web")`) and the managed launcher
   (`~/.shamsu/bin/shamsu.ps1` passes args through verbatim). But it is **opt-in and must be run as
   its own separate command** (`shamsu web` or `shamsu --web`) — nothing auto-starts it, nothing
   keeps it running after you close its terminal (it blocks until Ctrl-C), and if the workspace you
   care about was only ever driven headlessly (root cause A), it won't show up even here unless you
   pass `--scan`. *(The user has since reported this standalone path is also broadly broken in
   practice — "not ready at all, many errors" — see §0.2 above. That goes beyond what this pass
   verified in the code and needs its own live-driven audit.)*

**Secondary observations (contributing, lower-severity)**
- `session_logger.log("assistant.message", ...)` (used for `_log_assistant_message` in
  `cli/repl.py`, called once at the end of a simple-chat turn) writes to `events.jsonl`, and its
  event-type string ("assistant.message") does **not** match the `"chat.message"` check in
  `SessionLogger.log()` (manager.py:667) that increments `metadata.message_count`. So
  `session.json`'s `message_count` field under-counts for some paths — a cosmetic bug (session list
  previews may show stale/zero counts) but not a data-loss bug, since `messages.jsonl` and
  `activity.jsonl` are populated by other, correct call paths (`ChatState._append` →
  `session_logger.append_message`, and `SimpleChatLoop._publish`).
- The portal's primary rendering path (`api.py::_conversation_from_turns`) is well-designed and
  *fixes* an earlier real bug (documented in its own docstring: a session with 2 real user questions
  rendered as 12 user bubbles + 4 empty ones when read naively from `messages.jsonl`, because
  loop-authored nudge messages carry `role: user`). It now reads `activity.jsonl` first and only
  falls back to filtered `messages.jsonl`. This part is solid and not the cause of the user's
  complaint.

**Bottom line for the gap analysis (as of this pass — see §0 corrections above for what's since been
disputed):** Session history storage itself (the JSONL transcript + turn-stream design) is
well-engineered and I verified it working end-to-end on a real, recent, on-disk session. **The break
appeared to be entirely in discovery/visibility**: (1) headless/scripted runs — the dominant way this
project actually gets exercised, per its own docs — never register their workspace, so their
sessions are structurally invisible to both the in-REPL `/web` view and standalone `shamsu web`
unless the user knows to `--scan` or manually add the workspace; and (2) even for interactive
sessions, there is no always-on server — "the web portal" is either a thread that dies with the
terminal it was typed into, or a separate blocking command the user must remember to start
explicitly every time, with a token that expires and isn't logged anywhere retrievable. Concrete fix
targets: add a `remember_workspace()` call in `shamsu/cli/noninteractive.py`'s run path; and decide
whether `shamsu web` should become long-running/auto-started (the code's own comment already
anticipates this: *"When the control plane lands, the portal can move out into `shamsu serve`..."* —
i.e. the authors know this is unfinished). **User feedback since this pass (§0) indicates the actual
scope is broader than this — both the persistence claim and the "just needs discovery fixing" framing
need re-verification before being treated as the full picture.**
