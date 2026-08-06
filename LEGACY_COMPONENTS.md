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

_None yet._

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
| **Decision record** | `docs/decisions/<adr>.md` |
| **Steps 1-10** | ☐☐☐☐☐☐☐☐☐☐ |
| **Migrated in** | `<commit>` |

**Why migrated rather than rewritten from scratch:**

**What was dropped:**
-->

---

## Approved candidates (not yet migrated)

Sourced from plan §8.3. Listing here is *permission to evaluate*, not approval
to import.

| Candidate | v1 location | Target v2 home | Status |
|---|---|---|---|
| Sandbox path validation | `shamsu/safety/` | `security/` | ⚪ Not started |
| Command risk classification | `shamsu/safety/`, `shamsu/tools/executor.py` | `security/` | ⚪ Not started |
| Command timeout handling | `shamsu/tools/executor.py` | `tools/` | ⚪ Not started |
| Model-output normalization | `shamsu/llm/output.py` | `models/` | ⚪ Not started |
| Tool-call salvage / quote repair | `shamsu/llm/output.py` | `models/` | ⚪ Not started |
| Test-output digesting | `shamsu/verify/` | `verification/` | ⚪ Not started |
| Error-signature generation | `shamsu/agents/error_feedback_loop.py` | `verification/` | ⚪ Not started |
| Tool-result truncation | `shamsu/agents/chat_loop.py:90-97` | `context/` | ⚪ Not started |
| Git utility functions | `shamsu/tools/agent_tools.py` | `tools/` | ⚪ Not started |
| Structural code-graph client | `shamsu/abstract/`, `shamsu/retriever/` | `code_intelligence/` | ⚪ Not started |
| Reliability metrics | `shamsu/telemetry/` | `telemetry/` | ⚪ Not started |
| Secret-redaction utilities | `shamsu/safety/` | `security/` | ⚪ Not started |

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
