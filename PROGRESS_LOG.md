# SHAMSU Progress Log

One entry per completed task, newest at the top. Raw model/test output lives in
`logs/test-runs/<date>-<task>.log`.

### 2026-08-31 - Interactive check_page, a look before the ceiling, and a spill nobody could see
Files edited: `shamsu/tools/page_check.py`, `shamsu/agents/simple_chat.py`,
`shamsu/llm/capabilities.py`, `shamsu/skills/bundled/ui-designer/*`,
`tests/test_page_check.py`, `tests/test_spec_to_contract.py`,
`tests/test_model_facts_and_limits.py`, `tests/test_simple_skills.py`
What changed: four fixes from reviewing a live build that produced a game in
which nothing happens. `check_page` takes `click` and `wait_seconds` and reports
canvas coverage and motion - measured on that game, clicking START moved it from
1.22% to 1.23% covered, which is the bug stated as a number; no vision model is
involved, the browser reads its own pixels and the harness writes the sentence.
A turn with three rounds left that has written code and run nothing is now asked
to check it. A plan asked for and never written is reported at turn end, since
that build finished with no contract at all. And the harness now reads
`/api/ps` and says when the model is running partly from system RAM - the cause
of a 536-second model call that looked exactly like a hang.
Tests: 20 new. Full suite 4394 passed / 2 skipped / 0 failed.
Log: logs/test-runs/2026-08-31-interactive-check-and-spill.log

### 2026-08-31 - check_page retries a server that is still binding; a dead command stops claiming it started
Files edited: `shamsu/tools/page_check.py`, `shamsu/tools/executor.py`,
`tests/test_page_check.py`, `tests/test_background_processes.py`
What changed: reviewed the first live run of `check_page` in `F:oice-demo`.
The game it built works, and two defects showed up anyway. `check_page` reported
`net::ERR_EMPTY_RESPONSE` on a server that had been detached seconds earlier -
`curl` fetched the same URL ten seconds later - so a working page was reported
broken and the model fell back to a check that cannot see rendering; connection
-level errors now retry 3x over ~3s while a real failure still fails at once,
and approval is asked once across retries. And `command.detached` was logged
unconditionally, so a command that exited 1 on a bad path was recorded as
"Started in the background"; the launcher now reports whether it stayed up.
Also confirmed, and worth recording: the situation-triggered skill injection
fired twice in that run, and the write guards refused an oversized rewrite and
two deletes before the model found the correct patch.
Tests: 8 new (retry classification, a first navigation that fails and a check
that still passes, approval asked once, a command that dies is not recorded as
running). Full suite green.
Log: logs/test-runs/2026-08-31-check-page-retry.log

### 2026-08-31 - check_page: a real browser, reachable from the agent
Files edited: `shamsu/tools/page_check.py` (new), `shamsu/agents/simple_chat.py`,
`shamsu/agents/simple_router.py`, `tests/test_page_check.py` (new),
`tests/test_simple_chat.py`
What changed: `BrowserTool` had a passing real-Chromium test and no tool schema
at all, so a model asked to build a web page could never check it - which is why
the 2026-08-31 session invented `verify_web_app`, invented its output, and
skipped twelve assertions. `check_page(url)` now loads the page and reports
console errors, which elements rendered, whether a canvas has a non-zero drawing
surface, and a screenshot. Local URLs are auto-approved (a prompt per check is
what would make it unusable); anything else still asks. Assigned to the `run`
category and added to the non-registry set - both caught by existing tests.
Tests: 15 new, all driving real Chromium against real fixtures, covering the
three failures that look identical from outside: a page that throws, a page that
renders nothing, and a 0x0 canvas. Verified against the real asteroid game.
Full suite 4327 passed / 2 skipped / 0 failed.
Log: logs/test-runs/2026-08-31-check-page.log

### 2026-08-31 - Verdict honesty, cross-file coverage, surface parity, pinned sampling
Files edited: `shamsu/agents/simple_chat.py`, `shamsu/control/runner.py`,
`shamsu/integrations/telegram/sessions.py`, `scripts/validate_release.py`,
`pyproject.toml`, `RELEASE_VALIDATION.md`,
`tests/test_harness_honesty_and_parity.py` (new)
What changed: seven items from the 2026-08-31 audit. The turn verdict now
reports outstanding contract checks, how many times the loop steered the model
and how often the context was trimmed - all three were measured and none
reached the user; counting the corrections needed a single seam (`_steer`)
because thirteen call sites wrote them directly. `_cross_file_problems` reads
its suffix set from `verify.wiring` instead of restating three of its ten, so
Python and TypeScript projects get cross-file checks at last. Telegram now
builds its tools through `build_simple_tools` in simple mode, so all three
surfaces share one approval policy and the eval override works there; the web
runner gained the ActionLedger it never had. Sampling parameters are pinned,
with repeat_penalty at 1.0 rather than Ollama's 1.1 applied to code. The
release gate's two latency budgets were re-based off 1.5s, which no local model
can meet and which had the gate permanently red. pytest-timeout added at 300s
after a venv test wedged the suite for 35 minutes. The file skeleton is
memoised on its inputs instead of parsed twice a round.
Not done here, and deliberately: browser/dev-server tools (BrowserTool and
DevServerTool have no schema at all - a build, not a wiring fix), autonomous
continuation, the contract as a gate, and requirement extraction. State-
triggered skills landed concurrently from another author and were verified
rather than duplicated.
Tests: 19 new, full suite 4312 passed / 2 skipped / 0 failed.
Log: logs/test-runs/2026-08-31-audit-batch.log

### 2026-08-31 - Background servers are written down, swept and stoppable
Files edited: `shamsu/tools/executor.py`, `shamsu/cli/repl.py`,
`tests/test_background_processes.py` (new)
What changed: a detached server was tracked only in memory and reaped only by
`atexit`, so closing the console window, killing the process or crashing left it
running with nothing on disk that named it. Found live: two `http.server`
processes holding port 8000 two hours after their session ended. Each background
process now gets `.shamsu/processes/<pid>.json` written before the readiness
wait; opening a workspace sweeps the directory, drops dead entries and REPORTS
live ones rather than killing them; `/processes [stop <pid>|stop all]` lists and
ends them; `_kill_pid_tree` works from a bare pid, since a process from a
previous session has no Popen handle.
Tests: 18, including the real regression - launch a server, wipe the in-memory
registry to simulate the crash, and assert it is still findable and stoppable
from disk alone. Full suite green.
Log: logs/test-runs/2026-08-31-background-process-tracking.log

### 2026-08-31 - Three defects the voice-demo session exposed, and the skill matcher it broke
Files edited: `shamsu/agents/simple_chat.py`, `shamsu/agents/simple_skills.py`,
`shamsu/verify/wiring.py`, `tests/test_voice_demo_regressions.py` (new),
`tests/test_wiring_frontend.py`
What changed: reviewed a live two-hour run in `F:oice-demo` that built an
asteroid game which declared victory the moment you pressed Start. Every guard
fired correctly; three things behind them did not. `contract_create` accepted
`assertions` as a printed Python list and stored eight requirements as one
assertion, so the Definition of Done was inert for the whole session -
`normalize_arguments` now recovers a printed list for the four array arguments
that get sent that way. The duplicate-class diagnostic named the fault and not
the next call, and the model answered it with 26 refused edits across four
turns; it now says which copy to delete, with which tool, and which file must
load first. And a `.js` written but never referenced by any page was invisible -
`unreferenced_script` is the reverse of `missing_asset` and the more expensive
direction, because nothing 404s. Running the new skill matcher against the real
prompts from that session also caught a defect in it: `phrase in text` made `ui`
match inside "build", so a game build scored `ui-designer`; matching is on word
boundaries now.
Tests: 26 new tests in `test_voice_demo_regressions.py`, built from the actual
payloads and files of the run; one existing wiring assertion widened, because
`unreferenced_script` is a genuine fourth fault in the snake-game fixture it
covers. Full suite green.
Log: logs/test-runs/2026-08-31-voice-demo-regressions.log

### 2026-08-31 - One context window, budgets that follow it, and four automatic capabilities
Files edited: `shamsu/context/budget.py`, `shamsu/agents/simple_chat.py`,
`shamsu/agents/simple_prompt.py`, `shamsu/agents/prompts/simple_system.md`,
`shamsu/agents/simple_skills.py` (new), `shamsu/llm/capabilities.py` (new),
`shamsu/runtime/settings.py`, `shamsu/runtime/models.py`, `shamsu/cli/repl.py`,
`shamsu/control/store.py`, `shamsu/control/approvals.py`, `evals/harness.py`,
`tests/test_context_window_settings.py` (new),
`tests/test_model_facts_and_limits.py` (new),
`tests/test_capabilities_are_automatic.py` (new), `~/.shamsu/settings.json`
What changed: ten fixes in one pass, at the user's request. The window
precedence now lives once in `chat_ctx_ceiling`, so a setting made inside
SHAMSU no longer makes the chat call and the background calls disagree and
reload a 6GB model between them - found live as a saved `chat_max_ctx` of
32786. `/context window` does real arithmetic on `k`, snaps to one of four
windows, refuses one the model cannot hold, and completes from a dropdown.
`tool_result_budget`, `summary_budget` and the skeleton ratio take the live
ceiling, so a session shrunk to 8k stops spending as if it had 32k - one tool
result was allowed 98% of the window. `max_rounds`, `turn_budget_s` and
`approval_timeout_s` became settings with a `/set` command. Model window and
tool-calling support now come from `/api/tags`, cached off the hot path, with
the table as fallback. Skills are matched by the harness and injected instead
of waiting for a `use_skill` that was called zero times in every logged
session; the code graph builds itself instead of only refreshing; the prompt
and the roster read the same probes; and the four contract assert tools are
withheld until there is a contract. A floor of ten prompt-named tools survives
narrowing, which is the general form of the 2026-08-29 write-roster fix.
Tests: 3 new files, 92 tests, all passing; full suite green (see log).
Log: logs/test-runs/2026-08-31-context-window-and-automatic-capabilities.log

### 2026-08-31 - F6 skips a spoken reply
Files edited: `shamsu/cli/tui.py`, `shamsu/cli/repl.py`,
`shamsu/voice/playback.py`, `tests/test_tui.py`, `tests/test_voice_engines.py`
What changed: F6 stops the reply being spoken. Deliberately not the same thing
as `SHAMSU_VOICE_OUTPUT=off`, which is the setting - this is the key for when
the answer is being read out, you have already read it, and you want the next
thing to happen now. The text stays in the pane; only the audio is dropped.
The status bar offers the key only while something is actually speaking, since
a hint for a key that does nothing is how the approval menu went wrong.

The binding was the easy half. Live, the sound kept going for 2.14 seconds
after the key: `stream.write` blocks until the device accepts the data, so
writing a whole sentence per call meant the stop flag went unread for the
length of that sentence. It was set the entire time and nothing was looking at
it. Audio now goes out in 0.1s blocks with the flag checked between them,
measured at 0.45s from key to silence - most of what is left is the device
buffer that `abort()` drops.
Tests: 5 added. Three on the frame (F6 stops a speaker that is mid-utterance;
it says so when there is nothing to skip; a playback error leaves `_speaking`
False rather than offering a dead key for the rest of the session), two on the
player (a 4-second chunk goes out as ~40 writes, and stopping during the third
block drops the remaining ninety-odd). 144 pass across voice/voice_engines/tui,
ruff clean |
Log: logs/test-runs/2026-08-31-f6-skip-speech.log

### 2026-08-31 - A skill is chosen from what the turn is doing, and sized to fit
Files edited: `shamsu/agents/simple_skills.py`, `shamsu/agents/simple_chat.py`,
`shamsu/skills/bundled/{debugger,planner,critic,qa-tester}/` (new),
`shamsu/skills/bundled/{large-file-surgery,developer,sql-databases}/SKILL.md`,
`tests/test_simple_skills.py` (new), `tests/test_skills.py`
What changed: skills were dead. Across the asteroid session - 24 model calls,
13 minutes, failed - `use_skill` was called zero times and nothing was
auto-injected either, while the 7-skill index rode along in all 24 prompts at
~147 tokens each. Not a bug in the matcher: "build an asteroid game with
multiple levels and sound effects" scores `developer` at 2.0 against a floor
of 3.0 and everything else at 0.0, so injecting nothing was the correct answer
to the question being asked. The question was wrong.

Selection now asks the SITUATION first and the request second, because a
request is what someone thought the job was before starting it. `Situation`
reads two lists the loop already keeps - `_observed_writes` and
`_turn_failures` - so nothing new is recorded: three writes to one path is
`large-file-surgery`, two identical `contract_assert` refusals is `qa-tester`,
two identical anything-else failures is `debugger`. Re-asked at each round
boundary, injected once per turn per skill. That run would have got surgery at
step 8 of its 9-part file and qa-tester at the second of three identical
"needs evidence" refusals.

It also fixes a second-order bug: the stack filter reads the extensions
PRESENT, and the workspace was empty when the turn began, so `ui-designer` was
filtered out at the moment of choosing and became applicable four tool calls
later. Re-asking mid-turn re-reads it.

Sizing: 0.04 of an 8k window is 327 tokens, and `large-file-surgery` was 722 -
over its own 700 budget - so the skill this situation code injects was the one
guaranteed to arrive cut in half, with the "do not rewrite the whole file"
rules in the half that got cut. Rewritten to 294. `developer` 393 -> 292,
`sql-databases` 591 -> 324. All 13 skills now fit an 8k window intact.

Four smallcode personas converted from `reference/smallcode/agents/`:
debugger, planner, critic, qa-tester - the ones that map onto failures this
harness actually has. Not `oracle`, `scout` or `librarian`, which assume
sub-agent delegation SHAMSU does not have. Rewritten rather than copied: their
`model:`/`tools:` frontmatter is meaningless here, and 200-250 tokens of
numbered imperative steps is what a 3B follows.
Tests: 23 added. The fixture is the asteroid run's real write and failure
sequence, written out rather than read from that workspace - it is a demo, not
a test fixture. Three are invariants rather than cases: every bundled skill
fits a small model's window, every bundled skill has triggers (one without
them can only be reached by a `use_skill` call that never comes), and every
name `SITUATION_SKILLS` points at exists. 647 pass across
skills/simple_skills/simple_chat/capabilities/hardening.

One existing test asserted on the phrase "Inspect relevant files" in the
developer skill and broke when it was rewritten; it now asserts the rule
(`patch_file`) rather than the sentence, since the test is named for discovery
and was telling us nothing about it |
Log: logs/test-runs/2026-08-31-skills-situation-and-sizing.log

### 2026-08-31 - One icon column, one text column
Files edited: `shamsu/cli/tui.py`, `shamsu/cli/turn_render.py`, `tests/test_tui.py`
What changed: there were TWO icon columns and text landing in both. The frame
put its own marks - a prompt's chevron, an answer's diamond - in column 0 with
text at column 2, while every row the renderer painted was indented two spaces
and carried its icon at column 2 with text at column 4. Reasoning was indented
four, a diff padded four, activity lines two. Nothing lined up with anything.

One rule now: a mark owns column 0, text starts at column 3, and subordinate
detail - a diff under a write, a reasoning trace - sits at column 5 so it
reads as belonging to the row above rather than as another row. A row with no
mark of its own (an activity line, a repeat counter) starts at the text column
rather than in the gap. `marked()` builds a mark plus the gap that follows it,
so a glyph and its padding are never written apart.

The width is `ICON_COLUMN` in the renderer and `GUTTER_WIDTH` in the pane -
two constants, because neither module imports the other and neither should.
`test_the_icon_column_is_the_same_width_on_both_sides` is what stops them
drifting; if they ever disagree, marks land in one column and text in another,
which is the exact layout this replaced.
Tests: 3 added - the two constants agree, no text ever lands in the icon
column, and every row starts its text at the same column - each asserted over
a whole rendered turn (prompt, reasoning, reads, a collapsed repeat, an
approval, a write with a diff, an error, a notice, an answer) rather than one
row type. Four existing tests hard-coded the old one-space gap; they now
assert through the mark constants, so the gap lives in one place and they keep
testing what they are about. 110 in test_tui, 808 across the
tui/turn/render/parity/trace/repl/log suites, ruff clean |
Log: logs/test-runs/2026-08-31-tui-icon-column.log

### 2026-08-31 - The answer reads as a block, and the action rows stop looking alike
Files edited: `shamsu/cli/tui.py`, `shamsu/cli/turn_render.py`, `tests/test_tui.py`
What changed: three things, all of them "the pane is technically correct and
still hard to read".

A mark repeated twenty times stops being a mark. `CONTINUATION_MARKS` gives a
kind a SECOND glyph for its second and later lines, so an answer opens with
the diamond and is carried by a thin rule. The block's extent is still legible
with the colour stripped out - which was the whole reason for marking every
row - without the column of diamonds. A kind absent from the map repeats its
own mark, which is what the approval bar wants.

Every read, search and outline shared ICON_TOOL, and reads are most of a turn:
a live run is three or four of them per write, so the differentiated icons
were being spent on the rare rows and the common ones were an undifferentiated
column. `_TOOL_STYLES` is consulted before the family rules, so a tool takes
its own mark without widening the if-chain. Failure still wins over all of it:
a failed search is a failure first, and must not differ from a successful one
by colour alone.

An answer began on the line directly under a tool row, where the eye has
nothing to catch - the mark changes, but marks are two columns wide and the
text runs on at the same indent. `LogPane.separate()` puts one blank row
there, and is idempotent because its callers are.
Tests: 6 added (block opens once and is carried; a second answer opens its own
block; the separator exists and does not stack; read/search/outline/write/test
hold five distinct marks; a failure takes the failure mark whatever the tool
was), plus the earlier wrapping test updated to accept the rule. 107 in
test_tui, 764 across tui/turn/render/parity/repl/voice/log, ruff clean.

Full suite: 4222 passed, 1 failed, 2 skipped. The failure is
`test_release_scripts.py::test_unix_lifecycle_scripts_parse_with_bash`, which
shells out to bash and passes on its own - it fell over while bash-heavy
commands were running against the same machine. No shell script was touched
here |
Log: logs/test-runs/2026-08-31-tui-answer-block-and-icons.log

### 2026-08-31 - An answer no longer wraps itself on its own gutter
Files edited: `shamsu/cli/tui.py`, `shamsu/cli/repl.py`, `tests/test_tui.py`
What changed: the frame set `console.width = app.output_width()` - the PANE's
width - and the pane then prepended a two-column gutter to every decorated
line. Rich pads what it renders to the full console width, so each answer line
was already full when the mark went on, overflowed, and wrapped: a near-empty
continuation row under every single line, carrying no gutter and so no colour
either. That is what "something feels off when it replies in text" was.
`content_width()` is the pane width minus `GUTTER_WIDTH`, and it is what the
console gets now.

Second route to the same defect, fixed with it: the width was set ONCE at
frame start, so it went stale on the first terminal resize. `adopt_console()`
hands the console to the frame, which re-syncs the width on every paint beside
the pane's own `set_width`.
Tests: 3 added - an answer whose every row carries its mark (verified to FAIL
on the old behaviour: 5 rows, 1 unmarked, against 4 rows all marked), the
console following a resize, and a guard that every gutter really is
GUTTER_WIDTH wide, since a 1-column mark would restore the bug for one kind of
line only. 101 in test_tui, 509 across the tui/repl/voice/frame suites |
Log: logs/test-runs/2026-08-31-tui-answer-wrapping.log

### 2026-08-31 - SHAMSU speaks as am_adam
Files edited: `shamsu/voice/kokoro_engine.py`, `tests/test_voice_engines.py`
What changed: `DEFAULT_VOICE` is `am_adam` rather than `af_heart`. One line,
but it decides what SHAMSU sounds like on every machine that has not set
`SHAMSU_VOICE_NAME`, so it is pinned by a test rather than left to whoever
edits the constant next.
Tests: 2 added (the default is what gets spoken; an explicit voice_name beats
it), 19 pass. Both take a `no_model_needed` fixture - written first without
it, they passed only on a machine where the 353MB model happened to be on
disk, which is a coincidence rather than a test |
Log: logs/test-runs/2026-08-31-tts-bakeoff.log

### 2026-08-31 - Kokoro speaks the replies, behind a swappable engine layer
Files edited: `shamsu/voice/engines.py` (new), `shamsu/voice/kokoro_engine.py`
(new), `shamsu/voice/piper_engine.py` (new), `shamsu/voice/playback.py` (new),
`shamsu/voice/__main__.py` (new), `shamsu/voice/speech.py`,
`shamsu/voice/__init__.py`, `pyproject.toml`,
`tests/test_voice_engines.py` (new)
What changed: SAPI was the only voice there had ever been, because
`SpeechPlayer` WAS the engine. It now holds settings and text cleanup only,
and asks `engines.build_engine()` for something with `speak()`/`stop()`.
Three exist - kokoro, piper, system - and none is named anywhere else in the
codebase. Adding a fourth is `register_engine()` plus one line in
`AUTO_ORDER`. `auto` walks that order and takes the first engine that can run
on this machine, reading a factory's `VoiceError` as "try the next"; a NAMED
engine fails loudly instead, because someone who asked for Kokoro and has no
model wants to hear about the model, not be handed SAPI in silence.

The defaults are measurements, not preferences, and the numbers are in
logs/test-runs/2026-08-31-tts-bakeoff.log. fp32 is 3.4x FASTER than the int8
build on this CPU (RTF 0.24 against 0.83), so the 92MB "small" model was
rejected and the 325MB one is the default - quantization bought disk and cost
most of the speed. `intra_op_num_threads=8` beat both 4 and all-20; at 20 the
RTF passes 1.2 and synthesis falls behind the speaker. Replies are synthesized
a sentence at a time on a producer thread that runs ahead of playback, so a
warm session starts speaking 0.6-0.85s after the answer lands.

The ONNX session names `providers=["CPUExecutionProvider"]` itself rather than
letting kokoro-onnx resolve them: its resolver takes EVERY available provider
once an accelerated onnxruntime is installed, and the GPU belongs to Ollama.
That is the one promise here a refactor could break invisibly, so a test holds
it rather than a comment.

Downloading is `python -m shamsu.voice download`, never automatic - 353MB is
not something a spoken reply may start while someone waits for an answer.
Tests: 17 added (engine selection + fallback + named-engine failure, CPU-only
provider pinning, per-sentence streaming, stop semantics, rate mapping,
playback abort-vs-drain), full suite 4212 passed / 2 skipped. The stop test
found a real defect - queued audio was still handed out after stop() - which
was fixed in `_synthesize_ahead` |
Log: logs/test-runs/2026-08-31-tts-bakeoff.log

### 2026-08-31 - A reply is spoken only when the prompt was spoken
Files edited: `shamsu/voice/speech.py`, `shamsu/voice/__init__.py`,
`shamsu/cli/tui.py`, `tests/test_voice.py`, `tests/test_tui.py`
What changed: `speak_reply` spoke EVERY reply, because nothing on that path
knew how the prompt had arrived - typed prompts talked back, and so did any
turn the terminal was merely watching. `TuiApp` now arms a single flag in
`_voice_transcribed` (the microphone path, the only place that knows), clears
it on anything typed, and consumes it in `speak_reply`; the policy itself lives
in `reply_should_be_spoken()` so there is one answer rather than one per
caller. `SHAMSU_VOICE_OUTPUT` carries the policy as well as the switch:
`voice` (default), `always` (the old behaviour), `off`.

Telegram and the web portal already drove their own chat loops and never
reached the speaker, so a voice note keeps being answered in text and the open
terminal stays silent. That was true by accident of structure, so it is now
held by a source guard - `test_remote_surfaces_never_reach_the_local_speaker`
- because the correct behaviour there is an absence, and an absence is what
the next refactor deletes without noticing.
Tests: 5 added (2 voice-policy, 1 silence-on-typed, 1 armed-once, 1 remote
guard), 140 passed across test_voice/test_tui/test_telegram_remote_control |
Log: logs/test-runs/2026-08-31-voice-output-gate.log

### 2026-08-24 - One contract per phase, derived from PLAN.md
Files edited: `shamsu/agents/plan_anchor.py`, `shamsu/agents/simple_contract.py`,
`shamsu/agents/simple_chat.py`, `shamsu/agents/simple_router.py`,
`shamsu/agents/loop_guards.py`, `tests/test_phase_contracts_from_plan.py` (new),
`tests/test_simple_chat.py`
What changed: `contract_create` asks the model to write a list, so in demo-3 it
wrote PLAN.md with eight phases and then wrote contracts matching nothing in it -
five across the session, each overwriting the last - and "phase 2" came to mean
whatever the rolling summary last said. New `contract_from_plan(phase=N)` makes
ONE PHASE of the plan the contract, its own numbered items as the assertions,
verbatim.

The obvious shape - one contract, one assertion per phase - was built, measured
and thrown away: the eight phase HEADINGS produce 0 assertions that trip
`claims_runtime_behaviour`, because a heading is a unit of WORK, so every one
would have passed on a file write. That is the failure this machinery exists to
stop, rebuilt out of its own fix. It also rendered at 2,019 chars against a
900-char anchor cap. A phase's own items are 3-5 near-claims and render at
~500.

What actually holds the line is `Contract.requires_run`: derived from a plan,
PROVENANCE gates it rather than wording, because a plan's items are written as
work ("Implement requestAnimationFrame game loop") and no sentence rule will
ever catch them. Nothing plan-derived passes on a write. Items go in verbatim -
rewording them is the model re-describing its own plan, which is the drift being
removed. Phases keep their own files (`.shamsu/contracts/phase-NN.json`), so
starting phase 3 no longer erases phase 2; switching away from an OPEN contract
is refused rather than overwritten (any open one, not just a slugged one - a
hand-made contract has no archive, so overwriting it loses the contract, not its
place); returning to a phase resumes it with its evidence rather than
re-deriving. `phase_progress` puts per-phase status in every prompt, and
`ask_for_a_plan` points at this tool instead of `contract_create` when a plan
document exists.
Tests: 21 new in `tests/test_phase_contracts_from_plan.py`. Four existing tests
went red on the new tool and all four were real wiring gaps, not test noise:
unreachable under two-stage routing (added to two router categories); not in
`_NON_REGISTRY_TOOLS` so the schema probe hit the wrong dispatcher; the `plan`
capability hint still read "There is no planning tool", which had become false;
and the OOM step-down test sat on a boundary that one more tool schema crossed -
every tool offered is tokens on every call. Full suite 3976 passed, 2 skipped |
Log: logs/test-runs/2026-08-24-phase-contracts.log

### 2026-08-24 - Review pass on the five fixes above
Files edited: `shamsu/tools/executor.py`, `shamsu/agents/simple_contract.py`,
`shamsu/agents/simple_chat.py`, `shamsu/integrations/telegram/sessions.py`,
`tests/test_plan_document_anchor.py`, `tests/test_contract_evidence.py`
What changed: five faults found by reviewing the above, three of them mine and
real. (1) `_run_command_detached` never closed the parent's write handle on the
still-running path - one leaked handle per detached server, and a server is
started to be LEFT running, so nothing would ever close it. The child holds its
own duplicate; ours now closes right after `Popen`. (2) Folding the PLAN.md
anchor into `_standing_plan` silently suppressed `ask_for_a_plan` on every
workspace with a plan file, because that ask is gated on the same call - which
would have taken `contract_create`, the evidence rule and the done-guard with
it, on exactly the projects most likely to need them. Split into
`_plan_document` and `_standing_contract`; the ask now tests the contract half.
(3) `_RUNTIME_CLAIM`'s leading lookbehinds used a literal space under `(?x)`,
which strips it - so `(?<!a )` became `(?<!a)` and matched nothing, and "a
**run** script" / "a **render** function" were being called runtime claims.
Rebuilt against a 30-assertion corpus: 15/15 runtime caught, 0 false positives,
was 13/15 and 2/15. Also `error` does not match inside `ReferenceError`, so
the most specific runtime claim there is slipped through - found by replaying
the contract that session had written by 10:15, whose three assertions all
passed on writes. (4) `done_guard` told a skips-only contract that "writing the
code is not evidence", with no writes in it. (5) Swapped the private
`_cleanup_run` for the public `complete_run`, which also logs the finish, and
widened the telegram guard to `BaseException` so a cancelled turn cannot leave
the session registered forever. `_DETACHED` now forgets exited processes rather
than growing for the life of the session.
Tests: 3 new (named exceptions, the error-type false positive, the
contract/document split); ruff clean on all three new test files. Full suite
3954 passed, 2 skipped | Log: logs/test-runs/2026-08-24-review-pass.log

### 2026-08-24 - The plan was on disk the whole time, and a write signed off a browser
Files edited: `shamsu/agents/plan_anchor.py`, `shamsu/agents/simple_chat.py`,
`shamsu/agents/simple_contract.py`, `tests/test_plan_document_anchor.py` (new),
`tests/test_contract_evidence.py`
What changed: two faults from `demo-3/asteroid`, both about a claim outliving
the thing that was supposed to check it.

(1) `plan_anchor.anchor` re-injects the CONTRACT every turn, which is what SHAMSU
means by a plan. It is not what the user meant. Asked to "outline your approach
in a PLAN.md file", the model wrote one in turn 1 and it reached no prompt ever
again: its real headings (`### Phase 3: Player Ship Module (player.js)`) appear
in ZERO of the 24 surviving prompts. What the model saw instead was the rolling
summary, which had invented a different decomposition and stamped it done -
"Phase 1 complete ... created and validated", "Phase 2 complete ... scaffolded" -
where PLAN.md says Phase 1 is "Project Setup & Scaffolding" and Phase 2 is "Core
Game Loop & Scene Setup". Neither matches, and "validated" never happened (three
commands succeeded all session). So "lets proceed with phase 2" resolved against
the summary and the model improvised. New `plan_document_steps` /
`document_anchor` re-show a root PLAN.md's step headings by their real names
every turn, and `_standing_plan` now shows both plans - the document even when
there is no contract, which is the state turns 2-4 were in.

(2) `contract_assert_pass` accepted a WRITE as backing for an assertion about
runtime behaviour. The contract it wrote at 09:15: a03 "game renders without
setElement error on page load", passed, `verified_by: write`, `observation:
"wrote src/main.js (not run)"`, and evidence quoting browser console output that
was never produced. `done_guard` did complain - at the end, only on a done claim,
and `render()` showed it as PASS the whole way there. New
`claims_runtime_behaviour` refuses the pass at the tool boundary and says what
would count, naming the now-detached server as a way to actually check a page.
A claim about what the code SAYS still passes on a write.
Tests: 6 new in `tests/test_plan_document_anchor.py` (incl. the real PLAN.md
shape), 4 new in `test_contract_evidence.py` incl. a replay of the real a03; two
existing write-provenance tests repointed to a static assertion, since
`_contract()`'s own two are both runtime claims. Full suite 3954 passed, 2
skipped | Log: logs/test-runs/2026-08-24-plan-anchor-and-runtime-claims.log

### 2026-08-24 - Told to stop reading before it had opened the file with the bug
Files edited: `shamsu/agents/loop_guards.py`, `shamsu/agents/simple_chat.py`,
`tests/test_loop_guards.py`, `tests/test_simple_chat.py`
What changed: in `demo-3/asteroid` the defect spanned seven source files -
`initGame()` never called in one, no default export in two more against a
`{ default: X }` import, a dead `let scene;` in four - and the read guard
interrupted at five reads, 17 times across the session, with "You probably have
enough ... do not keep reading". It did not have enough, and it obeyed: every fix
it shipped was scoped to whichever file it had read by the time it was told to
stop. Raising the ceiling was the WRONG correction and was tried first - nine
different files read to no purpose is exactly the open-ended "review X" this
detector exists for, and `test_eight_reads_without_producing_anything_is_interrupted`
went red saying so. No count separates the two cases. The instruction was what
was wrong. `ReadLoopDetector` now tracks the distinct things read (`targets`,
threaded through `_Round.read_targets` from the read signature simple_chat
already computed) and branches the WORDING, not the thresholds: a model
re-reading what it has is still told to stop reading; a model opening files it
has never seen is asked to say what it is looking for and read only that. Same
for the firm word at eight, which had been claiming "You have enough to go on" -
a statement about the code that this detector is in no position to make. The
turn-ending ceiling is untouched.
Tests: 6 rewritten in `tests/test_loop_guards.py`; the simple_chat test that
asserted the literal "Stop reading" repointed to the new instruction, with the
reason in the test. Full suite 3954 passed, 2 skipped | Log: logs/test-runs/2026-08-24-read-guard-wording.log

### 2026-08-24 - Two Telegram turns editing one file for nine minutes
Files edited: `shamsu/integrations/telegram/sessions.py`,
`tests/test_telegram_turn_overlap.py` (new)
What changed: `demo-3/asteroid` ran turn 6 (03:23:17) while turn 5 was still
going (03:21:40 -> 03:32:31). Nine minutes of overlap, both editing
`src/main.js`, and all three `old_string not found in src/main.js. The file was
NOT changed` failures that session were one turn patching a file the other had
moved underneath it. The guard already existed - `route_user_message` asks
`active_runs_for_session` whether something is in flight and merges a new message
in as feedback if so - and it was asking an empty registry: `register_run` is
reached only through `RunController`, which belongs to the legacy engine, so a
simple-mode turn (the default since 2026-08-18, and what that session ran)
registered nowhere. `_run_simple` now registers for the length of the turn and
releases in a `finally`, so a crashed turn cannot block the session either.
Note `QueuedRunner` in `shamsu/control/runner.py` does all of this properly with
leases - it is wired to the web portal only, and Telegram never went through it.
Tests: 3 new in `tests/test_telegram_turn_overlap.py`, incl. one asserting the
registry is non-empty from INSIDE the running turn. They have to opt out of
conftest's `SHAMSU_LEGACY_ROUTING` pin, which exempts one file by basename and
so hides every simple-mode path from the suite by default. Full suite 3954
passed, 2 skipped | Log: logs/test-runs/2026-08-24-telegram-turn-overlap.log

### 2026-08-24 - Eight `npm run dev` timeouts, and a translator called from nowhere
Files edited: `shamsu/tools/executor.py`,
`tests/test_server_commands_and_shell.py` (new)
What changed: in `demo-3/asteroid`, 13 commands ran across a two-hour session and
EIGHT were `npm run dev` - each burning the full 120s to exit 124, sixteen minutes
of wall clock, for a command that printed `VITE ready in 421 ms` and its URL
inside half a second and served the whole time. The agent therefore never saw the
page it spent sixteen turns fixing; its only working evidence channel was
`npm run build`, which exits 0 here because none of the bugs are build-time
errors. A server-shaped command now runs DETACHED: output to
`.shamsu/processes/<id>.log`, returns as soon as it announces itself or 3s after
it is still alive, stays up, and is reaped at exit. Measured on `python -m
http.server`: 120s+failure became 3.0s+exit 0, and `curl | head` against it now
returns the page. A trailing `&` is honoured as the background request it was -
cmd.exe read it as a separator and ran the thing in the foreground.
`_platform_command` turned out to be defined and called from NOWHERE in the
package, so the `python3` shim in its docstring had never run; it is now wired in
and also translates `mkdir -p` (which created a directory named `-p`, still in
that workspace), `head`/`tail` in a pipe ('head' is not recognized), and `which`.
NOT `rm`: translating first made `rm -rf /` reach the classifier as
`rmdir /s /q /`, which stopped matching the blocklist and turned a refused
command into one that ran. Four existing safety tests caught it. Translation now
happens downstream of `classify_command` and approval, and the `rm` rule is gone.
Tests: 33 new in `tests/test_server_commands_and_shell.py`, incl. one that starts
a real server and fetches a file off it, and two on the destructive-command path.
Full suite 3954 passed, 2 skipped | Log: logs/test-runs/2026-08-24-detached-servers-and-shell.log

### 2026-08-24 - The done-guard fired on 1 of 15, and skip was the door
Files edited: `shamsu/agents/simple_contract.py`, `tests/test_contract_evidence.py`
What changed: replaying all 15 assistant replies from
`F:/Work/shamsu test - 24aug/demo-3/asteroid` through `looks_like_a_done_claim`,
the guard fired on ONE - across a session that claimed success 16 times on a
game whose `initGame()` had never once been called. Three holes. (1) The phrase
list was fitted to an earlier run's prose and missed the shapes this model uses:
"All bugs fixed!", "Contract Complete", "Phase 2 Complete", "the game is now
running", "I've fixed the rendering issue" - now covered by `_DONE_PATTERNS`.
(2) `body.endswith("?")` exempted the WHOLE reply, so a message headed "Phase 2
Complete" that closed with a next-steps menu ending "Something else?" was waved
through on one character; the exemption is now scoped to the sentence that IS
the question. (3) Skip was pass with the evidence check removed - and the
pass-refusal message points the model straight at it. Live, `contract_assert_pass`
was refused for want of an observation and the next call was
`contract_assert_skip` on the same assertion, then five more, six of ten in three
minutes, ending "Contract Complete"; a10's skip REASON was "npm install completed
successfully - evidenced by existence of package-lock.json", a pass justification
posted through the skip door. `unproven` only ever looked at PASSED, so skips were
invisible to the guard and an all-skipped contract returned "" outright. New
`Contract.skipped`; `render()` no longer tells a skipped contract it can report
the task finished; `done_guard` names skips beside writes and quotes a skip reason
back when it reads like evidence. Skip still resolves - a guard with no exit is a
deadlock - it just stopped being silent.
Tests: 11 new in `tests/test_contract_evidence.py` incl. the 10 real phrasings and
the trailing-question case, both replayed from the session. Detector measured on
the real corpus: 12/12 caught, 0 false positives, was 1/12. Full suite 3954 passed,
2 skipped | Log: logs/test-runs/2026-08-24-done-guard-and-skip-door.log

### 2026-08-23 - The web portal: the plain link, the black hole, the disconnection
Files edited: `shamsu/webui/server.py`, `shamsu/webui/api.py`,
`shamsu/webui/local.py`, `shamsu/webui/cli.py`, `shamsu/webui/static/app.js`,
`shamsu/webui/static/app.css`, `shamsu/control/runner.py`,
`shamsu/runtime/turn_stream.py`, `shamsu/cli/tui.py`, `shamsu/cli/repl.py`,
`tests/test_web_access.py` (new), `tests/test_webui.py`, `tests/test_tui.py`
What changed: four reported faults, all reproduced before being fixed.
(1) The shell was served without a token - it has to be, the browser has
nowhere to put one before the page loads - while every `/api/*` demanded one,
so the bare `http://127.0.0.1:8765/` rendered a complete-looking app that then
401'd on every request. `requires_token` is now False on a loopback bind and
the link is the bare address; the token returns for any non-loopback bind, or
with `SHAMSU_WEB_TOKEN=1`. What still guards it: loopback, and the existing
foreign-Origin refusal. What it no longer guards: another program on this
machine can drive the agent - deliberate, and stated in the docstring.
(2) `_looks_like_id` only rejected path-shaped strings, so a prompt for session
`undefined` came back `202 accepted` with a queue id and then ran against a
thread nobody could open - no answer, no error, no trace. `session_exists` now
gates the route. A worker that raised also said nothing to the stream, leaving
the browser on `turn.start` for ever; it now publishes `error` + `turn.end
status=failed`.
(3) The real one behind "it didn't send to the cli": the portal builds its own
`QueuedRunner` with its own `TurnStream`, and a renderer is attached to one
stream by whoever built it - so the CLI had no way to know the web's existed.
The surfaces were not out of sync, they were not connected.
`TurnStream.add_observer` watches every stream in the process; the framed TUI
registers one and shows a web or Telegram turn's prompt, answer and verdict in
that surface's colour - not its tool rows, which would make both logs
unreadable.
(4) The browser rendered `status.text` and dropped `event.data`, so the footer
now carries `rnd 14/24 · ctx 71% · 34 tok/s`, and a failed `turn.end` goes red.
Also corrected: three panels called the portal "read-only", which it never was.
Tests: 12 new in `tests/test_web_access.py`, 116 across the web suites, 131
with the TUI and parity suites. Six existing tests asserted the old token
behaviour or the read-only wording and were repointed, each saying so in place.
Not done: the 600s timeout both reported web turns died on - a model/context
problem, now merely visible rather than silent |
Log: logs/test-runs/2026-08-23-web-portal-phases.log

### 2026-08-23 - The approval handover could strand the terminal for 15 minutes
Files edited: `shamsu/cli/live_console.py`, `tests/test_tui.py`
What changed: `reading_input()` is re-entrant and keeps a `_PROMPT_DEPTH`
because one prompt can open inside another; the frame handover did not. With a
nested prompt `_suspend_frame` overwrote `self._handover` with the inner
waiter, so the outer one was never set and its `run_in_terminal` callable sat
on the full `HANDOVER_TIMEOUT_SECONDS` - 900 seconds of a frame that does not
come back, which is the terminal-you-cannot-reach this was built to prevent.
The other direction was just as wrong: the inner prompt closing released the
frame while the outer question was still on screen. Now depth-counted - first
suspend hands over, last resume takes it back - and `set_frame(None)` forces a
release at turn end so an unbalanced open cannot strand it. Not the modal-float
redesign; that is still open and wants an interactive session. Also
investigated R10 (47 of 90 tool calls were re-reads) and found no independent
defect: the dedup guard fired 22 times, elision clearing `_ranges_sent` is
correct, and exactly 4 distinct files were read against a cap of 4. It is
downstream of the impossible-range fix.
Tests: 5 added. Suite 3843 passed / 2 skipped / 0 failed. | Log:
logs/test-runs/2026-08-23-r6-r2-r8-r1-r5-fixes.log

### 2026-08-23 - A clock on a turn, a reasoning trace that survives, and the empty turn
Files edited: `shamsu/action_ledger/ledger.py`, `shamsu/cli/turn_render.py`,
`shamsu/agents/simple_chat.py`, `shamsu/agents/chat_state.py`,
`tests/test_simple_chat.py`, `tests/test_action_ledger_storage.py`
What changed: (R7) the ledger recorded `thinking_chars: 2543, cot_path: ""` -
how much reasoning there was and none of it - because `full_artifacts` is true
only at `log_level: verbose` and the default is `essential`. Meanwhile the
screen clipped the same trace at 400 chars, so the user saw "Let me: 1. Delete
..." and could not recover the sentence naming the three files it was about to
delete. The trace is now written at every log level (bounded, redacted as
before), the display budget is 1400, and `_clip` cuts at a sentence, line or
word boundary instead of a fixed byte. THIS NARROWS A DELIBERATE POLICY - the
failing test was right, and is now split in two so the narrowing is explicit;
reverting is one `if self.full_artifacts:`. (R4) A turn had a round ceiling and
no clock: 24 rounds against a 600s per-call timeout, and
`removes_duplicate_definitions` reached the ceiling in 12 of 15 samples.
`DEFAULT_TURN_BUDGET_SECONDS = 1200`, checked between rounds so a generation is
never cancelled mid-flight, reporting what the turn managed.
`SHAMSU_TURN_BUDGET_S` overrides, 0 disables. (R3) 158 of 1385 assistant turns
in the baseline run were empty of both text and tool calls - 11%, ~9% of wall
time - and all 158 came directly after a tool result: a reasoning model
thinking and not narrating. Those are now re-asked once with `think: False`
rather than nudged. My first attempt made that a per-round flag, which the
retry's own `continue` reset - a test caught it before it shipped; it is a
per-turn budget now.
Tests: 12 added. Suite 3838 passed / 2 skipped / 0 failed. | Log:
logs/test-runs/2026-08-23-r6-r2-r8-r1-r5-fixes.log

### 2026-08-23 - Four defects the 240-attempt run and the demo2 logs found
Files edited: `shamsu/tools/agent_tools.py`, `shamsu/agents/simple_chat.py`,
`tests/test_file_tools.py`, `tests/test_simple_chat.py`
What changed: (R6) `read_file` clamped an impossible line range instead of
refusing it - `read_file(player.js, 145, 20)` returned `ok: true`, "Read file."
and ONE line, with `end_line` silently rewritten to 145. 8 of 47 ranged reads
in the F:\Work\demo2	est2 session were impossible and every one was answered
with a success; that payload went out five times and two turns ended on "I
stopped after 24 steps". It now refuses and names the call that was meant, and
the sibling case - `start_line` past EOF returning an empty successful read -
went with it. (R2/R8) `delete_file` was the only destructive tool with no
guard at all: 11 of 15 eval samples destroyed the real database in round one,
and live the model proposed deleting three source files to escape a repair
loop, stopped only by a human at an approval prompt. `_refuse_blind_delete`
now refuses an unlooked-at ambiguous target and any delete proposed while the
repair counters are hot. (R1) The prose nudge fired on files the turn had
already written - 5/5 false on edit cases; `changed` was in scope at the call
site all along. (R5) `asks_only_for_words` matched verbs, not questions, so
the nudge fired ten times on a pure Q&A prompt; the question test now runs
before the change-verb veto, with a polite-imperative veto so "Can you rewrite
X?" stays work. That last one exposed `rewrite` and `replace` missing from
`_CHANGE_VERBS` entirely.
Tests: 16 added (5 R6, 6 R2/R8, 5 R1/R5). Suite 3826 passed / 2 skipped / 0
failed. | Log: logs/test-runs/2026-08-23-r6-r2-r8-r1-r5-fixes.log

### 2026-08-23 - Harness nudges stop becoming things the user asked for
Files edited: `shamsu/agents/chat_state.py`, `shamsu/agents/simple_chat.py`,
`tests/test_simple_chat.py`, `scripts/overnight-eval.ps1`
What changed: RC9. `origin=ORIGIN_LOOP` was already set at the injection sites
and already written to `messages.jsonl`, but `ChatMessage` had no `origin`
field and `_hydrate_records` never read one - so `_digest`, which iterates
those objects, could not tell a nudge from a request and recorded every
`role: user` message as "you asked". The digest is what survives compaction, so
a long session's permanent record filled with instructions nobody gave. The
field now rides on the message and off disk, `_digest` skips loop-authored
turns, and `simple_chat.py:2777` (the contract nudge) - the one injection site
with no tag at all - is tagged. Second, smaller: `_history_pressure` measured
every hydrated message against the budget for the tail that is actually sent.
That ratio was nonsense as a number (13.69 on a 265k archive) but INERT - its
only consumer thresholds it at 0.6 and both measures cross 0.6 together, proven
over 2..320 exchanges. Kept as a correctness fix, explicitly not a performance
one; expect no eval delta from it.
Tests: 10 added (6 for RC9 incl. a source-level guard that fails if a future
nudge is added untagged, and a resume/old-transcript pair; 4 for the pressure
measure incl. the equivalence pin). Full suite 3709 passed / 2 skipped / 0
failed, plus tests/test_tui.py 84 passed. | Log:
logs/test-runs/2026-08-23-rc9-digest-and-honest-pressure.log

### 2026-08-23 - Four TUI bugs from one screenshot, and prompts coloured by surface
Files edited: `shamsu/cli/tui.py`, `shamsu/cli/repl.py`, `tests/test_tui.py`
What changed: (1) `console.status` is a rich Live that redraws by emitting
`\r` and the line again ~12x/sec, and `LogPane` split on newlines only - so
every spinner frame became a row and thousands of `Working...^M` filled the
pane. The pane honours `\r` as a line overwrite now, which also fixes npm, pip
and pytest progress output. (2) The serious one: `ANSI()` consumes the SGR
codes it knows and passes the rest through AS TEXT, and prompt_toolkit writes
fragment text verbatim - so a `\x1b[1A` in the pane physically moved the real
cursor and scrambled the sidebar. Private-mode CSI is stripped before parsing
(`ANSI()` renders `ESC [ ? 25 l` as the literal text `25l`) and every remaining
C0 control character after. (3) The input box was one line, which is useless
for the PRD or traceback that actually starts a turn: multiline now, Enter
submits, Alt+Enter opens a line, grows to 8 rows. (4) A SERVICES panel - model,
Telegram poller, web portal - sampled on a 5s TTL rather than polled, because
the sidebar repaints 5x/sec and those answers come from a SQLite lease table;
anything unreachable reads a grey `unknown` rather than raising inside a
renderer and taking the frame down. Kept out of `TurnTelemetry` on purpose:
that is fed by the turn stream and belongs to one turn, these outlive turns and
belong to other processes. (5) Prompts, logs and answers are now distinct: a
prompt is coloured by WHERE IT CAME FROM (cli green, web blue, telegram
magenta, unknown amber) with a gutter mark per surface, the answer is white
with its own mark, and logs keep whatever colour rich gave them so they stay
the neutral background. Both channels differ per surface because colour alone
is not a distinction. `TurnEvent.source` already carried the surface; a turn
begun elsewhere is echoed into this pane through `absorb_for_display`.
Tests: 72 in `tests/test_tui.py` (was 49), 227 across the neighbouring suites.
Full suite not run - a concurrent session's contract-evidence work has 10
unrelated reds in `test_simple_chat.py` |
Log: logs/test-runs/2026-08-22-framed-tui.log

### 2026-08-23 - The TUI sidebar says what a turn COST, in colour that means something
Files edited: `shamsu/cli/tui.py`, `shamsu/cli/live_console.py`,
`shamsu/agents/simple_chat.py`, `tests/test_tui.py`
What changed: the frame worked and said almost nothing - rounds, context, files,
contracts, two queue depths, all state and no cost, so a turn that ran 22
minutes and changed nothing looked like a productive one. `TurnTelemetry` now
counts model calls and seconds, tool calls and seconds, failures, and the
busiest tool; the sidebar shows `model 12 / 8m12s` against `tools 14 / 1s`
(where the time went), `failed 4` (how much did not land) and
`repeated contract_status x8` (the signature of a stuck run, amber from three).
Each is a documented failure mode of this harness, not a statistic someone might
like. Model duration rides on the event as `model_seconds` instead of being
parsed back out of the sentence "model responded in 41s". Colour is
load-bearing: green/amber/red mean fine/tight/out-of-room everywhere, on the
same 60/80 thresholds `CliTurnRenderer` already uses, and the round budget warns
as it runs out because running out is how a turn ends with nothing. The
statusline became coloured segments with an amber RUNNING / green READY block.
One layout bug fixed on the way: a 12-wide label column truncated
`contract_status x8` to `contract_status x`, throwing away the only number on
the row.
Tests: 49 in `tests/test_tui.py` (was 40), 66 in `tests/test_live_console.py`
still green |
Log: logs/test-runs/2026-08-22-framed-tui.log

### 2026-08-22 - The contract signed its own homework
Files edited: `shamsu/agents/simple_contract.py`, `shamsu/agents/simple_chat.py`,
`tests/test_contract_evidence.py` (new), `tests/test_simple_chat.py`
What changed: a run in `F:/Work/demo2/test` built a Three.js game, marked all
seven of its own contract assertions passed, and reported "All requirements
have been successfully implemented". Loaded in a real browser the game drew
neither the ship nor a single asteroid - three arithmetic faults, each fatal,
all of them pixels-used-as-world-units or a property read off the wrong object.
`contract_assert_pass` required its `evidence` argument to be a NON-EMPTY
STRING and nothing else, while its own refusal message asks "what did you run,
and what did it say?" - and the seven paragraphs it accepted were fluent and
specific, a02's being an accurate description of the exact line that put the
ship outside the camera frustum. Evidence is now what the HARNESS saw: the loop
records commands that exited 0 and files it wrote, per session, and an
assertion may be marked passed only against one of those - recorded as
`verified_by` RUN or WRITE, since a write proves the text reached the disk and
not that it is right. The model's paragraph is kept but labelled `you said:`
beside `backed by:`. `done_guard` no longer stops at `contract.done`: a
resolved contract whose passes are all writes is told that writing the code is
not evidence that the code runs. The refusal names `contract_assert_skip` as
the honest way out, because a guard with no exit is a deadlock. Safe to require
only because `exit 125` turned out to be `DENIED_EXIT_CODE` - the approval bug
fixed in f0158c2, not a shell fault as I had said.
Tests: 12 new in `tests/test_contract_evidence.py`; 4 sites in simple_chat
seeded with an observation because they test assertion plumbing, not evidence
policy. Full suite 3743 passed, 2 skipped. Verified by replaying all seven real
payloads from the session: 0 of 7 now pass, was 7 of 7, and the done claim is
corrected |
Log: logs/test-runs/2026-08-22-contract-self-certification.log

### 2026-08-22 - The approval prompt, and four faults in one question
Files edited: `shamsu/safety/approval.py`, `shamsu/control/console.py`,
`shamsu/cli/repl.py`, `tests/test_approval_bug.py` (new),
`tests/test_permission_manager.py`
What changed: one `run_command` approval drew THREE panels, leaked two
coroutines onto the user's screen, and offered a two-key menu under a
three-key hint in which `a` meant Deny. (1) `asyncio.run(ask_here_or_anywhere(
...))` builds the coroutine as an argument and then raises if a loop already
owns the thread, so it was never awaited and the `except Exception` fell back
to the plain local prompt EVERY time - approve-from-anywhere was dead in the
REPL. Replaced with `run_coroutine_blocking(factory)`, which creates nothing
until there is somewhere to run it and gives the coroutine its own thread when
one is needed; prompt_toolkit's sync `prompt()` has the same shape one layer
down and now passes `in_thread=True`. (2) The single-key reader prints "press a
to always allow" and accepts `a` unconditionally, while the menu only looked at
`a` when `offer_remember` was True - which is False for `run_command` by
design - so the key the user was told to press silently refused the action: the
20-of-22 denials carried in memory since 2026-08-20. `a` now always allows and
only REMEMBERS when that was offered, and the hint names only the keys on
offer. (3) The fallback re-drew a question already on screen (`render=False`)
and `ApprovalWatcher`, whose job is announcing OTHER processes' approvals, had
no filter for its own (`mark_raised_here`). (4) "Approval resolved on ." - the
empty-field case of `decided_by`. Also wraps the shared prompt in
`reading_input()`, which it had never used.
Tests: 10 new in `tests/test_approval_bug.py` incl. one asserting on captured
RuntimeWarnings after a gc.collect(), 2 repointed in permission_manager - one
of which asserted `approved is False` for `a`, the bug written down as the
expectation. That makes five tests this session that guaranteed the defect they
covered. Full suite 3722 passed, 2 skipped |
Log: logs/test-runs/2026-08-22-approval-silent-deny.log

### 2026-08-22 - "old_string not found" was the answer to a correct edit
Files edited: `shamsu/tools/agent_tools.py`, `tests/test_patch_repair.py` (new)
What changed: the reported turn made four `patch_file` calls and two
`replace_symbol` calls on `js/main.js`, all refused, then read the file nine
more times and ended having changed nothing. The payload had two faults at
once. It was a SINGLE LINE of literal backslash-n - which is also why
`replace_symbol` reported "2 unclosed {". And every line the model had copied
out of `read_file` was one space too deep, because the read's line-number
gutter ends in a space and the model stripped `74|` while keeping the
separator: 18 of the 22 lines were exactly one space deep and the 4 that were
exact were the JSDoc header above the read's first line, typed from memory.
Both repairs already existed and neither could reach it - `_strip_line_numbers`
correctly requires every line to carry a gutter, and the decoded form was
tested for an EXACT match and discarded when it missed, so every line-based
repair then ran on a one-liner with no lines to split. Now: repairs run on the
sent text and then on the decoded text; a new matcher tolerates a leading
indent shift (unique match, 3-line minimum, returns the FILE's bytes); and the
replacement is re-indented PER LINE - each line that appears in `old_string`
moves by its own measured error and a changed line inherits the correction
above it. A single modal shift was tried first and silently dedented the lines
that were already right whenever they outnumbered the shifted ones.
Tests: 11 new in `tests/test_patch_repair.py`; full suite 3707 passed, 2
skipped. Verified by replaying all four real payloads through the real tool
against the real file: 2 now apply and `node --check` passes on both, 2 are
still refused because the model genuinely mis-typed the text |
Log: logs/test-runs/2026-08-22-patch-repair-escapes-and-gutter.log

### 2026-08-22 - A turn that did nothing for 21m52s and reported SUCCESS
Files edited: `shamsu/agents/simple_chat.py`, `shamsu/agents/loop_guards.py`,
`shamsu/cli/turn_render.py`, `tests/test_turn_verdict.py` (new),
`tests/test_loop_guards.py`, `tests/test_simple_chat.py`
What changed: four defects from one reported turn. (1) `status="done"` meant
"the loop returned without raising" - a claim about the PROCESS that every
surface read as a claim about the OUTCOME - so a turn that changed nothing,
failed four tool calls and printed "This answer was cut off." was badged
`✓ SUCCESS`. `SimpleChatResult.truncated` now exists, status is
error>stopped>incomplete>done, `error` is actually published, there is a
yellow `▲ INCOMPLETE` badge, and the verdict line carries the numbers that
already existed. (2) `ReadLoopDetector`'s two flags are one-shot and nothing
counted past them, so beyond eight reads there was no ceiling at all - hence
fifteen reads of one file with one guard line said about it. It now escalates
to `READ_LOOP_EXHAUSTED` and the loop ends the turn. (3) A hallucinated tool
name with no near match fell through to a dump of all 37 tools, the exact
repetition `closest_tool_names` was written to replace; `plan` now gets told
what to do instead. (4) The oversize-write refusal ended "Nothing you generated
is lost", which held for four messages before `_shorten_arguments` dropped it -
the payload now spills to `.shamsu/oversized/` and the refusal names the path.
The 8,000-char cap is unchanged; one data point is not grounds to reopen a
documented decision. Also removed six lines of dead duplicated code.
Tests: 18 new in `tests/test_turn_verdict.py`, 2 new in loop guards, 1 new in
simple_chat, 3 repointed - each had asserted the mechanism rather than the
property, so each guaranteed the bug it covered; full suite 3661 passed, 2
skipped |
Log: logs/test-runs/2026-08-22-harness-verdict-and-ceilings.log

### 2026-08-22 - The framed TUI: output pane, sidebar, and its own scrollback
Files edited: `shamsu/cli/tui.py` (new), `shamsu/cli/repl.py`,
`shamsu/cli/live_console.py`, `shamsu/safety/approval.py`,
`tests/test_tui.py` (new)
What changed: spec item 01 in full ("a real layout instead of a scrolling log")
and item 02 (the right sidebar), opt-in via `/tui` or `SHAMSU_TUI=1`. I had
argued a frame costs the terminal's scrollback; it does, and that is not a
reason - Neovim and lazygit take the alternate screen and scroll inside the
application, so `LogPane` does too: bounded, wrapped, wheel/PageUp/PageDown,
and it does not move when the sidebar or input repaint. The turn's output
reaches the pane by pointing the REPL's one `rich.Console` at a `PaneWriter`
for the length of the turn (`Console.file` and `.width` are settable), so
panels, markdown and diffs all land without a single call site learning a frame
exists. Three things the spec did not list and all of which bite: an approval
prompt is a second prompt_toolkit Application and cannot run inside the frame,
so `reading_input()` gained a matching CLOSE hook and the frame hands the real
terminal over via `run_in_terminal` for exactly the question's lifetime;
Ctrl+C never reaches the SIGINT handler in full-screen mode, so
`_RequestRunner.interrupt()` is a new entry point the key binding uses; and
mouse capture takes click-drag away from the terminal's own selection, so it is
toggled with F2 and the statusline says which state you are in. The bug the
tests caught was the one that mattered: the wheel did nothing, because `scroll`
applied its delta to an offset last computed for a different window height and
clamped straight back to the bottom.
First real run found the frame was scoped to a TURN, not the session: it
flashed up and dropped back to the ordinary prompt after every turn, with an
empty pane each time because it was a new pane. `FrameHost` now owns the
application for as long as the mode is on, `main()` reads its prompts from the
frame's input box, and the console is redirected for the frame's whole life -
so the scrollback is the conversation.
Tests: 40 new in `tests/test_tui.py`, incl. the real Application driven over
`create_pipe_input()`; 66 in `tests/test_live_console.py` still green. Not
proven: a real Windows terminal - this session has no TTY, so the alternate
screen itself is untested |
Log: logs/test-runs/2026-08-22-framed-tui.log

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
