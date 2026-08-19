# Why SHAMSU wrote incomplete files and called them complete

**Session:** `test-shamsu/test1` · `20260819-074923-10d7` · qwen3.5:9b-q4_K_M · 9 turns
**Reported:** files cut off mid-code; "read and complete" could not recover.

Five independent defects. Each is sufficient on its own; together they make
truncation invisible and unrecoverable.

**The window was never full.** Prompts peaked at 21,472 tokens of 32,768 and the
reply was stopped by our own 8,192 `num_predict` cap — see RC3 and RC5.

---

## The damage

```
file          braces open/close   diff   max indent   state
game.js            60 / 39        +21        96       truncated
player.js          47 / 30        +17        68       truncated
bullet.js          24 / 17         +7        28       truncated
main.js           126 / 127        -1        24       suspect
asteroid.js        28 / 28          0        68       ok
level.js           16 / 16          0        12       ok
utils.js           19 / 19          0        20       ok
```

`bullet.js` and `player.js` both end on the literal line `} else {`.

Max indent 96 and 68 are the tell: the model degenerated into ever-deeper
nesting, which burned the output budget before the file ended.

---

## RC1 — a truncated generation still commits its tool calls

**The core defect.**

From turn 2 onward, every write in the log is immediately followed by the
cut-off notice:

```
Round 2  write_file -> ok    →  **This answer was cut off.**
Round 3  write_file -> ok    →  **This answer was cut off.**
Round 4  write_file -> ok    →  **This answer was cut off.**
Round 5  write_file -> ok    →  **This answer was cut off.**
Round 9  append_file -> ok   →  **This answer was cut off.**
```

The generation hit its cap mid-`write_file`, the partial tool call was salvaged
and **executed**, and the truncated content landed on disk as `ok`.

`_hit_the_length_limit()` exists and fires correctly — but it is only consulted
at `simple_chat.py:1311`, where it rewrites the *prose answer*. Nothing consults
it in `_run_tools`. So SHAMSU knew the generation was cut off and committed its
writes anyway.

**Fix:** when `done_reason == "length"`, do not execute mutating tool calls from
that generation. A write whose arguments were cut off is not a write. Either
discard it and tell the model to send the file in pieces, or write it and mark
the file suspect — but `-> ok` is wrong either way.

---

## RC2 — the verifier certifies files it never opens

`simple_chat.py:2766` (`_verify`):

```python
for relative in dict.fromkeys(written):
    ...
    checked.append(relative)          # <- appended BEFORE the filter
    if path.suffix != ".py":
        continue                      # <- everything non-Python skipped
    compile(...)                      # only Python is ever parsed
...
return {"ok": True, "message": f"Checked {', '.join(checked)}: no syntax errors."}
```

The filename is added to `checked` before the extension test, so a skipped file
is reported as checked. What SHAMSU told the model, verbatim from the log:

```
"Checked js/bullet.js: no syntax errors."     (+7 unclosed braces)
"Checked js/game.js: no syntax errors."       (+21 unclosed braces)
"Checked js/player.js: no syntax errors."     (+17 unclosed braces)
"Checked game-plan.md: no syntax errors."     (a markdown file)
```

**572** such claims in this one session. This is not a missing check — it is a
false statement handed to the model as a tool result, and it is why SHAMSU
reported the code complete. The one signal that should have caught RC1 instead
confirmed the opposite.

**Fix:** only list files actually parsed. For everything else say so
(`"skipped (no checker for .js)"`). Then add real checkers — `node --check` for
`.js` when node is present, and a brace/paren balance test as the zero-dependency
fallback, which would have caught all three files here.

---

## RC3 — the window was never full, and the message says it was

**The 32k window was never reached.** Measured across all 77 logged prompts:

```
largest prompt        21,472 tokens   of a 32,768 window   (66%)
typical               20,200-20,900
reply cap             8,192           num_predict = output_reserve(32768)
21,472 + 8,192      = 29,664          still 3,104 under the window
```

At the largest prompt, **11,296 tokens of window were free** and the reply was
stopped at 8,192 by SHAMSU's own per-reply cap. The window was not the
constraint at any point in this session.

What the model was told instead:

```
This answer was cut off. I ran out of room to answer in. The prompt was
16,965 tokens of a 32,768 window. The window holds the conversation and the
reply together, so a long conversation leaves less space to speak in.

/new starts a fresh conversation, or ask for a smaller piece of this one.
```

Three things wrong with it:

1. **The diagnosis.** The reply hit `num_predict`, not the window.
2. **The advice.** `/new` shortens the conversation, which was never the limit.
3. **The number is frozen.** `16,965` appears **54 times, identical**, across a
   session whose prompt ranged 20,200–21,472. It was generated **once** as a
   real answer and then replayed 53 times inside later prompts — the harness's
   own error message became conversation, was fed back every turn, and taught
   the model that "I ran out of room" is how a turn ends.

**Fix:** report the window only when the window is actually binding; otherwise
say the reply hit the per-reply cap. Raise `num_predict` toward the free space
when a write is in flight. And do not append this message to history — it is a
harness notice, not something the model said.

---

## RC5 — the verbatim tail is the prompt

Why a small project produces a 21,000-token prompt, and the direct answer to
"did we really use the window?".

`KEEP_VERBATIM_MESSAGES = 20` keeps the last twenty messages at full size;
elision may only touch what is older. Measured over all 77 prompts:

```
                  messages     chars    avg chars/msg
older (elided)       4,655   2,352,729            505
last-20 verbatim     1,475   2,485,838          1,685
```

**Elision works.** Old messages are shrunk to a third the size. The problem is
that the protected tail is **24% of the messages and 51% of the content** — and
in the worst prompt, 87%:

```
msgs   older(elidable)   last-20 verbatim   total
  31            11,056             74,835   85,891 chars
```

In a file-writing session the last twenty messages are whole-file payloads. One
single assistant message in that prompt was **25,473 characters** — an entire
file, kept verbatim, correctly, by design.

The constant came from a 130-message measurement where "twenty is the knee".
That measurement assumed conversational messages. It does not hold when one
message can be 25,000 characters, and this is the case simple mode is for.

**Fix:** cap the verbatim tail by **tokens, not message count** — keep the most
recent messages until a token budget is spent, so twenty small turns stay whole
and three whole-file writes do not. This is the single change that would most
reduce prompt size here, and unlike raising the window it does not scale the
problem up with the files.

---

## RC4 — recovery is impossible: 24 patch attempts, 0 successes

```
patch_file -> FAILED : 24
patch_file -> ok     :  0
```

Every failure is `old_string not found`. Cause is visible in the arguments — the
model emits a **literal** `\n` (two characters) where a newline belongs, mixed
with real newlines in the same string:

```json
"old_string": "// Start the application when DOM is ready\\ndocument.addEventListener(...);\n * Create or get existing game instance */\n..."
                                                        ^^^^ literal backslash-n
```

That text does not exist in any file, so it can never match. The harness's error
is accurate and unhelpful — *"Nearest similar line is line 425"* — because it
compares the mangled string as given.

Reading itself is **not** broken: `read_file(start_line=410, end_line=435)`
returned correctly, and `_budgeted` caps results at 8,000 tokens with explicit
guidance to fetch the rest by range. Both truncated files are ~6,000 tokens and
would fit whole. What fails is turning a read into a patch that matches.

**Fix:** normalise a literal `\n` in `old_string` before matching when the raw
form does not match and the unescaped form does — the same salvage principle
already applied to malformed tool calls. And on a failed match, return the real
lines around the nearest hit so the next attempt is computed from actual text
rather than memory. `read_and_patch` already carries that rule; plain
`patch_file` does not.

---

## Order to fix

1. **RC2** — smallest change, largest effect. It converts a silent failure into
   a visible one, and it is what lets any of the others be observed.
2. **RC1** — stops the corruption at its source.
3. **RC5** — halves the prompt, which is what gives the model room to finish a
   file in the first place.
4. **RC4** — makes the files already on disk repairable.
5. **RC3** — message accuracy, plus not replaying the notice into history.

## Reproduce

```powershell
cd F:\Work\PROJECTS\shamsu\test-shamsu\test1
# truncation
foreach ($f in Get-ChildItem js\*.js) {
  $t = Get-Content $f -Raw
  "{0,-14} {1,3} open / {2,3} close" -f $f.Name,
    ([regex]::Matches($t,'\{')).Count, ([regex]::Matches($t,'\}')).Count
}
# the false verdicts
Select-String -Path .shamsu\chat-logs\*.md -Pattern 'no syntax errors' | Measure-Object
```
