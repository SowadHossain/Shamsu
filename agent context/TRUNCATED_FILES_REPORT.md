# Why SHAMSU wrote incomplete files and called them complete

**Session:** `test-shamsu/test1` · `20260819-074923-10d7` · qwen3.5:9b-q4_K_M · 9 turns
**Reported:** files cut off mid-code; "read and complete" could not recover.

Four independent defects. Each is sufficient on its own; together they make
truncation invisible and unrecoverable.

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

## RC3 — the cut-off message blames the wrong thing

```
This answer was cut off. I ran out of room to answer in. The prompt was
16,965 tokens of a 32,768 window... /new starts a fresh conversation.
```

Arithmetic: `32,768 − 16,965 = 15,803` tokens of window were **free**. The
generation stopped at `num_predict = output_reserve(32768) = 8,192` — SHAMSU's
own cap, with 7,611 tokens of window still unused.

So the diagnosis is wrong and the advice is wrong: `/new` shortens the
conversation, which was never the constraint. The user is sent to fix something
that is not broken while the real limit stays put.

**Fix:** distinguish the two cases. Report the window only when the window is
actually the binding constraint; otherwise say the reply hit the per-reply cap
and raise `num_predict` toward the free space when a large write is in flight.

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
3. **RC4** — makes the files already on disk repairable.
4. **RC3** — accuracy of the message; no behaviour depends on it.

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
