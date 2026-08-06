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
