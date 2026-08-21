# Changelog

All notable SHAMSU release changes are documented here.

## Unreleased

### Changed - the terminal paints a turn instead of listing it (2026-08-21)

Every action row was `[dim]{text}[/dim]`: one grey for a successful read and a
failed `run_tests` alike, and a live 3B that called `contract_status` eight
times was eight identical lines you had to count. The CLI renderer now paints:

- An icon, a verb, the file and a duration per row, with failures in loud red
  and the reason beside them.
- A colourised diff snippet under a write, so you can see it did the right
  thing without opening the file.
- Reasoning traces dim and italic, indented, so they never compete with the
  actions.
- Approvals announced in yellow and answered in green or red.
- A `SUCCESS` / `FAILED` badge closing the turn. The verdict was previously not
  printed at all - a turn ended by scrolling off with no statement of whether
  it had worked.
- A run of identical calls collapsed to one row plus `x8`, held ACROSS the
  model replies between them, which is the shape a real contract loop has.
- A spinner that names the current action and carries the context meter and the
  round budget: `read_file calc.py | ctx 68% (22.3k/32.8k) | rnd 4/24`. Both
  numbers already existed and were shown nowhere; you found out you had filled
  the window by watching the run degrade. The meter goes yellow at 60% and red
  at 80%.

There is no `rich.Live` layout and no pinned input box. `simple_feedback.py`
reads raw keystrokes from the same terminal during a turn so a run can be
steered mid-flight, and a display that owns and repaints the screen from
another thread fights it. `console.status` coexists because it is transient.

The renderer reads the turn stream, not the ActionLedger. The ledger is a disk
writer whose every call site swallows exceptions, so a UI built on it turns a
rendering bug into a blank screen; it also carries no status events, so the
spinner would have no source. What the request behind that actually wanted -
the terminal and `log-summary.md` never disagreeing - is now asserted by
`test_the_terminal_the_phone_and_the_session_log_agree`, which runs one turn
and checks all three surfaces report the same actions in the same order.

### Added - four things the turn stream never carried (2026-08-21)

You cannot render what nothing emits. `simple_chat.py` gained emit calls only,
no change to loop logic:

- `tool.result` now carries `duration_ms`, `target` and the unified `diff`.
- A `reasoning` kind: the thinking channel reached the ledger but never the
  stream, so no surface could show it.
- `approval` events: the loop never published one. The question went straight
  to its own Console from inside a tool, so every surface watching a turn saw a
  gap where a human was being asked something - and on the terminal that is
  exactly why the prompt and the spinner used to collide.
- A status tick at the start of each tool and after each model call. Heartbeats
  only fire every five seconds, so on a fast model the spinner said
  "thinking..." straight through a file read and never named what it was doing.

`reasoning` and `approval` are deliberately not body kinds: a trace belongs to
the response that produced it and an approval to the write it gated, so neither
is a separate action. The CLI renders both from `data`. Parity is untouched.

### Changed — the log is two Markdown files per session (2026-08-21)

The readable log was nine places at once: one `report.md` per run, and beside
it eight typed subfolders under `.evidence/` — `prompts/`, `reasoning/`,
`responses/`, `tool-results/`, `commands/`, `contexts/`, `diagnostics/`,
`mutations/`. Eight folders is not eight times the information; it is one story
cut into eight piles sorted by payload type, which is the one key nobody
searches on. A session now carries:

    .shamsu/sessions/<session-id>/
        log-summary.md     every action, one line each, in order
        log-detailed.md    the same actions, with the payloads, anchored
        attachments/       flat; only what was too big to inline
        session.json
        messages.jsonl

`log-summary.md` is the index you skim — icon, title, surface, outcome,
duration, and a `detail` link where there is more to see. `log-detailed.md` is
the same sequence with prompts, diffs, command output and reasoning traces
attached, each under an anchor the summary links to. Both are appended as the
turn runs, so a session killed mid-turn keeps everything up to the moment it
died.

Five things the old report could not show:

- **Reasoning** renders as a collapsed sub-panel *inside* the model's own
  entry, not a separate file in another folder. A `<think>`, `<thought>` or
  `<reasoning>` block leaked into the visible answer is pulled out and rendered
  there too, so the trace reads the same way whichever way the model returned
  it.
- **Approvals** are their own row, with the request and its resolution paired
  into one line and both timestamps in the panel — a run that sat four minutes
  waiting for a human used to look identical to one that spent four minutes
  thinking.
- **Retries** group: consecutive attempts on one file emit as
  `↺ Write attempts — config.py · 1 of 2 kept`, with the superseded attempt
  struck through. They were two unrelated rows and the reader had to notice the
  filename matched.
- **Surface badges** name where a row's input came from, so a message steered
  in from a phone is distinguishable from the local prompt that started the run.
- **Overflow**: a payload over 2,400 characters is written to `attachments/`
  and linked, with eight lines of head kept inline.

The eight typed subfolders became one flat `.evidence/attachments/`. The kind
that used to be the folder moved into the filename (`model_0000.prompt.txt`,
`.reasoning.txt`, `.response.txt`) — all three used to be `model_0000.txt` and
would otherwise have collided. The JSONL evidence files stay where they are:
`evidence_outcome()` computes every run's terminal status from them.

`report.md` is gone. `store.report_path()` returns the session's
`log-summary.md`, falling back to a run's own `report.md` (or the older
`narrative.md`) when one exists, so `/run report` still works on runs recorded
before this change. `/run prompt` and `/run cot` read the flat folder first and
the typed folders second, for the same reason.

`essential` and `verbose` still differ, but the line moved. With one document,
`essential` had to withhold the model's own words to stay readable; with two,
the summary stays skimmable on its own, so the response and the reasoning trace
— the two things you debug a small model with — are kept at both levels.
`verbose` adds the prompt that was sent and the context payload.

### Changed — the summary is titles-only (2026-08-21)

Aligned to the Turn Log Viewer design, whose governing rule is that
`log-summary.md` is "deliberately titles-only - each line is a link, not a
description". A tool call and its result are one action to a reader and are now
one row; they were two, and the second quoted the result, which turned a
24-round turn into a fifty-row wall half of it pasted diffs. One word survives
past titles-only: a failed tool is marked on its row, because a live 3B run
called `contract_assert_pass` seven times and was refused every time, and
without the marker those are seven identical rows.

Four smaller gaps closed at the same time: "Building context" is a row at both
levels (the pack itself stays verbose-only); system notices such as "context is
filling; eliding older tool payloads" are a row type in both files, never a
link, and now reach the log at all rather than only the console; the verdict
carries its one-line reason ("2 files changed, checks passed"); and the turn
header carries the full timestamp and the surface it came from.

### Fixed — a self-executed write never reached `changed_files` (2026-08-21)

`replace_symbol` and `append_file` are run by the chat loop itself rather than
handed to the tool registry, and the registry is what journals a mutation. So a
turn whose only edit was a `replace_symbol` recorded no mutation at all: live
2026-08-21, `calc.py` was correctly fixed and the run closed `failed` with
`changed_files: []`. That also blinded `evidence_outcome()` and meant the retry
grouping could never fire for those tools. They now journal an `applied`
mutation with `rollback_available=False` - there is no transaction and no
backup, so promising a rollback would be a lie. Registry writes are untouched
and are not double-counted.

The verdict reason was fixed with it: it now always states the changed-files
fact, so a failed turn can no longer report "checks passed" as its whole story
when the check in question was a syntax verdict on a file it only read.

### Fixed — simple mode never recorded its tools or model calls (2026-08-21)

The readable log is built entirely from the ActionLedger, on the documented
promise that every execution path records its tools there. Simple mode, the
DEFAULT path, never did: it called `log_event` for verification verdicts and
nothing else. The first live run of the new log came back with approvals and
file writes in it and no sign of what the agent read, ran, or said. Tool calls,
tool results, model calls, and reasoning traces are now recorded from
`_run_tools` and `_call_model`.

`messages.jsonl` is untouched, so `evals/harness.py::_turn_telemetry` still
counts rounds and tool calls exactly as before — held down by a test that runs
a full turn beside a transcript and asserts the counts still come back.

### Fixed (the reported failure, from a live transcript, 2026-08-21)

Run against the file it was reported on - `test-shamsu/test1/js/main.js`, 582
lines, over the write cap - on qwen3.5:9b. The report reproduced exactly: 24
rounds, 319 seconds, nothing changed. The file's defect was never a syntax
error; four functions are declared TWICE, and in JavaScript the later
declaration silently wins, which no parser can see.

The transcript named the cause. The model had diagnosed it correctly and tried
the right fix - `replace_symbol(symbol="handlePauseMenuAction(action)",
content="
")`, delete the duplicate - and was refused twice with
"replace_symbol needs a filepath, a symbol and content", then ran out of rounds.
Two defects behind one refusal: the symbol carried the signature the outline had
just shown it, and empty content read as a missing argument rather than an
intent. There is no other way to remove a function - `patch_file` would need the
exact text of a thirty-line body.

- **`replace_symbol` deletes when given empty content**, and accepts a name with
  its signature. Deleting a DUPLICATE is allowed; deleting the last definition
  is refused and names the deliberate route, because two legal steps otherwise
  removed every definition of a function on a live run - a hole opened by this
  fix, not one it inherited.
- **The contract tools answer to the names the model uses.** 23 calls, none
  using `assertion_id`, 17 carrying the right id under `assertion_index`,
  `contract_id`, `claim`, `claim_id` or `assertion` - the `search_files` defect
  again. With exactly one assertion open, evidence alone now resolves to it.
- **Overlapping re-reads are answered from what was already sent.** The old
  guard compared signatures, so `105-210` twice was caught and `100-215` against
  `100-250` was not. 90% of the newly requested range, not of the union.
- **`read_symbol` returns EVERY definition of a name.** A tool that cannot
  express "show me both" cannot be used to remove a duplicate.
- **`run_command` is no longer silently denied** to any caller using
  `approval_override` - the headless runner, `shamsu run`, the eval harness.
  File writes were auto-approved while every shell command hit a console prompt
  with no TTY behind it. The benchmark had recorded that as model failure for
  five days.
- **`patch_file` cannot delete the last definition of a symbol**, and
  `read_file` is withdrawn for the turn after three reads of one path come back
  from cache - announced once, pointing at `replace_symbol`, restored by the
  first edit that lands.

### Added (efficiency is measurable now, 2026-08-21)

- **`EvalResult` carries `rounds` and `tool_calls`**, read from the session
  transcript rather than reported by a driver. `duration_s` conflated model
  speed with round count, so "correct in 16 rounds" and "correct in 24" scored
  identically and the only way to tell them apart was reading transcripts by
  hand.
- **`evals.diff` prints cost per ATTEMPT and below the line.** The verdict never
  reads it: getting cheaper while breaking a case that used to pass is still
  REGRESSED, and spending more rounds to fix a case you used to fail is still
  IMPROVED.
- **`removes_duplicate_definitions_without_losing_anything`** - the real file's
  shape, reproduced rather than copied. Its check counts declarations INCLUDING
  INDENTED ONES: a `grep "^function"` reads a nested definition as absent, which
  produced a false data-loss alarm on one day and missed a real one on the next.

### Added (context control and honest context reporting, 2026-08-21)

- **`/context window [tokens]`** - read or set the context window from the chat.
  The setting has existed install-wide since the web portal needed it
  (`runtime/settings.chat_max_ctx`, read live by `simple_chat.max_ctx`) and the
  terminal, the surface people actually use, had no way to reach it: changing a
  window meant exporting an environment variable and restarting. Says where the
  current value came from, and says so when an environment variable is
  overriding what was just saved, rather than reporting success for a change
  that will not take effect.

### Fixed (three status commands were reporting the legacy loop, 2026-08-21)

- **`/context show` reported a 2,000-token per-tool-result cap while the default
  loop used 8,000.** It read `chat_loop._CHAT_MAX_CTX` and
  `_TOOL_RESULT_MAX_TOKENS` - constants belonging to the legacy path, frozen at
  import, and disagreeing with simple mode. A status command that lies about the
  number most responsible for filling the window is worse than no status
  command. It now reads simple mode's live values and shows the reply reserve
  as a share.
- **`/context compact` described `ContextBudgetManager`'s threshold**, which is
  the legacy compaction, not the one running. It now reports the real thing:
  window, summary cap, compaction count, elision count, and the distinction
  between them.

### Fixed (context construction: shares, not flat constants, 2026-08-21)

- **The reply reserve could be the entire window.** `max(4096, ceiling // 4)`
  returned 4096 at every window below 16k - 50% of an 8k window and **100% of a
  4k one**, leaving nothing for the prompt. Unreachable while 32k was the only
  setting anyone used; `/context window` makes it reachable and
  `_shrink_for_oom` was already walking sessions down into it. The floor now
  gives way rather than eating the window, capped at a third.
- **One tool result could be 97.7% of an 8k window.** `MAX_TOOL_RESULT_TOKENS`
  was flat at 8,000 - a quarter of a 32k window and nearly all of an 8k one.
  Now a share with that value as its ceiling. This is the same defect
  `output_reserve` already fixed once: *"a fixed 4096 reserve is what starved
  simple mode."*
- **The prompt no longer claims a past on a fresh thread.** *"Earlier messages
  in this conversation are real... including turns you can no longer see"* was
  sent on every turn including the first. Now a conditional section, gated on
  the session having turns behind it. (Honest note: this removed a false claim
  but did NOT fix the reply that exposed it - a 3B still opens with an apology
  when given a non-coding instruction, and the same model on the same fresh
  thread answers a CODING request cleanly. The prompt was wrong; it was not the
  cause.)
- **A reply that is nothing but a tool-call object is no longer the answer.**
  `parse_model_turn` salvages a leaked call only on an exact name match, which
  is right for prose - an unregistered name mid-explanation is an example, not
  a call. But when the whole reply is the object there is no prose for it to be
  an example in, and live 2026-08-21 a turn answered
  `{"name": "run_file", "arguments": {"filepath": "hello.py"}}` as its finished
  answer, for a tool that does not exist. The closest-match correction never
  fired because the call never reached dispatch. Now nudged, with the nearest
  real names.

### Added (agent loop phase 3 - a plan that is written down and shown again, 2026-08-20)

- **A job with parts is asked to write them down.** `contract_create` has been
  offered all along and a model that does not think to call it never does; new
  `agents/plan_anchor.py` asks once, naming the call rather than stating a rule.
  ("Plan before you start" has been in this project's prompts before and did not
  survive contact with a 3B.) Conservative on purpose: a false positive costs a
  round AND anchors the model to a plan it had no reason to write, while a false
  negative costs nothing.
- **And shown it again every turn.** This is the half that was missing. The
  contract has held ordered, persisted, individually-checkable items for weeks,
  and reached the model only if it called `contract_status` - so the thing meant
  to keep a multi-step task on the rails was invisible to precisely the model
  that had lost the thread and stopped asking. It is now re-injected into the
  grounding block, capped, and dropped once every item is resolved, because a
  finished contract is history rather than a plan.
- **A read-only `plan` category.** smallcode's `planner.md` persona is a strong
  planner for one reason visible in its frontmatter - `tools: [read_file,
  find_files, search, ...]`, no write tools, so it *cannot* skip to
  implementing. That discipline needs no sub-agent and no second model call
  here: it is a tool category with the write tools left out. "how should we
  approach the refactor?" now routes there rather than being handed the write
  tools because `refactor` outscored the question.

  On smallcode's step cursor, which is NOT ported: their own code says *"Don't
  auto-advance - let the model explicitly mark completion. Auto-advance leads to
  drift in long traces."* `contract_assert_pass` already is that mechanism.

- **Adaptive retry temperature** (`src/model/adaptive_temp.js`): first retry
  colder, second warmer, then back to the configured value. At one fixed
  temperature a retry produces the same strategy and the same mistake, which is
  how one payload went out nine times byte-for-byte.

### Changed (the eval diff stops being too blunt, 2026-08-20)

- A movement is now withheld only if it is flaky **and** not statistically
  significant (one-tailed Fisher exact, p <= 0.05, enumerated directly so the
  tool still runs in a bare checkout). The flat exclusion shipped that morning
  proved too blunt within hours of real use: it meant a case that was flaky
  BEFORE could never be shown to have been FIXED, however solid it became. The
  live case that exposed it - `4/7 -> 7/7` - is still correctly withheld at
  p~0.19; `1/20 -> 20/20` no longer is. Regressions are judged by the same test
  in the other direction, so a guard that quietly destroys a case cannot hide
  behind "it was flaky anyway".

### Added (read path and onboarding, 2026-08-20)

- **Both ends of a file nothing can outline.** Outline-first answered large CODE
  files; everything else - `.md`, `.txt`, `.csv` - still got a head clip, which
  is precisely what starts the dead end in SMALLCODE_GAP_ANALYSIS.md §2: the
  model patches against a half it was never shown. A 900-line changelog was read
  as its oldest entries, and then the model was asked to add one at the bottom.
  Now the first and last 60 lines arrive with the gap named and a pointer to
  `start_line`/`end_line`. Code is untouched: an outline shows the whole shape,
  which beats two arbitrary slices.
- **`This project: Python, tests: `pytest -q`.`** - smallcode's bootstrap, one
  line in the grounding block. Every piece already existed: the manifests are
  one `exists()` each and `detect_test_command` already read package.json
  scripts and pytest layouts. Nothing had ever summarised them into the prompt,
  so a model opening a fresh workspace spent three to five calls working out
  what kind of project it was and how to run its tests - every session. Silent
  when it knows nothing, because "Project: unknown" looks like an answer.
- **Claude/OpenAI-shaped tool names resolve.** smallcode's `normalizeToolCall`.
  A model trained on those transcripts reaches for `Edit`, `Bash` or `Grep` by
  reflex, and those are far enough from a SHAMSU name that even the new fuzzy
  matching found nothing - live, `Edit` fell all the way through to a re-listing
  of the whole roster.

### Added (agent loop phase 2 - the guards simple mode did not have, 2026-08-20)

New module `agents/loop_guards.py`, adapted from smallcode
`src/governor/early_stop.js` and `quality_monitor.js`. Simple mode already had
eight detectors - prose, promise and empty-turn nudges, the contract nudge,
truncation refusals, the unproductive-edit ceiling, repeated reads, the per-file
edit ceiling - every one written inline in a `_run_turn` now past 2,600 lines.
They work, and none could be tested without standing up a whole loop. The four
below are the ones that did NOT exist; the older eight stay inline for now,
because moving working, tested code is risk with no behaviour to show for it.

- **Read-loop detection** - soft at 5 consecutive read/search calls that produce
  nothing, firm at 8. Simple mode already caught the same read repeated three
  times, which is a different fault: that is a model losing track of what it
  has. Eight DIFFERENT reads producing nothing is a task with no terminal state
  - "review X" can always justify one more file - and no counter saw it. Reset
  by producing ANYTHING, not only by writing: an answer is production too.
- **Greeting regression** - a model replying "How can I help you today?" after
  eleven tool calls has lost the conversation. Deliberately matched on whole
  phrases and only when work has already happened this turn, so "Hi - I've added
  the handler" stays a normal answer.
- **Closest-match on an invented tool name.** `There is no tool called X.
  Available: <thirty names>` re-listed a roster already in the prompt the model
  had just shown it was not reading. It now answers with the nearest few, and
  with the exact name when only a `functions.` prefix was wrong.
- **Per-tool trust decay** - three consecutive failures demote, five withhold
  for the session. **A writing tool is never withheld**, which is the deliberate
  difference from smallcode: theirs may drop any tool, and dropping `patch_file`
  leaves a model that cannot edit anything - a worse state than the loop it
  prevents. A search returning nothing can be taken away; the ability to change
  a file cannot. Never empties the roster either.

### Fixed (a third exit before the turn gives up, 2026-08-20)

- **Four failed edits now change the approach instead of ending the turn.**
  The ceiling used to stop with *"I have stopped rather than keep guessing. It
  would help to tell me the exact text to look for, or to paste the few lines
  around the problem"* - an apology that hands the work back to the user, and
  the thing users actually reported when asking the agent to fix a syntax error
  it had introduced. Patching was one strategy and it was the only one tried.

  The loop now says so once, and names the call: read the file's outline,
  `read_symbol` the one function that is wrong, then `replace_symbol` with its
  complete new body. That is the tool which does NOT require reproducing the
  old text byte-for-byte - precisely the step that has been failing four times
  in a row. smallcode's equivalent forces a whole-file rewrite; here that would
  be a harder version of what the model is already failing at, and
  `MAX_WRITE_CHARS` would refuse it for any sizeable file, so it would swap one
  dead end for another.

  Once per turn. Offered twice it stops being a change of strategy and becomes
  the loop repeating itself at the model, so the second time it really does
  stop.

### Added (tools simple mode could not reach, 2026-08-20)

An audit of the registry against simple mode's roster found **36 of 43 tools
were never offered to the model** - built, tested, and unreachable. The clearest
symptom was already sitting in `BENCHMARK.md`: `rename_file_via_move_tool`, an
eval case NAMED after `move_file`, failing and annotated as model variance. The
only route to a pass was guessing `mv` against `move` against `ren` through
`run_command` and getting it approved.

- **`move_file`** - renaming is not a write plus a delete, and simple mode had
  neither half.
- **`delete_file`** - the other half of editing a project. Ships WITH `ask_user`
  deliberately: its own description tells the model to ask rather than guess
  between candidate targets, and until now that pointed at a tool simple mode
  did not offer.
- **`ask_user`, and the loop half that was missing.** The tool never blocked -
  it returns a structured question and expects the loop to end the turn on it.
  The legacy loop did; simple mode did not, so the tool sat unreachable while
  the prompt told the model to ask whenever a decision was the user's to make.
  A question now ends the turn as a normal answer (not a `stop` - nothing went
  wrong), and stands in the transcript as an assistant turn, so the next thing
  the user types reads as its answer. No pending-question store: the
  conversation is the store.
- **Read-only `git_status`, `git_diff`, `git_log`** - so the model can see what
  it actually did rather than what it believes it did. `_with_diff` shows one
  edit; these show the turn. Gated on the workspace being a repository, by the
  same rule as every other conditional family. The 19 mutating git tools stay
  out: `run_command` reaches them with approval and the risk classifier.

Web search and page fetch were **deliberately not wired**. They depend on an
auto-started SearXNG instance, and offering a tool that may always fail
contradicts the rule this codebase already committed under the title *"Offer
only the tools that have something to answer from."* They need a reachability
probe first, like the other conditional families have.

### Added (deterministic tool-category routing, 2026-08-20)

- **`agents/tool_classifier.py`** - a weighted regex scorer over the user's
  message that narrows the tool roster with no extra round, ported from
  smallcode `src/compiled/tool_router.js`.

  Simple mode had two ways to shrink the roster and neither covered the models
  this project ships on. `select_category` costs a full round trip and engages
  only at or below 16,384 tokens of context; `_without_unavailable_families`
  is free but asks *"could this tool answer at all?"*, never *"does THIS
  request need it?"* Above 16k the catalogue therefore went out whole -
  measured at **26 schemas and 3,196 tokens on every turn** of a 32k window,
  about a tenth of it, growing with every tool added. That tax is what made
  the seven tools above look expensive.

  Measured after: `fix the missing brace in game.js` sends 14 schemas / 1,885
  tokens, `run the tests` sends 10 / 1,200.

  **One deliberate difference from smallcode.** Their direct mode has no
  escape: a wrong guess strands the model with the wrong tools and no way to
  say so. Every narrowed roster here carries `select_category`, so a
  misclassification costs one round trip to correct - and an explicit choice by
  the model outranks the scorer. A guess with a way back is a different
  proposition from a guess without one. Low confidence, a near-tie, a greeting,
  or a request over 300 characters all send everything: a classifier unsure
  what a request wants is not evidence that it wants little.

### Added (agent loop phase 1, 2026-08-20)

- **`python -m evals.diff <baseline> <feature>`** - a mechanical verdict on
  whether a change helped, adapted from smallcode `bench/diff.js`. Exit 0
  improved, 1 regressed, 2 noise, 3 error, so it can gate CI. It exists because
  §31.1 scored anywhere from 1/7 to 5/7 across nine runs of identical code: at
  that variance nobody can read a delta out of two BENCHMARK.md tables by eye,
  and every behavioural change from here on is a change to a stochastic system.
  Three departures from smallcode, all forced by the fact that our harness runs
  N samples per case where theirs runs one:
  - A case's reward is its PASS FRACTION, not a boolean. `2/3 -> 3/3` is a real
    movement their model cannot express, and it is the size of movement a guard
    change actually produces.
  - A case flaky in EITHER run is reported and then held out of the verdict, on
    both sides of the delta. `render_report`'s footer has said *"a delta that
    lives entirely inside the flaky set is no delta"* for months and nothing
    enforced it.
  - Unequal sample counts REFUSE to compare instead of printing a caveat.
  A case that passed every attempt and now fails every attempt overrides a
  positive average outright - no mean should be able to buy back a behaviour
  that used to work every time.

### Fixed (agent loop phase 0, 2026-08-20)

- **A file a patch broke is no longer reported as one still being written.**
  An open block means two opposite things - the first section of a chunked
  write, or a closing brace a patch just ate - and nothing distinguished them.
  So `node --check: SyntaxError: Unexpected end of input` was thrown away,
  replaced with "that is expected part-way through - continue with
  `append_file`", and the whole report returned `ok: true`. A user asking the
  model to fix the file was asking it to fix something it had just been told
  was fine, and the advice it did get - append to the END - cannot close a
  brace missing in the MIDDLE. The exemption now requires that the write which
  last landed ADDED to the file: `append_file`, a creation, or any write whose
  diff shows a net gain in lines. A patch never qualifies. Where the file
  genuinely was being built in sections earlier in the turn, the real error is
  reported *and* says so, rather than one fact being suppressed to protect the
  other.
- **An unclosed block points at the innermost opener, not line 1.**
  `open_blocks` is the stack still standing, so its first entry is the file's
  outermost block - for a module wrapped in a class, line 1 - which sent a
  model repairing a missing brace to the top of a file whose damage was three
  hundred lines lower. The last entry is the nearest to where the text stops.
- **Two failures no longer cost a reasoning model its reasoning for the rest of
  the turn.** smallcode's rule is `isRepair && attempt > 1` - the model already
  overthought THIS solution. Ours read a turn-wide tally incremented by ten
  different things, including nudges that are not repairs, and never reset. Any
  write that lands now clears it.
- **The write-refusal and OOM stops stop replaying into history.** Audited
  against every message `_stop` can emit; these three were the only harness text
  still hydrating as assistant turns. "I refused all of them. Nothing was
  changed." is not something a model should learn is a normal way to end a turn.

### Added

- A per-call content cap for every tool that carries a payload
  (`write_file`, `append_file`, `patch_file`, `read_and_patch`,
  `create_and_run`): `clamp(2,000 | 0.85 x reply_cap | 8,000)` characters. This
  restores smallcode's 4x headroom ratio, where the model is never permitted to
  attempt a write large enough to exhaust its own output budget. The ceiling is
  llama.cpp's ~13KB tool-argument wall, which does NOT report
  `done_reason: "length"` and so was invisible to the existing truncation
  guard; the floor says the window is the wrong shape for the task rather than
  degrading to useless chunk sizes. `MAX_REPLY_TOKENS` is deliberately
  unchanged - the unit of work is bounded, not the budget.
- The number 60 lines is now stated in the system prompt AND in the schema
  description of every payload argument, not only enforced in the tool. A
  refusal names the strategy ("write the first 60 lines, then append_file each
  section") rather than only the limit, which turns an unrecoverable failure
  into a recoverable one: the content was fully generated and is merely
  rejected at the door.
- A pre-write gate that tests for TRUNCATION SIGNATURES rather than validity -
  an unterminated string or comment, a dangling operator, a bracket opened on
  the last line - for new files and for every language, closing the hole where
  both write-time gates bailed out when the target did not already exist and
  only understood Python. A first section with unclosed blocks passes, because
  a gate testing for validity would refuse every legitimate chunk.
- Continue-from-the-tail recovery in simple mode, language-agnostic: a file
  found stopping mid-construct is asked for ONLY the missing remainder, quoted
  against its own last twelve lines, instead of being resent whole.

### Added (read path, 2026-08-20)

- **Outline-first reading.** `read_file` on a file over 200 lines returns its
  outline - every class and function, its signature and its exact line range,
  bodies omitted - instead of the first 24,000 bytes. Head-clipping is what
  started the dead end in §2: the model patched from what it saw, `old_string`
  was in the half it never saw, the fuzzy retry missed too, and the whole-file
  rewrite was refused for being a partial read. Deterministic - Python through
  `ast`, braced languages through a declaration scan - rather than smallcode's
  LLM summary of the first 8KB, which derives a 2,000-line file's outline from
  its first ~200 lines and costs a full generation.
- **`read_symbol`**, the follow-up an outline earns: one function or class,
  exactly, from the same parse that produced the outline, so the range cannot
  drift from what the model was shown.
- **Line numbers on every read**, smallcode's gutter. `start_line` is arithmetic
  on a wall of text until the model has seen which line is which. The gutter is
  stripped back out of `patch_file` arguments, so copying what you were shown is
  safe.
- **`run_tests`**, which detects the project's test command (npm script, pytest
  layout, Cargo, go.mod, Makefile) instead of leaving the model to guess it, and
  runs it through `run_command` so approval and the risk classifier still apply.
- **`use_skill` and a skill index in the prompt.** The skill loader, its
  frontmatter parsing and its override rules all existed and nothing in simple
  mode had ever called them. Adds a `large-file-surgery` skill for the
  outline -> symbol -> patch -> verify workflow.
- **The system prompt is now `agents/prompts/simple_system.md`**, section by
  section, so the most-edited and least code-like thing in the agent can be read
  and changed without opening Python. Adds smallcode's acting rule: a model that
  writes one section and asks "what would you like next?" spends the turn on a
  question the user already answered.

### Added (symbol editing + Definition of Done, 2026-08-20)

- **`replace_symbol(filepath, symbol, content)`** - replace a whole function or
  class by NAME. `patch_file` could never do this cheaply: replacing a function
  means reproducing every line of the old one exactly, and a model that can
  write the new one will still fail to retype the old one. Three guards, live
  proven: content is re-indented to the original's column (a small model sends a
  method at column zero far more often than not); an edit that would stop a
  working file parsing is refused; and an edit that would silently DELETE
  members of a container is refused by name.
- **Definition-of-Done contracts** - `contract_create`, `contract_status`,
  `contract_assert_pass` / `_fail` / `_skip`, and a done-guard that sends a
  premature "the task is complete" back with the list of unchecked assertions.
  Stored on disk, because a `SimpleChatLoop` is rebuilt for every user message
  and a contract that does not outlive one turn is not a contract. `passed`
  requires evidence; `failed` counts as resolved, so the model can still REPORT
  a failure. `SHAMSU_CONTRACT=0` disables it.
  (Distinct from `verify/contract.py`, which DERIVES a contract from the
  prompt, and from `verify/dod.py`, which runs registry-declared checks. This
  one is authored by the model for the task in hand.)
- `read_symbol` on a container returns ITS outline rather than its body. Live
  2026-08-20 the model did exactly as told - read the outline, then read_symbol
  the class it needed - and got 313 lines back, because `export class Player`
  spanned lines 34-347. The outline had just saved the window and the next call
  spent it.
- A missing symbol now suggests the closest matches before listing the roster.
  The model asked for `initializePlayer`, `updatePlayerState` and `renderPlayer`
  in three consecutive calls; each time it was handed thirty names in one line
  and invented a fourth.

### Fixed

- **An append that breaks a working file is rolled back.** Live 2026-08-20 the
  model was shown a REPLACEMENT for `takeDamage` and appended it past the
  closing brace of the class, leaving the module unparseable - then appended the
  same eleven lines again. Structural counting cannot catch this (the block is
  perfectly brace-balanced), so the write happens, is judged by the real
  checker, and is undone. Silent when the file was already unfinished, or the
  guard would break chunked writing.
- **The prose nudge no longer leads with `append_file`.** It said "call
  append_file to add it to the end", and a model showing a replacement took that
  literally - which is what produced the corruption above. It now leads with
  `replace_symbol`, then `patch_file`, and offers append only for content that
  belongs at the end.
- `read only the functions you need` is no longer classified as read-only MODE.
  A run that fixed a real bug reported `contract violation: prompt forbade file
  changes but 2 changed`, because the spaced form matched. `read-only` and
  `readonly` still match unconditionally; the spaced form now has to prove it is
  not governing an object.

- **The truncation gate no longer judges a fragment as a file.** It ran on
  `patch_file.new_string` and refused a legitimate JSDoc block three times with
  "it ends inside a /* comment opened on line 23", then ended the turn blaming
  an output limit that had never fired. A patch replaces a region that may start
  inside one block and end inside another, and an append chunk is unfinished by
  design. The gate now runs only on `write_file` and `create_and_run`, where the
  payload really is a whole file; the size cap still applies to all five.
  smallcode caps payload size and checks nothing else.
- Reading an unchanged file again says so instead of resending it - eight
  `read_file js/game.js` calls in one turn were eliding the window to make room
  for copies of a file that had not changed. Unlike smallcode's version, the
  memory is dropped whenever an elision sweep runs, so the claim is true
  whenever it is made.
- Ranged reads are no longer counted as repeated reads. `_argument_summary`
  returned the filepath alone, so section 3 of a file read in pieces was
  answered with "you have already called this" - firing on exactly the strategy
  the outline now tells the model to use.
- The stop message after three refused writes names the cause it actually had,
  instead of blaming the output limit for a content refusal.
- The bundled `developer` skill said "Default to `write_file` with the COMPLETE
  file content" - the exact opposite of the 60-line rule the tool enforces. A
  skill that fights the harness is worse than no skill.

- A file still being built now reports its open blocks as PROGRESS ("3 block(s)
  still open - continue with append_file") instead of as a fault. Verifying
  after every chunk was correct and had to stay; reporting an unfinished
  section as broken would have sent the model repairing a file that was simply
  not finished yet. A file left open when the turn ENDS is still failed, so the
  run outcome cannot read a half-written file as a success.
- A write that GROWS a file no longer counts toward the repeated-edit ceiling.
  Live 2026-08-20, told to build a 1,500-line file, qwen2.5:3b chunked as asked
  but carried each section with `write_file`; five verified-clean sections in,
  the turn was stopped for "5 blind edits I cannot confirm". The exemption
  already existed for `append_file` and now follows the shape rather than the
  tool.
- Unknown models no longer silently drop to an 8,192-token window. The table
  was exact-match only, so `qwen2.5:3b-instruct-q4_K_M` - one quantisation
  suffix from a listed model - asked for 8k, which collapses the reply cap and
  shrinks every write cap derived from it. Family patterns are consulted before
  the fallback.
- `test_count_tokens_falls_back_when_asset_missing` left the tokenizer cache
  memoised on a missing asset, so every later `count_tokens` in the session
  silently used chars/4.

## 0.4.0b1 - 2026-07-20

### Added

- Full-request noninteractive harness with deterministic approval policy,
  dry-run, timeout, JSON result contract, and persisted-evidence validation.
- Canonical ActionLedger run folders for every prompt, including structured
  decisions, tool/model calls, context records, command output, diagnostics,
  mutations, verification, final output, and concise `/run show` inspection.
- Composite routing, mentioned-document normalization, shared workspace file
  policy, asynchronous memory finalization, TaskFlow PRD acceptance, and
  complete PRD-to-Django generation/browser checks.
- Web provider fallback and browser capability status, one-time first-run
  readiness report, expanded `/doctor`, and idempotent workspace schema
  upgrades that preserve historical evidence.
- Three-OS CI lifecycle validation and a deterministic Python/Django/Node/
  React/mixed release dogfood benchmark.

### Fixed

- False-success outcomes, swallowed patch/fallback errors, ungrounded compound
  routing, malformed approval semantics, and non-actionable Git probe errors.
- Missing optional run artifacts on read-only requests and incomplete run
  summaries.
- Unix lifecycle scripts failing under Bash because of CRLF line endings.
- SQLite web-cache connections remaining open until garbage collection on
  Windows; cache creation is now lazy for non-web prompts.

### Measured

- 1.266s import startup, 1.071s cold first answer, 0.307s slowest warm answer,
  90.9 MB peak RSS, and 52,128 bytes of run-log growth across five deterministic
  dogfood sessions on the release workstation.
- Default model tier: 11/12 cases at three samples; light tier: 9/12. Stochastic
  cases and tier limitations are documented rather than hidden.

## MVP Release

This MVP focuses on a local-first path from workspace understanding to
approval-backed project work on low-resource machines.

### Added

- CLI REPL with workspace-scoped `index`, `status`, `log`, `search`,
  `symbols`, `parse-prd`, `models`, and `django setup` commands.
- SQLite-backed workspace index with Python symbol extraction, snippets, and
  stale-file cleanup.
- Markdown PRD parsing, rule-based entity extraction, and `ProjectSpec`
  assembly.
- Deterministic Django fixed templates for generated projects using Django,
  DRF, Simple JWT, crispy forms, DaisyUI, HTMX, and SQLite.
- Natural-language routing for QA, code edit, bug fix, audit, test generation,
  documentation, and project-generation intents.
- Approval-backed command runner with workspace checks, command risk
  classification, blocked-command rejection, timeouts, captured output, and
  secret redaction.
- Django generated-project setup runner for `pip install -r requirements.txt`,
  `makemigrations`, and `migrate`.
- Approval-backed patch validation, Rich preview, apply, rollback, backups,
  failure restore, and post-patch re-indexing.
- Read-only git dirty-worktree helpers for safer edits.
- Local JSONL audit trail under `.shamsu/` for safety-sensitive events.
- Local Ollama runtime status and repair helpers.

### Changed

- README now includes the PRD format guide, generated Django project run guide,
  demo script, and explicit known limitations.
- CLI help now presents workflow examples and generated-project setup commands.
- Local logs and command outputs are redacted before display.

### Known Limitations

- SHAMSU is a workspace sandbox, not an OS sandbox or Docker isolation layer.
- Full PRD-to-Django project generation is branch-dependent until the complete
  pipeline lands on the default branch.
- Generated Django projects target local development with SQLite.
- PostgreSQL, Docker deployment, React SPA generation, file uploads, email
  delivery, and background workers are outside the MVP scope.
- Local model quality and speed depend on the installed Ollama models and host
  hardware.
