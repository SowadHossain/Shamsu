# Fix log — branch `small-shamsu`, 2026-08-19

One short entry per item: what changed, why that approach, and the decision the
code now makes. Full evidence lives in `TRUNCATED_FILES_REPORT.md`; status lives
in `ISSUES.md`. This file is the summary you read first.

Every guard below was proved by **removing it** and watching the named test fail
with the named message. A commit is not proof.

Suites: `tests/test_simple_chat.py` 254 -> 297 passed; `tests/test_agent_tools.py`
17 -> 34 passed. 0 failures. Ruff clean. 433 passed across every suite touched.

---

## C2 — the verifier certified files it never opened · `dbbaaa1`

**Files**
* `shamsu/agents/simple_verify.py` — new
* `shamsu/agents/simple_chat.py` — `_verify()`

**Why** `checked.append(relative)` ran *before* the `.py` extension test, so every
non-Python write came back as "no syntax errors" from a checker that never
parsed a byte — 572 times in one session, including a file with 21 unclosed
braces. The one signal that should have caught the truncation confirmed the
opposite, so the model reported the code complete.

**Approach** One checker per extension, three verdicts, and a file only reaches
`checked` if something actually parsed it. `node --check` when node is on PATH;
a bracket-balance scan when it is not, because SHAMSU runs on machines with no
toolchain installed and a checker that needs one is no checker at all.

**Decision**

```
file just written
├─ not on disk ............................. problem "file was not created"
└─ on disk
   ├─ .py/.pyi ............ compile() ........ ok | problem
   ├─ .json ............... json.loads ....... ok | problem
   ├─ .js/.mjs/.cjs ....... node on PATH?
   │                        ├─ yes .. node --check .. ok | problem
   │                        └─ no ... bracket scan ... ok | problem
   ├─ .jsx .ts .tsx .css .c .java .go .rs .. bracket scan .. ok | problem
   └─ anything else ....................... SKIPPED  <- the escape
```

**The escape** `skipped` is not a problem. A `.md` file nobody can parse is
reported as unchecked, so the model is never left repairing prose.

**Bias** The bracket scan stays silent when unsure — an apostrophe in a comment,
or a `/` it cannot classify as regex or division, resumes rather than inventing
a fault. A false *"your file is broken"* is the same defect as a false *"no
syntax errors"*, pointed the other way.

**Proof** Restore `checked.append` to before the verdict →
`test_verify_never_reports_a_file_it_has_no_checker_for_as_checked` fails with
`Checked game-plan.md: no syntax errors.`, the exact string from the log.

---

## C1 — truncated generations committed their writes · `b08d298`

**Files** `shamsu/agents/simple_chat.py` — `_run_tools`, new
`_refuse_truncated_write`, new `WRITING_TOOLS`

**Why** `_hit_the_length_limit()` worked the whole time. It was consulted in
exactly one place — where the prose answer is assembled — and nowhere near the
writes. So the harness knew the reply had been cut off mid `write_file` and
executed the partial call anyway, five rounds running, reporting each `ok`.

**Approach** Refuse, do not write-and-flag. `write_file` **replaces**: a
truncated write does not make a slightly short file, it destroys what was there
and keeps the half that fit. Only the *last* call of a severed generation is
refused — earlier calls in the same reply finished generating — and only if it
writes. Reads still run, or the model is stranded with no way to see the damage.

**Decision**

```
generation returned
├─ done_reason != "length" ................. run every call normally
└─ done_reason == "length"
   ├─ call is not the last one ............. run it (it finished generating)
   ├─ last call, read-only ................. run it
   └─ last call, writes to disk ............ REFUSE
      ├─ 1st ....... "send it in pieces: write_file then append_file"
      ├─ 2nd ....... "FIRST 60 LINES ONLY, then append_file per section"
      └─ 3rd ....... stop the turn and say so   <- the escape
```

**Also fixed** `append_file` was never in `MUTATING_TOOLS`, yet the log has
`Round 9 append_file -> ok` above a cut-off notice. `WRITING_TOOLS` answers a
different question — *would this call, cut off, damage the workspace?* — so the
two sets stay separate.

**Proof** Remove the branch →
`test_a_truncated_write_never_overwrites_a_file_that_was_already_good` fails with
`assert 'function update() {\n' == '// the whole working file\n'`.

---

## C10 — elision deleted the read and kept the wrong conclusion · `09f29ee`

**Files** `shamsu/agents/simple_chat.py` — `_elide_payloads`, new
`_current_file_reads`, new `_read_result_path`

**Why** `RECOVERABLE_TOOLS` asks *"can this be fetched again?"*. True of a file
read, and the wrong question — the right one is *"what is this result still
doing in the reasoning?"*. Assistant prose is not elidable at all, so the final
prompt held fifteen reads of `main.js` reduced to
`{"elided": "call read_file for the current contents"}` and eight surviving
sentences all asserting the bug was on line 426. The model re-derived line 426;
it was the only evidence left in the room.

The asymmetry is what made it self-reinforcing: the harness elided what it
**could** re-fetch and kept what it could not, and the un-refetchable thing is
the model's own speculation.

**Approach** The report's Fix 1, which it calls sufficient on its own: keep the
latest read per file verbatim, elide superseded reads of the same file as
before. Fifteen stubs of one file carry no information — and neither did fifteen
full copies.

**Decision**

```
old tool result, sweeping for space
├─ carries file CONTENT (has resolved_filepath + content)
│  ├─ newest read of this file?
│  │  ├─ yes, and it is the most recent read overall .... KEEP whatever it costs
│  │  ├─ yes, and protected total still under 35% ....... KEEP
│  │  └─ yes, but over the allowance / over 4 files ..... elide
│  └─ superseded by a newer read of the same file ....... elide
├─ write echo (resolved_filepath, no content) ........... elide
└─ shell output (not re-fetchable) ..................... compact head+tail
```

**The escape** is one layer down and already existed: protection stops a payload
being **shrunk**, not a message being **evicted**. `select_for_budget` still
drops what cannot fit, so protection can never deadlock a turn. A test holds the
assembled prompt under the ceiling with six oversized reads.

**Changed my mind mid-way** The first version released protection whenever the
elision target was still unreachable. That target is often unreachable because
of user prose, which elision cannot touch — so it fired constantly and
re-created the defect. The bound belongs at *selection*, not after.

**Proof** Remove the `id(message) in spare` check →
`test_the_current_contents_of_a_file_survive_elision` fails showing the live
stub `{"elided": "call read_file for the current contents"}`.

---

## C7 — a turn ending on a promise was accepted as done · `a7a5631`

**Files** `shamsu/agents/simple_chat.py` — new `ends_on_an_unmade_promise`, one
branch in the round loop

**Why** Fourteen turns ended with prose announcing an edit and no tool call,
every one on a colon, every one handed back as a finished answer.
`describes_an_unmade_edit` cannot catch it: it needs a fenced code block of four
lines or more, so it only fires when the model *shows* the code instead of
writing it. Here the model shows nothing. It promises, and stops.

This is the defect the user actually felt — *"I told it to read files but nothing
happened, the agent remained dumb."* It was not dumb. It stopped at the exact
moment it was about to act, and was told that was complete.

**Approach** Both halves required — last non-empty line ends in `:` **and**
announces an action — because a colon alone introduces the next paragraph and an
announcement alone opens one. It is a promise as the *final* word that means
nothing followed it.

**Decision**

```
reply with zero tool calls
├─ empty ................................... existing empty-reply nudge
├─ shows code + names a real file .......... existing prose nudge (C7 defers)
├─ output cap severed it ................... label partial   <- C1's case, not a promise
└─ last line ends ":" AND says "let me" / "I'll" / "I will" / ...
   ├─ nudge 1 ...... quote it back, "call the tool that does it"
   ├─ nudge 2 ...... same
   └─ 3rd time ..... stop the turn          <- the escape
```

**The escape** opens with *"I said I would take an action"*, a prefix
`chat_state.py` already filters on rehydration — so the harness notice never
becomes conversation the model imitates. That is RC3's lesson, where one harness
message was replayed 54 times and taught the model that "I ran out of room" is
how a turn ends.

**Proof** Disable the check →
`test_a_turn_ending_on_a_promise_is_not_returned_as_the_answer` fails with
`SimpleChatResult(final='Let me fix that:', rounds=1, tool_calls=0, stopped=False)`.

---

## Opened, not fixed

| # | Why it was logged rather than absorbed |
|---|---|
| C11 | C2 made the verifier honest about what it skips. 14 common extensions (`.html`, `.php`, `.rb`, `.yaml`, `.toml`, `.cs`) still have no checker. Scheduled after the patch cluster. |
| C12 | `09f29ee` landed RC10 fix 1 only. Fixes 2 (elide stale claims with the read behind them) and 3 (mark a re-read that follows a user correction) are not done, and closing C10 without saying so would overstate it. |

---

## C3 — a literal `
` in `old_string` could never match · `66fc252`

**File** `shamsu/tools/agent_tools.py` — `edit_file`, new `_decode_literal_escapes`

**Why** The model emits `
` as two characters where a newline belongs, mixed
with real newlines in the same string. That text is in no file, so it can never
match: 24 patch attempts and 0 successes in one session, 29 more in the next.
The harness's error was accurate and useless because it compared the mangled
string as given.

**Approach** The salvage principle already used for malformed tool calls — if
the raw form does not match and the decoded form does, decode it. `new_string`
is decoded with it, or the literal backslash lands **in** the file and one
corruption is traded for another.

**Decision**

```
old_string not found by exact match
├─ contains an unescaped 
, 
 or 	
│  └─ decoded form matches the file? ..... yes .. apply, and SAY so
│                                          no ... fall through
├─ whitespace / line-ending drift ........ unique fuzzy hit .. apply
└─ still nothing ......................... fail, and name the format mistake
```

**Narrow on purpose** `n`, `r`, `t` only. `\d`, `\s` and `\` are ordinary
content in a regex or a Windows path, and decoding them would corrupt the very
edit being made. A real `"\n"` in JavaScript source is left exactly as the
file has it.

**Reported either way** On success the message says the salvage happened — one
the model never hears about is one it makes every turn, and the next may not be
salvageable.

**Proof** Disable the decode →
`test_a_patch_whose_newlines_arrived_as_two_characters_still_applies` fails on
the log's own `main.js` payload.

---

## C6 — stall counters reset every time the user typed · `4dfc17b`

**File** `shamsu/agents/simple_chat.py` — new `SessionStalls`, `_run_tools`

**Why** `MAX_UNPRODUCTIVE_EDITS = 4` existed the whole time and would have
caught the failure. It never fired: `_unproductive` lived on `SimpleChatLoop`,
and `repl.py:4812` builds a fresh one per user message. The model failed four
times, the user typed, and it started again from zero. 29 patch calls, 11
distinct payloads, one sent **nine times** byte-for-byte.

**Approach** Key the store by session id, so `/new` gives a clean slate without
the `/new` handler having to know it exists. Signatures digest the *whole*
argument set — `_argument_summary` truncates, which would have made two
4,000-character patches differing only at the end look like one call.

**Decision**

```
writing call about to run
├─ this exact call already failed >= 2 times in this conversation
│     └─ NOT RUN: hand back the error it already had + what to do instead
└─ run it
   ├─ succeeded ......... forget every remembered failure for THAT file
   └─ failed ............ remember (signature -> error), count it

no-op mutations reach 4 (across turns) .... stop, tell the user, THEN reset
```

**Two escapes** A successful edit to a file forgets that file's failures — the
world changed, and a patch that could not match before may match now. And the
no-op counter resets once it has *fired*: the defect was a counter that reset
silently without ever tripping, but one that stayed hot would stop the next turn
before it started.

**Proof** Put the counters back on the loop object →
`test_the_no_op_counter_is_not_reset_by_the_user_typing` fails with
`a new turn wiped the count`.

**A test that was wrong first** The original version bound `_stalls` by hand and
passed with the fix removed — it exercised the store, not the wiring. It now
builds the loop through the constructor with a session id, the way `repl.py`
does.

---

## C8 — the same patch error returned 29x, unchanged · `a342fd1`

**File** `shamsu/tools/agent_tools.py` — `_nearby_edit_hint`

**Why** Every failure returned one sentence: *"Nearest similar line is line 424:
..."*. It never widened, never escalated, and never offered the actual text, so
the model had nothing new to reason from and recomputed the same call from
memory — which is where the error was in the first place.

**Approach** `read_and_patch` already carries the rule *a half-failure returns
the half that worked*; plain `patch_file` did not. It now hands back the real,
numbered lines around the nearest anchor, so the next attempt can be **copied**
rather than recalled.

**Decision**

```
old_string not found
├─ nothing in the file resembles its first line
│     └─ say exactly that + "call read_file"   <- no fake anchor
└─ an anchor exists
      └─ 7 lines either side, numbered, 160 chars each
         + "copy it character for character, not from memory"

(the same call a 3rd time is not run at all — C6)
```

**Proof** Restore the one-line hint → three tests fail, quoting the 29x sentence
back verbatim.

---

## Correction to an earlier note in this file

An earlier draft said every stall counter — *including the two guards added for
C1 and C7* — reset when the user types, and that C6 would fix all of them at
once. Half of that was wrong, and only the counters C6 actually moved were
moved:

| counter | scope | why |
|---|---|---|
| `unproductive` (no-op edits) | **session** | tracks the model repeating itself, which spans user turns. This was the bug. |
| remembered failed calls | **session** | same reason |
| `promise_nudges` (C7) | turn | a nudge *budget*, not a stall detector. A new user message is a new question and deserves a fresh one. |
| `_truncated_refusals` (C1) | turn | same — and it already stops the turn on the third, so it cannot run away |

Making the last two session-scoped would end a whole conversation after two
nudges ever, which is a worse failure than the one it would prevent.
