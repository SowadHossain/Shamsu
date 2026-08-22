# SHAMSU Progress Log

One entry per completed task, newest at the top. Raw model/test output lives in
`logs/test-runs/<date>-<task>.log`.

### 2026-08-22 - Every structured call returned "", and the live console never turned on
Files edited: `shamsu/llm/manager.py`, `shamsu/cli/repl.py`,
`scripts/install.ps1`, `tests/test_structured_generation.py` (new),
`tests/test_live_console.py`
What changed: two unrelated bugs from one reported session. (1) `/plan` wrote
`_No steps were produced._` twice while the CLI printed the model's reasoning
and the reasoning WAS the plan JSON. A `format` grammar constrains Ollama's
`response` channel only, and omitting `think` is not neutral - this model
defaults to thinking ON, so the schema was satisfied inside `thinking` and
`response` came back empty. Measured on the real PLAN_SCHEMA: think omitted ->
0 chars, `think: true` -> 0 chars, `think: false` -> 561 chars and a plan.
SHAMSU sent the two failing shapes and never the third, so on the default
tier's own anchor EVERY `generate_structured` caller was returning "" - the
planner, the router, the PRD development plan, the repair proposer, the
scaffold filler. The flag is now sent explicitly both ways and never asks for
thinking on a constrained call, plus a narrow salvage that reads the trace when
a schema call comes back empty. (2) The live console announced "stdin is not a
terminal" in a process where rich was painting panels at true console width and
prompt_toolkit was reading keys - on Windows prompt_toolkit reads the console
buffer through Win32, not `sys.stdin`, so `isatty()` was the wrong question.
The gate now asks rich and prompt_toolkit directly, `SHAMSU_LIVE_FEEDBACK=1`
forces it on, and the launcher stops enumerating `$input` (which engaged
PowerShell's pipeline machinery and redirected the child's stdin) unless
`[Console]::IsInputRedirected` says something was really piped. One existing
test had asserted the isatty behaviour by name and so guaranteed the bug;
repointed to what its name claims.
Tests: 10 new in `tests/test_structured_generation.py`, 6 new + 1 repointed in
`tests/test_live_console.py`; full suite 3640 passed, 2 skipped. Verified live:
the exact prompt that produced 0 steps twice now produces a grounded 5-step
plan |
Log: logs/test-runs/2026-08-22-structured-empty-response.log

### 2026-08-22 - A pinned input line, a telemetry toolbar, and a side dispatcher
Files edited: `shamsu/cli/live_console.py` (new), `shamsu/cli/repl.py`,
`shamsu/agents/simple_feedback.py`, `shamsu/safety/approval.py`,
`tests/test_live_console.py` (new)
What changed: console fixes 01-04 of the "Harness and Console" spec. Deleted
`_LiveFeedbackReader` - forty lines of raw `msvcrt.getwch()` that echoed
nothing, backspaced a buffer you could not see, and sent every keystroke to the
model. The pinned input box was previously ruled out as fatally conflicting
with that thread on Windows; the thread was the only reason the conflict
existed, and `prompt_toolkit` already owned every other input surface here
(idle prompt, approvals, control console). One `PromptSession` on the REPL's
own loop under `patch_stdout` replaces it, `console.status` retires into
prompt_toolkit's `bottom_toolbar` so nothing fights for the bottom line, a
leading `/` now runs locally and never enters the message array, and
`/queue add` lines up work that waits for the turn instead of interrupting it.
The toolbar narrows from the LEFT: clipping trimmed the end, where the queue
depths are, so the first thing to vanish on a narrow terminal was the number
telling you your steer had been accepted. `reading_input()` in
`safety/approval.py` gained an observer list so the console is handed over
SYNCHRONOUSLY before an approval question is drawn - polling `prompt_is_active`
alone left a window in which two prompt_toolkit applications were live on one
terminal. Harness fixes 05-08 are not in this change. Live 3B run:
`/context status` typed mid-turn was answered locally and never reached the
transcript; the steer did reach the model at a round boundary. Not fixed here,
but exposed by that run: the turn reported SUCCESS having never written a file.
A startup line now states whether the live console is on, or names the reason
it is off: everything here happens DURING a turn, so a fresh REPL was
indistinguishable from an old one until you sent a prompt.
Tests: 66 new in `tests/test_live_console.py`, incl. one that drives the real
`PromptSession` over `create_pipe_input()`; full suite 3624 passed, 2 skipped |
Log: logs/test-runs/2026-08-22-live-console-tui.log

### 2026-08-22 - Old chat-logs replayed into the session log, redacted
Files edited: `shamsu/ui/chatlog_migrate.py` (new), `shamsu/ui/turnlog.py`,
`shamsu/cli/session_commands.py`, tests
What changed: `/logs migrate` reads the unredacted `.shamsu/chat-logs/*.md` left
behind by the deleted `simple_log.py` and replays them through `TurnLogWriter`,
so the copy comes out redacted, spilled and identical in shape to a log written
today. Originals are never touched - they may be the only record of a session
someone wants, and deleting them is the user's call. Two parser traps: the
model's own replies contain markdown headings (`## Project Review Summary`),
so only exact known prefixes are structural and only outside a fence; and the
old writer grew its fences to survive backticks, so a closer must be at least
as long as its opener. Three defects found while building it: every tool was
counted twice (it appears in both `tool calls requested` and its own result
section), a conversation split across two files had one half silently dropped
(the old writer opened a new file when a session gained a title - `test1` has
turn 1 and turn 2 in separate files, and alphabetical order put turn 2 first),
and a response whose `prompt sent` header was missing dropped the whole round.
Ran on 24 real files across 13 workspaces: 23 migrated, 36 turns, 1 correctly
skipped (a session with a live log), 0 errors, 0 secret-pattern hits in 46
migrated files.
Tests: +12 in `tests/test_trace_output.py`, full suite 3558 passed.

### 2026-08-22 - The model was copying our own elision marker into its patches
Files edited: `shamsu/agents/simple_chat.py`, `shamsu/tools/agent_tools.py`,
`shamsu/cli/repl.py`, tests
What changed: diagnosed a reported collapse on qwen3.5:9b. Not context pressure -
`_shorten_arguments` cut any argument over 100 chars to 80 chars plus
" ...[elided]", and applied that to the model's own past `patch_file` calls,
whose `old_string` is always longer than that. The model retried by copying the
stub out of its own history, marker and all, and the tool could only answer
"old_string not found" - the same function attempted three times, the third
carrying our marker as if it were code. Content arguments are now DROPPED rather
than trimmed (the keys stay, so the call still reads as an action), and
`edit_file` refuses an incoming argument carrying either marker with a message
that sends the model to `read_file` instead of to another identical retry.
Also fixed the telemetry behind the same report: `SESSION_COUNTERS` was a
module-level global whose docstring claimed "per session", so resuming a thread
reported the previous thread's numbers as its own. Now keyed by session id with
an explicit active pointer. `/context meter` estimates from the transcript on a
cold resume, and `/context status` no longer advertises 256k for a model running
in a 32k window.
Tests: +14 in `tests/test_simple_chat.py`, full suite 3546 passed.
Log: logs/test-runs/2026-08-22-elision-poisoning-and-telemetry.log

### 2026-08-21 - SECURITY: unredacted prompts on disk; chat-logs/ removed
Files edited: `shamsu/agents/simple_log.py` (deleted), `shamsu/agents/simple_chat.py`
(20 call sites), `shamsu/action_ledger/ledger.py`, `shamsu/ui/turnlog.py`,
`shamsu/cli/repl.py`, tests
What changed: `simple_log.py` contained no calls to `redact` at all - it wrote
the exact prompt and raw reply for every turn to `.shamsu/chat-logs/<session>.md`
at every log level, the one path in the project that put model text on disk
without going through the shared secret-pattern list. Proven by running a turn
whose prompt contained a fake key and searching every file under `.shamsu/`;
that search found a SECOND leak nobody knew about, `model-transcript.csv`, whose
markdown twin redacts and which wrote its row dict straight out. Both fixed.
The module is deleted and `log_turns`/`SHAMSU_NO_CHAT_LOG`, which then
controlled nothing, went with it. `log-detailed.md` now keeps the prompt at
every level (it was verbose-only) so deleting the leak did not take the record
with it - redacted, with the overflow rule spilling a large prompt to
`attachments/`. A startup panel warns any workspace that still has the old
folder, and deliberately does not delete it.
Tests: `tests/test_trace_output.py` 47 -> 57, nine removed with the module,
full suite green.
Log: logs/test-runs/2026-08-21-chat-logs-redaction-leak.log

### 2026-08-21 - The CLI paints a turn instead of listing it
Files edited: `shamsu/cli/turn_render.py` (overhauled), `shamsu/runtime/turn_stream.py`,
`shamsu/agents/simple_chat.py` (emits only), `shamsu/cli/repl.py`, tests
What changed: every action row used to be `[dim]{text}[/dim]` - one grey for a
successful read and a failed `run_tests` alike. Rows now carry an icon, a verb,
the file, a duration and a loud red FAILED; writes show a colourised diff
snippet; reasoning renders dim and italic; approvals are announced and answered
in yellow; the turn ends on a SUCCESS/FAILED badge, which was previously not
printed at all. A run of identical calls collapses to one row plus `x8`, across
the model replies between them - the shape a real contract loop has. The
spinner names the current action and carries `ctx 68% (22.3k/32.8k) | rnd 4/24`,
both numbers that already existed and were displayed nowhere. Driven from the
turn stream, NOT the ActionLedger: the ledger swallows exceptions by design and
has no status events, so a UI on it would go silently blank. The intent behind
that request is kept as a three-way parity test instead. No `rich.Live`, no
pinned input - `simple_feedback.py` reads raw keystrokes on the same terminal.
Five defects were found by painting a turn to a real console, none of them
visible to a unit test; the worst was that the collapse rule as first written
would never have fired on the run it was written for.
Tests: `tests/test_turn_stream_parity.py` 28 -> 44, full suite green.
Log: logs/test-runs/2026-08-21-cli-rich-renderer.log

### 2026-08-21 - Logging: two Markdown files per session, replacing eight typed folders
Files edited: `shamsu/ui/turnlog.py` (new, replaces `shamsu/ui/narrative.py`),
`shamsu/action_ledger/{ledger,store}.py`, `shamsu/agents/simple_chat.py`,
`shamsu/cli/{request_lifecycle,session_commands,noninteractive}.py`,
`shamsu/ui/trace.py`, `shamsu/integrations/telegram/sessions.py`, tests
What changed: a session now carries `log-summary.md` (every action, one line
each), `log-detailed.md` (the same sequence with prompts, diffs, output and
reasoning attached under anchors the summary links to) and a flat
`attachments/` for anything too large to inline, beside `session.json` and
`messages.jsonl`. The eight typed subfolders under `.evidence/` collapsed into
one flat `attachments/`, with the kind moved into the filename. Five additions
the old `report.md` could not show: reasoning as a collapsed sub-panel inside
the model's own entry (including `<think>` blocks leaked into the answer),
approvals as their own row with request and resolution paired, consecutive
attempts on one file grouped as "1 of 2 kept", a surface badge per row, and an
overflow rule at 2,400 chars. The first live run found that SIMPLE MODE - the
default path - never logged its tools or model calls to the ledger at all, so
the log came out with approvals and file writes in it and no sign of what the
agent ran; that is now wired.
Then aligned to the Turn Log Viewer artifact once it could be read (downloaded;
the fetch is blocked because it is shared-with rather than owned). Its governing
rule - "log-summary.md is deliberately titles-only, each line is a link, not a
description" - meant a tool call and its result became ONE row instead of two,
plus: "Building context" as a row, system notices in both files, a verdict
reason, and the mockup's header format. The next live run then exposed a third
simple-mode ledger gap: `replace_symbol` and `append_file` are executed by the
loop rather than the registry, so a real edit never reached `changed_files` -
calc.py was correctly fixed and the run closed `failed` with nothing recorded.
Tests: 47 in `tests/test_trace_output.py` (17 before), full suite 3516 passed /
2 skipped. Seven live runs on qwen2.5-coder:3b-instruct.
Log: logs/test-runs/2026-08-21-logging-refactor.log
Spec: docs/reference/turn-log-mockup.md (gitignored, local)

### 2026-08-21 - The reported failure, reproduced on the user's own file and fixed
Files edited: `shamsu/agents/simple_chat.py`, `shamsu/agents/simple_outline.py`,
`evals/{harness,diff,cases,__main__}.py`, tests
What changed: ran qwen3.5:9b against `test-shamsu/test1/js/main.js` - 582 lines,
over the write cap - and reproduced the report exactly: 24 rounds, 319s, nothing
changed. The file's defect was never a syntax error; four functions are declared
TWICE and in JavaScript the later one silently wins, which no parser can see.
The transcript named the cause: the model had diagnosed it correctly and tried
`replace_symbol(symbol='handlePauseMenuAction(action)', content='
')` - delete
the duplicate - and was refused twice, because the symbol carried its signature
and empty content read as a missing argument. Fixed, along with: 17 of 23
contract failures (the model sends `assertion_index`, the code reads
`assertion_id`), overlapping re-reads, `read_symbol` returning only the first of
several definitions, and `run_command` being silently denied to any caller using
`approval_override`.
Then a REGRESSION I introduced: allowing deletion by name let two legal steps
remove every definition of a function. `replace_symbol` was exempt from the
erasure guard on the assumption `_members_lost` covered it; it does not. Fixed
in a574b46 and the exact live sequence is now refused at step two.
Tests: full suite 3319 -> 3425 passing.
Log: logs/test-runs/2026-08-21-real-file-repair.log

### 2026-08-21 - Efficiency is now measurable, and the read-loop was variance
Files edited: `evals/harness.py`, `evals/diff.py`, `evals/cases.py`,
`evals/__main__.py`, `tests/test_evals_diff.py`
What changed: `EvalResult` carries `rounds` and `tool_calls`, read from the
session transcript rather than reported by a driver. `evals.diff` prints both
per ATTEMPT and BELOW the line - the verdict never reads them, so getting
cheaper while breaking a solid case is still REGRESSED and spending more rounds
to fix a case you used to fail is still IMPROVED. Added
`removes_duplicate_definitions_without_losing_anything`, which reproduces the
real file's shape; its check counts declarations INCLUDING INDENTED ONES.
THE LESSON, twice over in one day: a `grep "^function"` reads a nested
definition as absent. That produced a false data-loss alarm on the user's file
(I told them not to apply a correct fix) and then missed a real one the next
run. And the "consistent 3-in-3 read-loop leak" - 3, 13, 12 blocked reads - did
not reproduce at all on the next three runs (8, 1, 3) of the same code against
the same file. Rounds were 24 in all seven runs taken. Efficiency work off
3-run samples is chasing variance; that is what this telemetry exists to end.
First measured run of the new case: PASS, 25 rounds, 23 tool calls.
Log: logs/test-runs/2026-08-21-real-file-repair.log

### 2026-08-21 - Context control from the chat, and three commands that were lying
Files edited: `shamsu/cli/repl.py`, `shamsu/agents/simple_chat.py`,
`shamsu/agents/simple_prompt.py`, `shamsu/agents/prompts/simple_system.md`,
`shamsu/agents/loop_guards.py`, tests
What changed: added `/context window` (the setting existed install-wide; the
terminal could not reach it). Fixed `/context show`, which reported the legacy
loop's frozen constants - a 2,000-token tool-result cap against the 8,000 simple
mode actually uses - and `/context compact`, which described the legacy budget
manager's threshold rather than the compaction that runs. Then the budget
itself: the reply reserve was 100% of a 4,096 window and 50% of an 8,192 one,
which `/context window` had just made reachable; one tool result could be 97.7%
of an 8k window. Both are shares now. The prompt stopped claiming earlier
messages on a fresh thread, and a reply that is nothing but a tool-call object
is nudged instead of being handed over as the answer.
Verified: compaction checked LIVE on qwen2.5-coder:3b at a forced 4,096 window -
it fired at turn 3 and the turn-1 fact survived eviction, recalled correctly at
turn 6. The summary grows 0 -> 133 -> 401 -> 669 -> 937 -> 1098 then stops at
its budget cap, which is correct.
Corrections to my own earlier findings, both from checking rather than assuming:
HYDRATE_MAX_MESSAGES is 400 in simple mode, not 24 - the 24 is chat_state's
default and simple mode overrides it, so there was nothing to fix. And the
continuity prompt section was a real false claim but NOT the cause of the turn-1
apology: with it removed the 3B still apologises to a non-coding instruction,
and answers a coding one cleanly on the same fresh thread.
Tests: 8 new; full suite 3443 passing / 0 failing.

### 2026-08-20 — Phase 3: a plan that is written down, and shown again
Files edited: `shamsu/agents/plan_anchor.py` (new), `tests/test_plan_anchor.py`
(new), `shamsu/agents/simple_chat.py`, `shamsu/agents/simple_router.py`,
`shamsu/agents/tool_classifier.py`, `evals/diff.py`, tests
What changed: the contract has held ordered, persisted, checkable items for
weeks and reached the model only when it called `contract_status` - invisible to
exactly the model that had lost the thread. It is now re-injected every turn,
capped, and dropped once resolved. A multi-part request is asked once to write
the steps down. Added a read-only `plan` tool category: smallcode's planner
persona works because its frontmatter gives it no write tools, and that needs no
sub-agent here. Also adaptive retry temperature, and a significance test in
`evals.diff` replacing the flat flaky-exclusion - which I had flagged that
morning as too blunt and which real use confirmed within hours.
Tests: 20 plan-anchor + 4 loop-level + 3 plan-role + 4 diff tests. Full suite
3433 passed / 0 failed. The suite caught a real false positive I would have
shipped: `port` was in the multi-step hint list for "port this to X" and matched
"remember: the port is 8080" - a 26-character note being asked to plan itself.
Eval: **NOISE, exit 2 - unmeasured.** The plan anchor fires on ZERO of the 12
eval cases; every prompt in the suite is a single-step one-liner. So the +348s
walltime is NOT the anchor - it cannot be - it is variance (the same suite has
run 1585, 1415, 895, 972 and 1320s). Cumulative against the original baseline
stays IMPROVED (+0.100, exit 0).
Log: logs/test-runs/2026-08-20-phase3-plan-anchor.log

### 2026-08-20 — OPEN: the eval suite cannot see most of this work
Not a task; a finding worth not re-deriving. All 12 seed cases are single-step,
one-line prompts ("Create a file hello.py", "Rename old_name.py"). The plan
anchor, the read-loop guard, greeting regression, trust decay and the third exit
all fire on MULTI-step or degenerate behaviour, and no case produces either. The
only thing the suite could measure this session was a missing tool - and it did,
cleanly: `rename_file_via_move_tool` 0/3 -> 3/3, exit 0.
Also open, and NOT caused by this session's work: `run_command_verify` and
`ask_before_choosing_an_approach` were 3/3 in the committed BENCHMARK.md of
2026-08-16 and are 0/3 in every run taken today, including the baseline captured
before any change here. Something between those dates broke them.

### 2026-08-20 — Read path and onboarding: both ends of a file, and what the project IS
Files edited: `shamsu/agents/simple_chat.py`, `tests/test_simple_chat.py`
What changed: a file nothing can outline now returns its first AND last 60 lines
rather than a head clip - `.md`, `.txt`, `.csv`; code still gets an outline,
which beats two slices. Added `project_brief`, one line naming the project's
language and test command, from manifests and `detect_test_command` that were
already there and had never been summarised into the prompt. Added the
Claude/OpenAI tool-name aliases (`Edit`, `Bash`, `Grep`) after watching `Edit`
fall through the new closest-match to a re-listing of the whole roster.
Tests: 6 new; full suite 3400 passed / 0 failed. My assertion that the brief
would say `vitest run` was wrong - it says `npm test`, the command you actually
run, which is the better answer.
Log: logs/test-runs/2026-08-20-phase2-loop-guards.log

### 2026-08-20 — Phase 2: the guards simple mode did not have, as objects
Files edited: `shamsu/agents/loop_guards.py` (new), `tests/test_loop_guards.py`
(new), `shamsu/agents/simple_chat.py`, `tests/test_simple_chat.py`
What changed: read-loop detection (soft at 5, firm at 8 reads producing
nothing), greeting regression, closest-match on an invented tool name, and
per-tool trust decay that never withholds a writing tool. The four simple mode
did not have. The eight that already existed stay inline: moving working, tested
code is risk with no behaviour to show for it, and can follow when something in
them needs to change anyway. The seam is the point - every one of the eight
needed a whole loop, a fake client and a scripted turn to exercise; these are
objects with their own tests.
Tests: 17 new unit tests (no model, no loop) + 6 loop-level tests. Full suite
3394 passed / 0 failed. Two of my own tests were wrong before the code was -
one expected a single fuzzy match where the exact name should win, which became
a real improvement; one searched for text that lives in the activity line rather
than the correction.
Eval: **NOISE, exit 2 - neutral.** 83.3% -> 80.6%, and the only case that moved
was the one already proved a coin flip (`ask_user_clarifies`, 4/7 at seven
samples). This is expected rather than disappointing: the guards fire on
degenerate behaviour - eight fruitless reads, a greeting mid-task, an invented
tool name, a tool failing five times - and none of the twelve cases exhibits any
of it. They are UNMEASURED, not disproven. The gap is in the suite: it has no
case that reproduces a model losing the thread, which is the entire class of
failure these guards exist for. Cumulative against the original baseline is
IMPROVED (+0.100, exit 0, walltime -613s) but that is the rename fix, not this.
Log: logs/test-runs/2026-08-20-phase2-loop-guards.log

### 2026-08-20 — A third exit before the turn gives up
Files edited: `shamsu/agents/simple_chat.py`, `tests/test_simple_chat.py`
What changed: four edits that changed nothing used to end the turn with "I have
stopped rather than keep guessing - it would help to tell me the exact text to
look for", which is the failure users reported: an apology that hands the work
back. The loop now changes approach once first, naming the calls - outline,
`read_symbol` the wrong function, `replace_symbol` with its new body. That is
the tool which does not require reproducing the old text byte-for-byte, which
is the step failing four times over. Offered once per turn; the second time it
really stops.
Tests: 3 new behavioural tests; one existing test updated because reaching the
stop now requires getting past the new exit. Full suite 3371 passed / 0 failed.
Log: logs/test-runs/2026-08-20-phase1a-tools.log

### 2026-08-20 — Phase 1a-1d: the tools simple mode could not reach, and room to put them
Files edited: `shamsu/agents/simple_chat.py`, `shamsu/agents/simple_router.py`,
`shamsu/agents/tool_classifier.py` (new), `tests/test_tool_classifier.py` (new),
`tests/test_simple_chat.py`
What changed: an audit found 36 of 43 registry tools were never offered to the
model. Wired `move_file`, `delete_file`, `ask_user` (plus the loop half that
ends a turn on a question, which only the legacy loop had) and read-only
`git_status`/`git_diff`/`git_log` gated on the workspace being a repository.
Room for them came from a deterministic regex classifier that narrows the
roster per request with no extra round - 26 schemas/3,196 tokens on a 32k window
became 14/1,885 for an edit request - with `select_category` kept in every
narrowed roster so a wrong guess costs one round trip rather than stranding the
model. Web tools deliberately left out: they depend on an auto-started SearXNG
and would violate "offer only the tools that have something to answer from".
Tests: 23 new classifier tests + 10 new tool tests; full suite 3364 passed.
Eval: 3 samples, default tier (7B). `python -m evals.diff` returned **IMPROVED,
exit 0** for `move_file` + narrowing: reward 66.7% -> 75.0%, reliable delta
+0.083, and the single case that moved was `rename_file_via_move_tool` 0/3 ->
3/3 - the one predicted in advance. Nothing regressed, and walltime fell 170s
because the narrowed roster is also a shorter prompt.

`ask_user`/`delete_file`/git measured separately and the verdict is **NOISE,
exit 2 - unproven.** At 3 samples both asking cases read 100%; re-measured at 7
against a worktree at HEAD they were 1/7 -> 4/7 and 4/7 -> 7/7. Reward more
than doubled (35.7% -> 78.6%) and the tool still refuses to count it, because
both were flaky in the baseline - Fisher's exact on 4/7 vs 7/7 is p~0.19, so
seven samples cannot call it. The mechanism is plausible (`ask_user` ends the
turn, so the question becomes `final`, which is what the check reads) but it is
not measured. KNOWN LIMITATION: a case flaky BEFORE can never be shown to have
been fixed, because the rule excludes it from either side. A significance test
would be better than a flat exclusion.
Log: logs/test-runs/2026-08-20-phase1a-tools.log

### 2026-08-20 — Phase 1: a mechanical verdict on whether a change helped
Files edited: `evals/diff.py` (new), `tests/test_evals_diff.py` (new)
What changed: added `python -m evals.diff <baseline> <feature>`, ported from
smallcode `bench/diff.js`, with exit codes 0 improved / 1 regressed / 2 noise /
3 error. Sample-aware where smallcode is not - reward is a per-case pass
fraction, flaky cases are held out of the verdict on both sides, and unequal
`--samples` refuses to compare rather than warning. A case that fell from
passing every attempt to failing every attempt overrides a positive average.
Without this, the eval variance already recorded in this project (1/7 to 5/7 on
identical code) makes every later phase unfalsifiable.
Tests: 16 new tests, all passing; verified against a real `--json-out` report
from a live 2-case run (diff against itself = NOISE, exit 2).
Log: logs/test-runs/2026-08-20-phase1-eval-diff.log

### 2026-08-20 — Phase 0: verification stops reporting a patch-broken file as "still being built"
Files edited: `shamsu/agents/simple_chat.py`, `shamsu/agents/simple_verify.py`,
`shamsu/agents/chat_state.py`, `tests/test_simple_chat.py`
What changed: `_verify` suppressed a real syntax error whenever the file's only
complaint was open blocks - which is what a patch that eats a `}` leaves behind,
and also what the first section of a chunked write looks like. Nothing told them
apart, so `node --check: SyntaxError` became `{"ok": true, "continue with
append_file"}` and a model asked to fix the file had just been told it was fine.
The exemption is now gated on the last write having ADDED to the file. Also:
unclosed blocks point at the innermost opener rather than line 1; the repair
counter that gates thinking is a streak reset by any successful write, not a
turn-wide tally; and the two write-refusal stops plus the OOM stop no longer
replay into history as assistant turns.
Tests: 5 new behavioural tests + 2 extended; full suite 3319 passed / 0 failed;
ruff at baseline (208, unchanged). Live on `qwen2.5-coder:3b-instruct`: a
non-growing write got the real node error while the append before it correctly
stayed "still being built". | Log: logs/test-runs/2026-08-20-phase0-verify-truth.log
