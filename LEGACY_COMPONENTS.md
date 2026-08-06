# Legacy Component Migration Ledger

Every piece of SHAMSU v1 logic that enters v2 is recorded here — one row per
component, with the evidence that it passed the migration process.

**Nothing may be imported from `legacy-code/` at runtime.** Migration means
*copying or rewriting* logic behind a clean v2 interface, then deleting the
dependency on the old loop.

---

## Migration process (plan §8.2)

A component may enter `src/shamsu/` only after all ten steps pass:

| # | Step |
|---|---|
| 1 | Identify the exact source file and symbol |
| 2 | Review its dependencies |
| 3 | Write isolated tests |
| 4 | Define a clean v2 interface |
| 5 | Copy or rewrite the logic |
| 6 | Remove old-loop dependencies |
| 7 | Document the decision |
| 8 | Pass v2 tests |
| 9 | Pass security checks |
| 10 | Pass evaluation tasks |

---

## Migrated components

Four components crossed in PR 15. Everything else in `src/shamsu/` was written
fresh against the v2 interfaces — where v1 had an equivalent, the row below says
so, because "we rewrote it" and "we never looked" are different claims.

---

### `shamsu.models.normalization`

| Field | Value |
|---|---|
| **v1 source** | `legacy-code/shamsu/llm/output.py` (1,159 lines) |
| **v2 destination** | `src/shamsu/models/normalization.py` (~200 lines) |
| **Method** | rewritten — mostly a deletion |
| **v2 interface** | `shamsu.interfaces.models.ModelClient` (contract parsing seam) |
| **Tests** | `tests/unit/test_migrated_utilities.py::TestNormalisation`, `::TestNoRepair` |
| **Steps 1-10** | ☑☑☑☑☑☑☑☑☑☑ |

**Why migrated rather than rewritten from scratch:** one part of v1's parser is
genuinely hard-won — the balanced-brace scanner that restarts past an
unterminated opening brace. A single left-to-right scan gives up the moment
depth stops returning to zero, so one truncated code fence swallowed everything
after it, and a real 7B reply lost a valid tool call sitting behind a cut-off
Python fence. That behaviour and its reason are carried across verbatim.

**What was dropped:** everything else. Six salvage strategies
(`_salvage_raw_tool_fences`, `_salvage_commented_tool_fences`,
`_salvage_embedded_json`, `_salvage_search_replace`, `_salvage_xml_tool_call`,
`_names_quote_repair_tool`), `_greedy_string_repair`, and
`_repair_unescaped_quotes`. Plan §8.4 names this class of code explicitly.

The line v2 draws: **normalisation removes wrapping and never edits content.**
Removing a `<think>` span or a fence has exactly one correct result. Repairing
an unescaped quote is a guess, and a wrong guess produces a *parseable* wrong
answer — worse than a parse failure, because the failure is visible and a
silently wrong `file.patch` argument is not.

---

### `shamsu.security.secrets`

| Field | Value |
|---|---|
| **v1 source** | `legacy-code/shamsu/safety/commands.py:SECRET_PATTERNS`, `redact`; `safety/audit.py:_redact_data` |
| **v2 destination** | `src/shamsu/security/secrets.py` |
| **Method** | copied verbatim |
| **v2 interface** | plain functions; no protocol needed |
| **Tests** | `tests/unit/test_migrated_utilities.py::TestRedaction` |
| **Steps 1-10** | ☑☑☑☑☑☑☑☑☑☑ |

**Why copied rather than rewritten:** this is the one component migrated
*verbatim*. The patterns are the accumulated result of real leaks, v1's
`test_command_output_secrets_are_redacted` passes against them, and a rewrite
would substitute untested guesses about what a secret looks like for evidence.
Improving them is a task with an evaluation behind it, not a side effect of
moving a file.

**What was dropped:** nothing. `redact_structure` was added, because tool
*arguments* are persisted as JSON on `tool_events` and a secret passed as an
argument never reaches the string path.

**Known limitation, carried deliberately:** the whole match is replaced, so
`password = hunter2` becomes `[REDACTED]` rather than `password = [REDACTED]`.
That loses the signal "a password is configured here". Narrowing the patterns
to capture only the value would mean rewriting every one against no evidence.

---

### `shamsu.security.commands`

| Field | Value |
|---|---|
| **v1 source** | `legacy-code/shamsu/safety/commands.py:classify_command`, `BLOCKED_PATTERNS`, `command_may_write_workspace` |
| **v2 destination** | `src/shamsu/security/commands.py` |
| **Method** | rewritten |
| **v2 interface** | returns `shamsu.interfaces.enums.Risk` |
| **Tests** | `tests/unit/test_migrated_utilities.py::TestCommandRisk` |
| **Steps 1-10** | ☑☑☑☑☑☑☑☑☑☑ |

**Why migrated:** the blocked-pattern list is real operational knowledge.

**What changed, both in the safe direction:**

1. **Unknown commands are `HIGH`, not `MEDIUM`.** v1 commented "unknown
   commands default to requiring approval" — but MEDIUM was also the level for
   `pip install`, so nothing above could tell a routine install from a command
   the classifier had never seen.
2. **Blocked patterns match the raw string, before normalisation**, and that
   ordering is now stated rather than incidental. A normalisation rule that
   accidentally rewrote `sudo rm -rf /` would otherwise downgrade a block to an
   approval prompt.

**Defect found and fixed during migration:** v1's `r"sudo"` was unanchored, so
`python sudoku.py` classified as `BLOCKED`. Safe in direction, but a rule that
fires on nonsense is a rule someone eventually relaxes. Now `\bsudo\b`.

**Not used by `test.run`**, which takes an allowlisted command *key* and never a
string (plan §24.3). This exists for the milestones that must accept a real
command line.

---

### `shamsu.telemetry.metrics`

| Field | Value |
|---|---|
| **v1 source** | `legacy-code/shamsu/telemetry/reliability.py` (788 lines) |
| **v2 destination** | `src/shamsu/telemetry/metrics.py` |
| **Method** | rewritten — the metric definitions are inverted |
| **v2 interface** | queries `StateStore` directly |
| **Tests** | `tests/unit/test_metrics.py` |
| **Steps 1-10** | ☑☑☑☑☑☑☑☑☑☑ |

**Why rewritten:** v1 counted what the loop *told* it — a counter incremented
at the site that believed it had succeeded. `false_success_rate` was therefore
the rate at which the loop *noticed* it had been wrong, which reads zero
precisely when things are worst. Plan §31's metric names were kept; the
computation was not.

**What changed:** every metric is now a query over `tasks`, `evidence`,
`tool_events`, and `failures`. Nothing is incremented by the component being
measured. `_evidence_holds` re-derives the gate result from rows rather than
reading a stored verdict, so it measures whether a conclusion was *earned*
rather than what was concluded.

`repeated_action_rate` counts *consecutive* identical calls rather than total
repeats: reading a file at the start and end of a task is ordinary, reading it
twice in a row is the loop spinning, and v1's total-repeat count was dominated
by the first case.

**The test that earns its place:** `test_a_bypassed_gate_is_caught` writes a
completed task with no evidence straight into the database. `CompletionGate`
cannot produce that state — and a metric that could never report it would be
measuring the runtime's opinion of itself.

---

## Written fresh (v1 had an equivalent)

These were on the §8.3 candidate list. Each was implemented against the v2
interfaces without consulting the v1 code as a source, because the v2 design
constrained the shape more than the old implementation could inform it. Listed
so the candidate list has no silent gaps.

| Candidate | v1 location | v2 home | What differs |
|---|---|---|---|
| Sandbox path validation | `shamsu/safety/sandbox.py` (32 lines) | `security/paths.py` | v2 resolves before deciding and follows symlinks; v1 did neither |
| Command timeout handling | `shamsu/tools/executor.py` | `tools/gateway.py` | Timeout races cancellation rather than running after it |
| Test-output digesting | `shamsu/verify/` | `verification/digest.py` | Keeps both ends of huge output; the exit code decides pass/fail |
| Error-signature generation | `shamsu/agents/error_feedback_loop.py` | `verification/digest.py` | Normalises temp paths, durations, and shifted line numbers |
| Tool-result truncation | `shamsu/agents/chat_loop.py:90-97` | `tools/gateway.py` | Capped *before* entering context, never after |
| Git utility functions | `shamsu/tools/agent_tools.py` | `tools/git.py` | 23 v1 git tools became 2 typed ones with fixed argv |
| Structural code-graph client | `shamsu/abstract/`, `shamsu/retriever/` | `code_intelligence/` | Rebuilt on stdlib `ast`; no external index service |

---

## Not migrated (plan §8.4)

`AgentChatLoop`, the old task lifecycle, the prompt-conversation replay model,
the planner orchestration, the completion logic, the memory orchestration, the
two-registry tool architecture, the inline recovery counters, long-running
mode, and implicit success classification. None of these are in `src/shamsu/`
and none will be.

<!--
Template — copy one block per migrated component.

### `<v2 symbol>`

| Field | Value |
|---|---|
| **v1 source** | `legacy-code/shamsu/<path>:<symbol>` |
| **v2 destination** | `src/shamsu/<path>:<symbol>` |
| **Method** | rewritten \| copied-and-stripped |
| **v2 interface** | `src/shamsu/interfaces/<file>.py:<Protocol>` |
| **Tests** | `tests/unit/<file>.py` |
| **Steps 1-10** | ☐☐☐☐☐☐☐☐☐☐ |
| **Migrated in** | `<commit>` |

**Why migrated rather than rewritten from scratch:**

**What was dropped:**
-->

---

## Candidate list status (plan §8.3)

Every candidate is now resolved. Nothing on this list is still open.

| Candidate | Outcome |
|---|---|
| Sandbox path validation | Written fresh — `security/paths.py` |
| Command risk classification | **Migrated** — `security/commands.py` |
| Command timeout handling | Written fresh — `tools/gateway.py` |
| Model-output normalization | **Migrated** — `models/normalization.py` |
| Tool-call salvage / quote repair | **Rejected** — plan §8.4; see the normalization entry |
| Test-output digesting | Written fresh — `verification/digest.py` |
| Error-signature generation | Written fresh — `verification/digest.py` |
| Tool-result truncation | Written fresh — `tools/gateway.py` |
| Git utility functions | Written fresh — `tools/git.py` |
| Structural code-graph client | Written fresh — `code_intelligence/` |
| Reliability metrics | **Migrated** — `telemetry/metrics.py` |
| Secret-redaction utilities | **Migrated** (verbatim) — `security/secrets.py` |

---

## Known defects in migration candidates

Bugs found in v1 code that is on the candidate list. Migrating any of these
means fixing the defect, not porting it.

### ~~`_FILE_TOKEN_RE` cannot match a POSIX absolute path~~ — FIXED

**Fixed in the archive** at the user's direction. `legacy-code/` is otherwise
not maintained; this was worth an exception because the archive doubles as the
evaluation baseline and a known-broken contract layer makes that baseline
noisier. The v1 suite went from 2349 to 2350 passing with no regressions.

The write-up below is kept because the *lesson* still governs v2.

#### Original defect

| | |
|---|---|
| **Location** | `legacy-code/shamsu/verify/contract.py:40` |
| **Found** | Triaging the v1 baseline failure `test_contract_normalizes_absolute_workspace_target_to_relative_path` |
| **Environment-dependent?** | No. Reproduces deterministically on any POSIX system. |

The pattern is:

```python
r"(?:[A-Za-z]:[\\/])?[\w][\w./\\-]*\.(?:[a-z0-9_]{1,12}|[A-Z0-9_]{1,12})\b"
```

It starts at `[\w]`, so a leading `/` is never captured. `"/tmp/ws/notes.md"`
matches as `"tmp/ws/notes.md"`. It *does* handle Windows drive letters — the
optional `[A-Za-z]:[\\/]` prefix — so this was clearly meant to cover absolute
paths and only half does.

The consequence is downstream, in `_requested_path`:

```python
if workspace is not None and candidate_path.is_absolute():
    return candidate_path.resolve().relative_to(...).as_posix()
```

`is_absolute()` is always `False` for POSIX input, so **the
workspace-relative normalization branch is dead code on Linux and macOS.**

Impact: a prompt saying "Create /home/me/proj/notes.md" records the requested
path as `home/me/proj/notes.md`. Contract verification then looks for that
relative path inside the workspace, does not find it, and reports a violation —
a false failure on a task the agent completed correctly.

#### The fix

```python
r"(?:[A-Za-z]:[\\/]|(?<![\w.])/)?[\w][\w./\\-]*\.(?:...)"
#                     ^^^^^^^^^^^^^ added
```

The lookbehind is load-bearing: a bare `/` alternative made `./notes.md` match
as `/notes.md`, which is a *new* silent corruption of exactly the kind being
fixed. It was caught by testing all four path spellings rather than only the
one the failing test covered.

**For v2:** path normalization belongs in `security/`, must be tested against
POSIX-absolute, Windows-absolute, relative, `./`-prefixed, and `../`-escaping
inputs, and must never derive a path by regex-scraping prose in the first
place. The v2 tool gateway takes typed arguments; a path is a parameter, not
something recovered from a sentence. `PathSandbox` implements this, and its
adversarial suite covers every form above.

---

## Permanently rejected

Per plan §8.4, these must **never** enter v2 as architecture. Rejection is not
revisitable without a superseding decision record.

| Rejected | v1 location | Reason |
|---|---|---|
| `AgentChatLoop` | `shamsu/agents/chat_loop.py` | Model owns the loop; no cancellation path |
| Old main loop structure | `shamsu/agents/chat_loop.py` | Replaced by typed runtime state machine |
| Old task lifecycle | `shamsu/agents/`, `shamsu/tasks/` | Replaced by SQLite-authoritative task state |
| Prompt-conversation replay model | `shamsu/agents/chat_loop.py` | Replaced by the context compiler |
| Old planner orchestration | `shamsu/plans/`, `shamsu/taskmaster/` | Replaced by planning contracts |
| Old completion logic | `shamsu/agents/chat_loop.py` | Replaced by the evidence-gated completion controller |
| Old memory orchestration | `shamsu/memory/` | Graphiti off the critical path; SQLite authoritative |
| Two-registry tool architecture | `shamsu/tools/registry.py`, `agent_tools.py` | Replaced by a single typed tool gateway |
| Inline recovery counters | `shamsu/agents/chat_loop.py:1051-1080` | Replaced by the bounded repair controller |
| Long-running mode behaviour | `shamsu/safety/autonomy.py` | Disabled until evaluations justify it |
| Implicit success classification | `shamsu/agents/chat_loop.py` | Replaced by required/verified evidence |

Reading these files for reference is encouraged. Reproducing their structure is not.
