# SHAMSU — Current State

**As of:** 2026-07-21 · **Version:** `0.4.0b1` · **Branch base:** `develop`

This is the honest snapshot. `PROGRESS.md` is the long historical ledger;
`REQUIREMENTS.md` is the spec. This file is what an agent or a new contributor
should read to know where the project actually stands.

---

## Verdict In One Paragraph

The plumbing is done and the plumbing is good. Indexing, retrieval, context
packing, the run/artifact ledger, safety gating, session persistence, model
tiering, and the diagnostic surface are all built, tested, and fast. What is
*not* reliable yet is the agent's behavior layer: routing picks the wrong lane
on ordinary prompts, stale project context leaks across unrelated requests, and
read-only instructions are not consistently honored. A fresh dogfood pass on
2026-07-20 produced 2 clean passes, 1 partial, and 4 failures out of 7 everyday
prompts. **SHAMSU is not yet reliable enough for general local coding-agent
use.** The gap is behavior, not infrastructure.

---

## Numbers

| Measure | Result | Source |
|---|---|---|
| Unit/integration tests | **1449 passed, 1 skipped** | `pytest tests/ -q`, run 2026-07-20 (post-fix) |
| Lint | passes | `ruff check shamsu tests scripts` |
| Model-quality evals (default tier) | **11/12**, 3 samples/case, 2 flaky | `BENCHMARK.md` (pre-fix; re-measure) |
| Release gate (deterministic) | **PASS** — startup 1.27s, first answer 1.07s, peak RSS 90.9 MB | `RELEASE_VALIDATION.md` |
| Real-prompt dogfood, before fixes | 2 pass / 1 partial / 4 fail of 7 | `test-shamsu/SHAMSU_FRESH_DOGFOOD_2026-07-20.md` |
| Real-prompt dogfood, after fixes | **re-verified: create-file, run-script, web-route, and `/run show` all corrected** | this document, below |
| Codebase | 202 modules in `shamsu/`, 126 test files | — |

The spread between "1418 tests green" and "4 of 7 real prompts failed" was the
single most important fact about this project. It is why the failures below were
found by dogfooding and not by the suite: **the tests pin components; they did
not pin end-to-end task semantics.** The regression suite added on 2026-07-20
pins the semantics for these specific failures, using the verbatim prompts.

---

## What Is Built

### Solid — built, tested, and behaving

| Subsystem | State |
|---|---|
| **Workspace indexing** | Incremental SQLite + FTS5 index at `.shamsu/index.db`. Skips rehash on unchanged size/mtime, skips symbol rebuild on unchanged hash, cleans stale rows on move/delete. Runs transparently — no manual `/index` needed. |
| **Search & retrieval** | FTS5 over-fetch plus additive re-ranking: path match, symbol match, lazily-built `rank_bm25` recency layer, and caller-supplied `boost_paths`. |
| **Context engineering** | Snippet packing, middle truncation, budget accounting with a vendored Qwen3 tokenizer (char/4 fallback offline). No full-codebase prompting. |
| **Run/artifact ledger** | Every prompt writes `manifest.json`, `events.jsonl`, `decisions.jsonl`, `tool-calls.jsonl`, `model-calls.jsonl`, `mutations/`, `contexts/`, `context-preview.json`, `final-output.md`, `summary.json`. Verified complete on every fresh run. |
| **Mutation ledger + rollback** | Every file mutation records a backup and a patch; `/undo` restores. Verified: the dogfood run that wrongly overwrote `qa_probe.py` had a recoverable backup. |
| **Safety layer** | Workspace sandbox, path-traversal blocking, dangerous-command denylist, secret redaction, tiered permission memory. `run_command`/`file_delete`/`web_search`/`mcp_tool` are never auto-approvable. |
| **Sessions** | Workspace-local sessions, resume, redacted JSONL events, rename/close/export to ZIP. |
| **Model tiering** | light / default / heavy, one role contract, persisted at `.shamsu/model_tier.json`, `SHAMSU_MODEL_TIER` override. Lazy per-model pull with a progress bar. |
| **Model I/O boundary** | `llm/output.py::parse_model_turn` salvages tool calls out of messy small-model output; `ModelSpec` capability flags gate native tools and `think=`. |
| **Runtime lifecycle** | Ollama detect/start/repair; last-session-exit unloads SHAMSU's own models (or stops the server only if SHAMSU started it), so ~6 GB isn't held after exit. |
| **Diagnostics** | `/doctor` — editable-install health, Ollama status, stray nested `.shamsu`, ancestor-workspace conflicts, PATH manifest, cookbook check. |
| **Install/uninstall** | One-command PowerShell + Bash install, managed launcher at `~/.shamsu/bin`, PATH manifest so uninstall removes only what SHAMSU added. Non-fatal on flaky Playwright/winget steps. |
| **Headless harness** | `shamsu run` with deterministic approvals, `--dry-run`, `--timeout`, JSON output, artifact validation. This is what makes dogfooding measurable. |
| **Eval harness** | `python -m evals` — task-success cases with per-case sampling and flaky-case flagging. |

### Built and works, but quality is bounded by the local model

| Subsystem | Note |
|---|---|
| **PRD pipeline** | Markdown/TXT/PDF input, rule-based entity extraction, `ProjectSpec` assembly, plan preview + approval, resume state. Parsing is deterministic and reliable; the *plan quality* depends on the planner model. |
| **Django generation** | Deterministic fixed templates + backend generators (models/serializers/forms/views/urls/admin), approval-backed writer, static consistency checker, setup/migrate/test/fix loop, frontend templates. The deterministic half is dependable. |
| **Code edit / bug fix** | Indexed context → specialist → unified diff → validate → preview → approve → apply, with a full-file rewrite fallback on a malformed diff. Diff quality is the coder model's ceiling, not the harness's. |
| **Plan mode** | `plan <task>` writes a reviewable `.shamsu/plans/*.md`; `proceed` executes it step-by-step as a `MilestoneTask`. |
| **Web + browser** | Permission-gated search (searxng/DDG) with real-URL decoding and snippet synthesis; Playwright browser inspect/debug. The *tool* works — see the routing bug below for how the answer gets mislabeled. |
| **Task tracking** | `MilestoneTask`/`TaskStep` with phase gating, blocked-vs-failed distinction, files/commands/test-results, persisted at `.shamsu/tasks/<id>.json`. `/tasks list|show`. |
| **Council mode** | Sequential draft → critique → reconcile, gated by low routing confidence / destructive action / security-sensitive path. Wired into bug-fix and code-edit. |
| **Autonomy toggle** | `/autonomy on` raises the agent-loop ceiling 5→40 rounds and the repair loop 3→15 iterations with stall detection. Default **off**; behavior is byte-identical to the capped path when off. |
| **Abstract / code memory** | Available, external retrieval mode, non-degraded, index fresh. Optional Graphiti + FalkorDB adapter. Note: `forget()` on the Graphiti path is still an unimplemented stub. |

### Deliberately off or parked

- **Copy-paste template scaffolds** (game-2d, 3D multiplayer) are disabled by
  default and routed to freeform generation. `SHAMSU_ENABLE_TEMPLATES=1` restores
  them. The Django writer is kept.
- **v2.3 phase two** — marker-based hole filling, bounded DoD repair loop,
  Playwright runtime checks for the multiplayer template.
- **MCP support** — specified, not built.

---

## What Actually Works On A Real Prompt

Verified end-to-end in the 2026-07-20 dogfood, headless, fresh workspace:

- ✅ **Workspace questions** — "what folder are you in?" → correct answer,
  route `workspace.location`, no model call, **0.19s**.
- ✅ **Read and explain a file** — correct explanation of a small script, no
  file changes, route `file.read`. Correct but **20.6s**, which is slow for a
  tiny deterministic read.
- ⚠️ **Web search** — the answer was *right* (Python 3.13.0, 2024-10-07, with
  source URL), approval and tool evidence logged correctly, no files touched —
  but the run was labeled `failed` on route `file.write`. The tool works; the
  wrapper lies.
- ✅ **Artifact integrity** — all ten artifact files present on every run;
  `run_validation.ok == true` across the board.

---

## What Was Broken — And What Was Actually Causing It

The 2026-07-20 dogfood reported 8 bugs. Tracing the run artifacts collapsed
them into **3 root causes plus 2 isolated defects**; one reported bug was not a
bug at all. All five are now fixed (2026-07-20), with regression tests in
`tests/test_dogfood_2026_07_20_regressions.py`.

### Cause A — a read-only clause was read as an instruction to act ✅ FIXED

`"Do not modify files"` contains `modify` and `files`, and three independent
keyword scanners saw intent to write. Measured: the identical web-search prompt
matched **no route** without that sentence and **`file.write`** with it.

It caused three of the eight reported bugs at once:

- the wrong route (`_looks_like_file_write_request`);
- a false failure verdict — a correct web answer buried inside *"I did not
  complete the requested workspace change"* (`_request_requires_workspace_change`);
- no enforcement anywhere: `_enforce_read_only_decision` existed but sat inside
  the `ROUTE_FALLTHROUGH` tail, downstream of the route table that had already
  mis-fired. Its regex also missed `"do not modify any other files"`.

**Fix:** new `shamsu/safety/read_only.py` is the single definition. The clause is
masked before *any* intent scan, and `AgentToolRegistry.set_read_only()` makes it
a hard deny on every mutating tool — deliberately independent of approval mode,
because `--approval allow` answers "may I act without asking", not "may I ignore
what you told me". A carve-out (`"any other files"`) is distinguished from a
blanket ban, so it no longer refuses the one file the prompt asked for.

### Cause B — the markdown fallback turned a correct answer into data loss ✅ FIXED

The worst one. The model was asked for a command's output and gave exactly
that — `Command output:\n```\n5\n```` with **zero tool calls**. The fallback
wrote `5` over the user's 5-line script. Four guards failed simultaneously:

- `_parses_as_python("5")` was `True` — `5`, `hello`, `done`, `{"a":1}` all parse
  as valid Python, so "must at least compile" admitted any command output;
- `_plausible_replacement` waived the size check entirely below 40 lines,
  leaving small files — the ones a stray token destroys outright — unprotected;
- an untagged fence is a usage signal, but was only consulted to break ties
  between multiple blocks, never for a lone block;
- the read-only instruction never reached the code.

**Fix:** all four closed, plus a fifth at the call site — a turn that already ran
a tool successfully no longer reaches the fallback unless the model explicitly
proposes a file write.

### Cause C — naming any document hijacked the run into `prd.build` ✅ FIXED

`_extract_prd_path_from_prompt` grabbed the filename the user wanted to
**create**; `_resolve_build_prd` failed to resolve it and silently fell back to
"the single workspace PRD". So *"Create shamsu_smoke_note.md"* resolved to
`prd.pdf` and built somebody else's product. Compounded by `_PRD_BUILD_NOUNS`
containing `"it"` matched as a raw substring, so the "names a product" test was
satisfied by almost any English sentence.

**Fix:** a named-but-missing document returns `None` instead of falling through;
nouns match on word boundaries.

> **Reported bug 1, "stale PRD/TaskFlow context leaks", was not a separate bug.**
> Once `prd.build` won the route, injecting the TaskFlow PRD was *correct* for
> that route. Fixing C removed it.

### Defect D — headless never saw slash commands ✅ FIXED

`run_prompt` called `_handle_request` directly; slash dispatch lived in the REPL
input loop. `/run show <id>` reached the model as English, which sent the agent
off to *run* the file named in the prompt. Headless now resolves a read-only
inspection allowlist (`/runs`, `/run`, `/doctor`, `/tasks`, `/permissions`) and
refuses anything else honestly instead of degrading into a model prompt.

### Defect E — two silent failures found while verifying the above ✅ FIXED

- **An explicit tool instruction was ignored.** With A fixed, the web prompt
  stopped mis-routing — and revealed that nothing matched it *at all*. The
  phrase list had `"search the web"` but not `"web search"`, so *"Use web search
  to find the release date"* fell through to the tool-less QA brain and answered
  from stale model memory with the wrong year.
- **Zero web results produced an empty answer reported as `success`.** The
  handler covered "denied" and "error" but not "approved, no error, no hits".

### Second pass (2026-07-21) — the two remaining causes ✅ FIXED

- **Dry-run was deny-mode with a rename.** `_ApprovalScript("dry-run")` computed
  `approved = self._mode == "allow"` (always `False`), so it only recorded
  actions that reached an approval gate — and a create-file agent gives up
  before that, producing zero preview. New `shamsu/safety/dry_run.py`: a
  `DryRunRecorder` (contextvar) plus `AgentToolRegistry.set_dry_run()`. Mutating
  tools now return a *synthetic success* and record a `PlannedMutation`, so the
  agent keeps planning and the run reports what it *would* have done. Commands
  stay denied (real side effects must not be faked). Works via the `--dry-run`
  flag *or* prose ("dry run only: create X") — the flag path is what the dogfood
  used. Verified live: the originally-failing run 4 now plans
  `create dry_run_should_not_exist.txt`, writes nothing, `contract.ok`.
  - *Caught while building it:* `"dry run only"` had been added to the read-only
    regex, so a dry-run create-file hit the hard read-only deny before the
    recorder could preview. A dry run plans a change; it does not refuse one.
    Removed from the ban list; `is_dry_run()` is now its own signal.

- **Run validation was structural, not semantic.** New `shamsu/verify/contract.py`:
  `derive(prompt)` reads the checkable promises (requested paths, read-only,
  scoped, dry-run); `check()` compares them against the **filesystem diff**, not
  the model's self-report. Surfaced as `HeadlessRunResult.contract`; a violation
  flips the CLI exit code to 1. Verified against the verbatim dogfood evidence:
  it fails the run that built the wrong product and the run that destroyed a
  file — both of which `validate_run` had passed as `ok`. **Scoped read-only is
  now enforced too:** `set_allowed_write_paths()` keeps the named target writable
  and denies everything else, so "create X, do not modify any other files" both
  creates X and blocks collateral.

## Still Open

1. Small-file reads are slow (~20s) for near-deterministic work.
2. Approval records came back empty on a run where mutation tools demonstrably
   ran under `--approval allow`.
3. `forget()` on the Graphiti memory path is a stub.
4. The contract only checks what a prompt states outright (named files, no-change
   clauses). It does not judge file *content* correctness — that still needs the
   verify gate or a human.

---

## Verification Of The 2026-07-20 Fixes

Re-run headless against a fresh workspace containing a PRD (the condition that
armed the hijack) plus the same `qa_probe.py`:

| Prompt | Before | After |
|---|---|---|
| `Create a new file named shamsu_smoke_note.md ... Do not modify any other files.` | `prd.build`; built a TaskFlow landing page; requested file never created | `file.write`; **creates exactly that file, nothing else** |
| `Run the command: python qa_probe.py -- then tell me its output. Do not change files.` | ran it, then **overwrote the script with `5`** | reports `Output: 5`; `changed_files: []` |
| `Use web search to find the official Python 3.13 release date ... Do not modify files.` | `status=failed`, `route=file.write`, correct answer buried in a failure message | `status=success`, `route=web` |
| `/runs`, `/run show <id>` | treated as English; agent tried to run a script | returns the actual run table / inspection panel |
| `/nonsense` | reached the model | `Unknown command: /nonsense. Run /help for commands.` |
| `Dry run only: create dry_run_should_not_exist.txt ...` (`--dry-run`) | no preview; agent looked for a nonexistent file and gave up | `planned_mutations: [create ...]`; `changed_files: []`; `contract.ok` |

The user's `qa_probe.py`, destroyed by the original dogfood run, was restored
from SHAMSU's own mutation backup — the rollback ledger did its job.

## Recommended Next Focus

1. **Re-run the full 7-prompt dogfood** as one pass and publish an updated log
   beside `SHAMSU_FRESH_DOGFOOD_2026-07-20.md`. Individual prompts are verified
   above; a single clean end-to-end pass is the acceptance gate.
2. **Wire the contract into interactive runs and `/run validate`**, not just the
   headless result — so a violation is visible in the REPL too.
3. **Re-measure `BENCHMARK.md`** — the current numbers predate these fixes.
4. **Address remaining open items** (slow reads, empty approval records).

Longer-term, still queued: v2.3 phase two, `/autonomy on` real-world trial
before considering it a default, and the final safety audit + release cut.

---

## How To Verify Any Of This Yourself

```powershell
cd F:\Work\PROJECTS\shamsu\Shamsu
.\.venv\Scripts\python.exe -m pytest tests/ -q
.\.venv\Scripts\python.exe -m ruff check shamsu tests scripts
.\.venv\Scripts\python.exe -m evals
.\.venv\Scripts\python.exe scripts\validate_release.py

# Real-prompt dogfood, one prompt at a time, fresh workspace:
python -m shamsu.cli.repl run --workspace <fresh-dir> --prompt "<prompt>" --output json --approval allow
```
