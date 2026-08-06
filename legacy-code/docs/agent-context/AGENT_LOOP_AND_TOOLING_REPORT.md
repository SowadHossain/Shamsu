# SHAMSU — Agent Loop, Timeout, and Tooling Report

**Commit:** `97bdc5e` — *Enhance PRD handling and routing logic in tests* (2026-08-06)
**Branch:** `mayday-lastresort`
**Author:** Claude (Opus 5), via codebase scan + targeted reads
**Status:** for review by Codex

> Every claim below carries a `file:line` reference against commit `97bdc5e`.
> Verification method and known gaps are stated in §11 — read that before
> treating any inference here as ground truth.

---

## 1. Executive summary

The repo contains **two agent loops, but only one is reachable in production.**

| | `AgentChatLoop` | `ToolCallingAgentLoop` |
|---|---|---|
| File | `shamsu/agents/chat_loop.py` (3546 ln) | `shamsu/agents/tool_calling_loop.py` (229 ln) |
| **Live?** | ✅ `repl.py:15754` — the only call site | ❌ **test-only** (`tests/test_native_tool_calling_agent.py`) |
| Role | Primary interactive/autonomous loop | Narrow "action tool" executor |
| Tools | `AgentToolRegistry` — **42 tools** | `ToolRegistry` — **3 tools** |
| Bound | 8 rounds (50 long-running) | 8 iterations **+ 300 s wall clock** |
| Retrieval | In-loop (`read_file`, `grep_files`, …) | **Explicitly none** |
| Cancel/feedback | **None** | `register_run` / `cancel_event` / feedback queue |

The headline finding: **the entire run-control plane
(`shamsu/runtime/run_control.py` — cancel, feedback injection, in-flight model-task
cancellation) is dead code.** Its only importer is the test-only loop. The
production loop has no mid-run cancellation path, including in long-running mode
at 50 rounds under a 60-minute ceiling. Verified by exhaustive grep — see §11.2.

Read §2 as documentation of a *design that exists in the tree but does not run*,
and §3 as the loop that actually executes.

The design centre of gravity is **defending a small local model from itself**.
Roughly a third of `chat_loop.py` is anti-stall, anti-hallucination, and
output-salvage machinery rather than orchestration.

---

## 2. Loop A — `ToolCallingAgentLoop` (⚠️ test-only; not in the production path)

`shamsu/agents/tool_calling_loop.py`

> **This loop does not run in production.** It is constructed only by
> `tests/test_native_tool_calling_agent.py` (§11.2). Documented here because it is
> the only implementation of run cancellation and feedback injection in the tree,
> and is the obvious donor if that capability is wanted in `AgentChatLoop`.

### Bounds
```
DEFAULT_MAX_TOOL_ITERATIONS = 8       # tool_calling_loop.py:21
DEFAULT_MAX_RUNTIME_SECONDS = 300     # tool_calling_loop.py:22
```

### Structure (`run()`, `tool_calling_loop.py:84-151`)

Per iteration, in order:

1. **Wall-clock check** (`:96`) — `time.monotonic() - started > max_runtime_seconds`
   → `RunStatus.TIMED_OUT`. Checked *before* the model call, so the deadline is
   enforced at iteration granularity, not mid-call.
2. **Cancellation check** (`:100`) — `control.cancel_event.is_set()` → `CANCELLED`.
3. **Feedback injection** (`:104`, `_inject_feedback` `:212-227`) — drains
   `control.feedback_queue` into the message list as high-priority user turns.
4. **Model call** (`_call_model` `:153-168`) — wrapped in an `asyncio.Task` stored
   on `control.current_model_task` so an external `cancel_run` can cancel it
   in flight.
5. **`asyncio.CancelledError` handling** (`:107-113`) — distinguishes *cancel* from
   *feedback interrupt*. If the cancel event is **not** set, the cancellation was a
   feedback interrupt: re-inject and `continue`. This is a genuinely nice touch —
   new user input preempts a running generation without killing the run.
6. **Normalization** (`:116`) — `parse_model_turn()`; see §7.
7. **Terminal check** (`:126`) — no tool calls → `COMPLETED`.
8. **Tool execution** (`:130-144`) — per call, re-checks cancel, executes, appends
   a `role: "tool"` message, re-injects feedback.

### Exhaustion is a failure, not a completion
```python
final = f"I stopped after {self.max_tool_iterations} tool iterations to avoid looping."
complete_run(self.run_id, RunStatus.FAILED, final)     # :145-147
```
Falling out of the loop is `RunStatus.FAILED`. Deliberate and correct — an agent
that used its whole budget without answering did not succeed.

### Retrieval is deliberately withheld
`ACTION_AGENT_SYSTEM_PROMPT` (`:24-35`) states retrieval tools are unavailable
"because Graphiti/Codebase-Memory handles retrieval outside this loop", and the
registry docstring repeats it (`shamsu/tools/registry.py:1-5`). This loop runs
*approved actions only*.

---

## 3. Loop B — `AgentChatLoop` (the primary one)

`shamsu/agents/chat_loop.py`

### Bounds
```
DEFAULT_MAX_TOOL_ROUNDS      = 8      # chat_loop.py:54
LONG_RUNNING_MAX_TOOL_ROUNDS = 50     # chat_loop.py:55
```

Main loop: `for round_index in range(self.max_tool_rounds)` (`chat_loop.py:1081`).

### Per-round sequence (`chat_loop.py:1081-1130+`)

1. `num_ctx = min(ctx_window_for_model(model), _CHAT_MAX_CTX)` (`:1082`)
2. `_refresh_system_prompt()` (`:1083`)
3. `_messages_within_budget(num_ctx, written_files)` (`:1084`) — budget-aware
   history trim
4. Budget indicator + `context.sent` verbose trace (`:1086-1099`)
5. `_chat_with_heartbeat(...)` (`:1102`)
6. `except asyncio.TimeoutError` → **classified** timeout (§5.3)

### Per-run mutable guard state (`chat_loop.py:1051-1080`)

An unusually large amount of state is tracked per run specifically to catch
small-model failure modes:

- `repeated_calls: Counter[(tool, args)]` — byte-identical repeat detection
- `successful_call_signatures`, `successful_read_paths`
- `unconfirmed_failed_writes: dict[str, str]`
- `mutation_recovery_attempts`, `missing_mutation_recovery_attempts`
- `written_files` — feeds the end-of-run verify gate
- `stall_answers` — times SHAMSU answered a "may I / which file" question *on the
  model's behalf*
- `last_failed_read`, `read_recovery_attempts`
- `prose_corrections`, `truncation_recoveries`, `empty_responses`
- `ran_any_tool` — used to classify timeouts (§5.3)
- `nonwrite_tool_succeeded` — marks that a fenced block in the reply is a
  *result*, not a file to write
- `escalation: _RetryEscalation` — raises sampling temperature after a proven
  byte-identical repeat

---

## 4. Loop bound inventory

| Bound | Value | Location |
|---|---|---|
| Chat tool rounds (normal) | 8 | `chat_loop.py:54` |
| Chat tool rounds (long-running) | 50 | `chat_loop.py:55` |
| Action-loop iterations | 8 | `tool_calling_loop.py:21` |
| Action-loop wall clock | 300 s | `tool_calling_loop.py:22` |
| Auto-repair attempts per failed verify | 3 | `chat_loop.py:122` |
| `RepairLoop.max_attempts` | 4 | `repair/loop.py:108` |
| Error-feedback iterations (normal) | 3 | `error_feedback_loop.py:60` |
| Error-feedback iterations (long-running) | 50 | `error_feedback_loop.py:21` |
| Same-error retries before abort | 2 | `error_feedback_loop.py:62` |
| Identical tool-call repeats | 3 | `chat_loop.py:225` |
| Read-failure recoveries | 3 | `chat_loop.py:252` |
| Prose-only corrections | 2 | `chat_loop.py:267` |
| Stall answers | 2 | `chat_loop.py:270` |
| Empty responses | 2 | `chat_loop.py:273` |
| Missing-mutation recoveries | 2 | `chat_loop.py:278` |
| Truncation recoveries | 3 | `chat_loop.py:2959` |
| Quote-repair attempts | 16 | `llm/output.py:108` |
| Empty TTY reads before abort | 3 | `safety/approval.py:20` |

### Autonomy profiles (`shamsu/safety/autonomy.py:20-36`)

```python
AutonomyLimits(max_iterations=3,  max_wall_time_minutes=10,
               max_same_error_retries=2, max_idle_minutes=3,
               max_repair_attempts=3)              # DEFAULT

AutonomyLimits(max_iterations=50, max_wall_time_minutes=60,
               max_same_error_retries=3, max_idle_minutes=5,
               max_repair_attempts=25)             # LONG_RUNNING
```

Persisted per-workspace at `.shamsu/autonomy.json`, **off by default**
(`is_long_running_enabled` → `False` when absent, `:43-51`). The module docstring
is explicit that higher ceilings must be "tried and trusted before ever becoming
the default for a given workspace."

`autonomy_limits()` (`:64-82`) clamps every field with `max(1, int(...))` and
falls back to defaults on malformed JSON — a corrupt config degrades to safe
limits rather than raising.

---

## 5. Timeout architecture

Three independent layers. This is the most carefully-reasoned part of the
codebase and the comments explain *why*, not just *what*.

### 5.1 Transport layer — `shamsu/llm/manager.py:70-88`

```python
LLM_CONNECT_TIMEOUT_SECONDS = _timeout_env("SHAMSU_LLM_CONNECT_TIMEOUT", 15.0)
LLM_IDLE_TIMEOUT_SECONDS    = _timeout_env("SHAMSU_LLM_IDLE_TIMEOUT",   180.0)
LLM_TOTAL_TIMEOUT_SECONDS   = _timeout_env("SHAMSU_LLM_TIMEOUT",        600.0)
```

Two distinct `httpx.Timeout` profiles:

- **Streaming** (`_streaming_timeout()`, `:75-83`) — **no total cap**. `read` is
  the max *silence between tokens*. Rationale (`:61-69`): as long as tokens keep
  arriving the call continues regardless of total generation time (slow CPU box
  is fine); silence past the idle window means stalled/deadlocked.
- **Blocking** (`_blocking_timeout()`, `:86-88`) — bounded 600 s overall for the
  single non-streamed path (tool calls), short connect to fail fast.

`LLMStalledError` (`:97-99`) surfaces an idle-window stall as a named condition
instead of a raw `httpx` timeout.

`_timeout_env` (`:53-58`) rejects non-positive and unparseable values, falling
back to the default — env misconfiguration cannot produce a zero timeout.

### 5.2 Orchestration layer

```python
_MODEL_CALL_TIMEOUT_SECONDS  = int(env("SHAMSU_MODEL_TIMEOUT_SECONDS", "120"))         # chat_loop.py:61
_REPAIR_MODEL_TIMEOUT_SECONDS= int(env("SHAMSU_REPAIR_MODEL_TIMEOUT_SECONDS", "120"))  # chat_loop.py:62-63
```

Applied via `asyncio.wait_for` (`:873`, `:955`, `:2164`).

**Note a layering tension:** the orchestration cap (120 s) is *below* the transport
idle timeout (180 s). A model streaming slowly but healthily is cut off by
`asyncio.wait_for` at 120 s before the transport's stall detector ever fires. The
transport's carefully-reasoned "progress keeps it alive" property is therefore
not observable through the chat loop's default path. Flagged for review — this may
be intentional (interactive responsiveness) but the comments at
`manager.py:61-69` read as though the idle-timeout semantics are the operative
ones.

**Heartbeat** — `_chat_with_heartbeat` (`:880-905`) emits a periodic "still
waiting" message (default 15 s, `SHAMSU_LLM_HEARTBEAT_SECONDS`, `manager.py:677`)
so a slow local model reads as working rather than frozen. The docstring is
explicit: "The timeout is unchanged."

### 5.3 Timeout classification — `chat_loop.py:456-460`, `:2235-2247`

Rather than reporting "timeout", the loop classifies it:

```python
TIMEOUT_LLM_NO_FIRST_TOKEN         = "llm_no_first_token_timeout"
TIMEOUT_LLM_GENERATION             = "llm_generation_timeout"
TIMEOUT_PLANNER_STALL              = "planner_returned_but_executor_stalled"
TIMEOUT_TOOL_EXECUTION             = "tool_execution_timeout"
TIMEOUT_TOOL_MISSING_AFTER_PROMISE = "tool_call_missing_after_promise"
```

`_timeout_category(round_index, ran_any_tool)` (`:2235-2247`):

- round 0, no tools, no plan → `NO_FIRST_TOKEN` (genuine model stall)
- round 0, no tools, plan produced → `PLANNER_STALL` (planner worked, executor didn't)
- any later round → `GENERATION` (mid-run)

Docstring states the intent plainly: stop "blaming the GPU" when the model already
produced a plan. The category is emitted to trace, session log, and audit
(`:1107-1126`).

### 5.4 Subprocess layer — `shamsu/tools/executor.py`

```python
timeout_seconds: int = 120      # executor.py:51
TIMEOUT_EXIT_CODE = 124         # executor.py:30
BLOCKED_EXIT_CODE = 126         # executor.py:28
```

Timed-out commands return exit 124 with captured partial stdout
(`:210-243`), matching GNU `timeout(1)` convention. Blocked commands return 126.

### 5.5 MCP layer — `shamsu/mcp/config.py:37`

`timeout: float = 30.0`, validated `> 0` (`:69-70`). Connection setup adds
headroom: `config.timeout + 5.0` (`manager.py:244`); calls use `+ 2.0`
(`manager.py:276`).

---

## 6. Tool handling

### 6.1 Two registries, two surfaces

**`ToolRegistry`** (`shamsu/tools/registry.py`) — 3 tools, action-only:
`run_safe_command`, `django_setup`, `django_test` (`:48-86`).

**`AgentToolRegistry`** (`shamsu/tools/agent_tools.py`, 3731 ln) — **42 tools**:

| Group | Tools |
|---|---|
| Filesystem read | `list_files`, `read_file`, `file_info`, `find_file`, `grep_files` |
| Filesystem write | `write_file`, `edit_file`, `append_file`, `move_file`, `delete_file` |
| Execution | `run_command` |
| Retrieval | `search_index`, `search_docs`, `ask_docs`, `ingest_docs`, `summarize_docs` |
| Web | `web_search`, `fetch_url` |
| Interaction | `ask_user` |
| Git (23) | `git_status`, `git_status_full`, `git_diff`, `git_diff_file`, `git_diff_staged`, `git_add`, `git_add_all`, `git_commit`, `git_push`, `git_pull`, `git_fetch`, `git_log`, `git_branch`, `git_branches`, `git_checkout`, `git_create_branch`, `git_init`, `git_remote`, `git_restore`, `git_stash_push`, `git_stash_pop`, `git_stash_list`, `git_unpushed_commits` |

### 6.2 Validation before execution — `registry.py:98-114`

`validate_arguments()` checks: tool exists → args are a dict → all `required`
present and non-empty → no unexpected keys → declared string types are strings.
`execute()` re-validates (`:117-119`) rather than trusting the caller.

Tool-call arguments are declared as **strings even when semantically numeric**
(e.g. `limit` → `{"type": "string", "default": "20"}`, `agent_tools.py:457-461`),
then coerced via `_as_int(..., minimum=, maximum=)`. This accommodates small models
that emit `"20"` instead of `20`. Coercion is clamped, so a garbage value cannot
produce an unbounded read.

### 6.3 Execution gating — `agent_tools.py:964-990`

Two gates before dispatch:

1. **Allow-list** — `_tool_is_allowed(name)`; when an orchestrated step restricts
   tools, the denial returns the allowed set so the model can self-correct.
2. **Required prefix** — if a step demands e.g. a `git_*` tool, substituting
   another is blocked with an explicit "do not substitute" message. `ask_user` is
   always exempt.

Both return a structured `ToolResult(False, …)` rather than raising — the loop
sees a failed tool result and can recover.

### 6.4 MCP delegation — `agent_tools.py:990`

`if name.startswith("mcp__"): return self._execute_mcp(name, arguments)` —
external MCP tools are namespaced and routed to `MCPManager`.

`MCPManager` (`shamsu/mcp/manager.py:207+`) runs an `_AsyncRuntime` on a dedicated
thread (`:43-69`) with `submit(coro, timeout)` bridging sync→async. Supports stdio
and SSE transports (`_open_transport`, `:102-146`).

### 6.5 Tool-result token capping — `chat_loop.py:90-97`

```python
_TOOL_RESULT_MAX_TOKENS = int(env("SHAMSU_TOOL_RESULT_MAX_TOKENS", "2000"))
```

The comment states the failure mode precisely: the budget-aware trimmer always
keeps the most recent message, so one oversized `read_file`/`grep_files` result
survives trimming and crowds out everything else. Results are therefore capped
**before entering history**, and the model is told how to see more (narrower
range/query). This is the single most important token-discipline mechanism in the
loop.

### 6.6 Ledger accounting — `tool_calling_loop.py:179-194`

Every call logs `log_tool_call` → `log_tool_result` with `original_tokens`,
`returned_tokens`, `max_tokens`, `truncated`.

**Finding:** in `ToolCallingAgentLoop` these are hardcoded
`original_tokens == returned_tokens`, `max_tokens=0`, `truncated=False` (`:190-193`)
— that loop does not truncate, so the fields are structurally present but carry no
signal. Ledger consumers must not read `truncated` as authoritative across both
loops. See §11.3.

---

## 7. Model output normalization — `shamsu/llm/output.py`

`parse_model_turn(response, registered_names, allow_salvage=True)` (`:162+`) is the
**single normalization boundary**, shared by both loops (`tool_calling_loop.py:116`
notes "same parser as the chat loop").

1. **Native `message.tool_calls`** when present → `salvaged=False` (`:189`).
2. Otherwise a **salvage cascade** (`:206-219`), in deliberate order:
   `_salvage_raw_tool_fences` → `_salvage_commented_tool_fences` →
   `_salvage_embedded_json` → `_salvage_search_replace` → `_salvage_xml_tool_call`

   The ordering is load-bearing: the comment at `:209-211` notes that if
   `_salvage_embedded_json` ran first it would brace-scan a `.json`/`.js` payload
   and mis-parse it.

3. **Registered-name gating** — salvaged calls are checked against registered tool
   names so a name that isn't registered is treated as an example, not a call
   (`:171-173`). Native calls always pass through, "so an unknown tool still
   surfaces honestly" (`tool_calling_loop.py:77-78`).

4. **Quote repair** for `write_file`/`append_file`/`edit_file` payload keys
   (`:100-108`), bounded at 16 attempts / 400 000 chars.

5. **Thinking-tag stripping** — `_THINK_RE` and `_DANGLING_THINK_RE` (`:112-115`);
   the dangling variant handles an unterminated `<think>` at truncation.

This layer exists because small local models frequently emit tool calls as prose,
fenced JSON, XML, or search/replace blocks rather than native calls.

---

## 8. Anti-stall and honesty guards (`chat_loop.py`)

Distinctive to this codebase; grouped by failure mode:

| Failure mode | Guard | Ref |
|---|---|---|
| Byte-identical repeated call | `repeated_calls` Counter + `_RetryEscalation` raises temperature | `:225`, `:1080` |
| "I'll read X next" without reading | `_READ_STALL_PHRASES`, bounded recoveries | `:234`, `:252` |
| Prose instead of a tool call | `_PROSE_ONLY_CORRECTION` | `:267`, `:2492` |
| Empty response | `_EMPTY_RESPONSE_CORRECTION` | `:273`, `:297` |
| Claims a write that didn't happen | `_MUTATION_PROMISE_RE`, `unconfirmed_failed_writes` | `:2817`, `:1054` |
| Claims failure that didn't happen | `_FALSE_FAILURE_RE` | `:442-446` |
| Asks permission instead of acting | `_PLANNER_PERMISSION_RE`, `_PERMISSION_QUESTION_RE`, bounded stall answers | `:2712`, `:3178`, `:270` |
| Truncated output | `_TRUNCATION_ERROR_MARKERS`, bounded recoveries | `:2962`, `:2959` |
| Signs off on unverified build | `_VERIFY_GATE_ENABLED` + `written_files` | `:142`, `:1058` |

### The verify gate — `chat_loop.py:142`

After an **autonomous** (long-running) run that wrote files, a deterministic
lightweight verifier runs once "so the loop never signs off on a build it never
checked (small models routinely hallucinate success)." Off for interactive chat
(user is in the loop); disable via `SHAMSU_VERIFY_GATE=0`.

### Whole-file overwrite default — `agent_tools.py` (`write_file` dispatch)

The model-facing `write_file` **always overwrites**, with an explicit rationale:
"small models forget an overwrite flag, get blocked, and then hallucinate
success." The internal `overwrite` param remains for programmatic callers. This
trades safety-by-default for honesty-by-default — worth an explicit review
decision (§11.4).

---

## 9. Safety pipeline — `shamsu/tools/executor.py`

Every command passes, in order (`:150-196`):

1. `classify_command(command)` → `CommandRisk`
2. `BLOCKED` → exit 126, session log + audit log, no execution (`:151-164`)
3. `MEDIUM` → `ApprovalManager.ask(ApprovalRequest)`; denial is logged and
   returned (`:166-189`)
4. Execute with `timeout=self.timeout_seconds` (`:210`)
5. Non-zero exit → attach `DiagnosticDigest` packet (`registry.py:149-150`)

`preflight` (`:356-397`) mirrors the same gates *without executing* — used to
validate and obtain approval ahead of time.

**Arbitrary Python is separately blocked** — `_looks_like_arbitrary_python`
(`registry.py:205-210`) rejects `python -c` and bare `python -` via two regexes
covering quoted and unquoted forms, returning exit 126 before the risk classifier
runs.

Path containment is `Sandbox.validate()` (fan-in 75 — the most-called safety
primitive in the codebase).

---

## 10. Repair and feedback loops

### `ErrorFeedbackLoop` — `shamsu/agents/error_feedback_loop.py`

Test → fix → re-test for generated Django projects.

- **Early exit**: tests pass before any fix → return immediately (`:92-95`)
- **Error-signature guard** (`:99-113`): `_error_signature()` (`:218-229`) builds a
  fingerprint from structured failures, falling back to the first 8
  error/failure/traceback lines. Same signature more than
  `max_same_error_retries` (2) → abort. Catches a model looping on an unfixable error.
- **Stall detection** (`:148-161`, non-long-running only): if `failed >= previous_failed`
  after a fix, abort. No improvement is treated as failure.
- **Iteration ceiling** is explicitly a *backstop*, not the normal stop condition
  (`:18-21`): "stall detection is what actually catches a fix attempt that isn't
  working; this just bounds worst-case iteration count."
- **Digest-before-LLM** (`:187-198`): raw test output is parsed into a compact
  `ErrorPacket` *before* reaching the bugfix model — "never the other way around."
  Wrapped in a bare `except Exception` that falls back to an empty packet, with a
  `pragma: no cover` noting it must never block the loop.

### `RepairLoop` — `shamsu/repair/loop.py`

`max_attempts=4` (`:108`). Stops early on: no actionable root error, no actionable
plan, or a target path outside the workspace (`:151-175`). Uses
`RepeatedActionBlocker`, `ErrorComparator`, and `TransactionWorkspace` (`:130-133`).
The `chat_loop` caller caps it lower at 3 (`:122`) with the rationale that the
ceiling "must stay small: the point is to rescue a one-line syntax error at the
end of a long autonomous run, not to let a model grind at a problem it cannot solve."

---

## 11. Verification method, gaps, and review flags

### 11.1 What I actually verified

- **Read in full:** `tool_calling_loop.py`, `error_feedback_loop.py`,
  `run_control.py`, `autonomy.py`, `registry.py`
- **Read in part:** `chat_loop.py` (constants `54-145`, `225-283`, `440-510`;
  loop body `1050-1130`; classification `2235-2247`), `manager.py` (`50-160`),
  `agent_tools.py` (`409-470`, `964-1060`), `repair/loop.py` (`100-175`),
  `executor.py` (structure via grep)
- **Grep/graph only:** `output.py`, `mcp/manager.py`, `context/budget.py`,
  `orchestrator.py`
- Line numbers spot-checked against `97bdc5e` before writing.
- §11.2 verified by **exhaustive** repo-wide grep for `register_run|cancel_event|
  run_control|complete_run|add_feedback` and for both loop class names, not by
  sampling. That check is reproducible in one command and should be the first
  thing a reviewer re-runs.

**Structural queries were answered from the `codebase-memory-mcp` knowledge graph**
(project `home-shamsu-Shamsu`, 161 071 nodes / 238 995 edges) rather than by
reading files, which is why a 21 000-line surface could be characterised without
loading it. Fan-in figures quoted in §9 and elsewhere come from the graph's
`get_architecture` output.

### 11.2 🔴 The cancellation machinery is unreachable in production

**Verified by exhaustive grep, not inference.**

`shamsu/runtime/run_control.py` implements `register_run`, `cancel_run`,
`add_feedback`, `complete_run`, and in-flight model-task cancellation — the whole
control plane described in §2.

Every importer of that module, repo-wide:

```
shamsu/agents/tool_calling_loop.py:16
```

That is the only one. And `ToolCallingAgentLoop` itself is instantiated **nowhere
in `shamsu/`** — the sole construction site outside its own module is
`tests/test_native_tool_calling_agent.py`.

Meanwhile the live loop is instantiated exactly once:

```
shamsu/cli/repl.py:15754:  result = await AgentChatLoop(workspace, **chat_kwargs).run(user_input)
```

`chat_loop.py`'s only occurrence of "cancel" is `beat.cancel()` at `:984` — that
cancels the *heartbeat* task, not the run.

**Consequences:**

1. `run_control.py` is dead code in production. `cancel_run` / `add_feedback` can
   never be invoked against a real run.
2. The live `AgentChatLoop` has **no mid-run cancellation path** — including
   long-running mode at 50 rounds under a 60-minute autonomy ceiling.
3. The mid-generation feedback-interrupt behaviour (§2 step 5), which is the
   nicest design in the loop layer, ships to no one.
4. CLI `KeyboardInterrupt` handling (`repl.py:16987`) sits at the prompt-read
   boundary (`except (EOFError, KeyboardInterrupt)`), not around an in-flight run.

The two loops therefore are not "two live paths" — one is production, one is
test-only. Whether `ToolCallingAgentLoop` is an abandoned earlier design or an
unfinished newer one is a question for whoever wrote it; the cleaner control model
suggests the latter, in which case porting `register_run` into `AgentChatLoop`
is the single highest-value fix available.

### 11.3 ⚠️ Ledger truncation fields are not uniformly meaningful

`tool_calling_loop.py:190-193` hardcodes `truncated=False`, `max_tokens=0`. The
chat loop *does* truncate (`_TOOL_RESULT_MAX_TOKENS`). Any analytics reading
`truncated` across both loops will undercount.

### 11.4 Review decisions worth making explicit

1. **120 s orchestration cap vs 180 s idle timeout** (§5.2) — the transport's
   progress-based liveness design is masked by the tighter `asyncio.wait_for`.
   Intentional?
2. **`write_file` always overwrites** (§8) — deliberate, documented, but a
   destructive default chosen to prevent hallucinated success.
3. **Two loops, one live** — resolved during this scan; see §11.2.
   `ToolCallingAgentLoop` is test-only. Decide explicitly: port its control plane
   into `AgentChatLoop`, or delete it and `run_control.py` together. Leaving a
   polished-but-unreachable loop in `agents/` invites future readers (human or
   model) to mistake it for the production path — as this report initially did.
4. **`error_feedback_loop.py:197` bare `except Exception`** — justified in comments
   and `pragma: no cover`'d, but it swallows every digest failure silently.

### 11.5 Not covered

`shamsu/cli/repl.py` (**17 411 ln**), `freeform_generator.py` (3911 ln),
`full_pipeline.py`, `plan_mode.py`, `task_harness.py`, `planner.py`,
`scaffold_pipeline.py`, `council.py`, the PRD pipeline, and
telemetry/observability were **not** examined.

Two follow-up passes are warranted:

- **`repl.py`** — at 17 411 lines it is by a wide margin the largest module in the
  repo and hosts the sole `AgentChatLoop` call site. Anything wrapping that call
  (interrupt handling, run lifecycle, approval plumbing) lives here and is
  unreviewed. If a cancellation path exists anywhere, this is where to look
  before accepting §11.2 as final — though note that `chat_loop.py` itself
  contains no `cancel_event` check, so any external cancellation could only act
  between rounds at best.
- **`freeform_generator.py`** — 3911 lines, almost certainly contains loop logic
  not reflected in §4's bound inventory.

The bound inventory in §4 should therefore be read as **complete for the modules
listed in §11.1, not for the package.**
