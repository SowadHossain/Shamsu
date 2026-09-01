---
name: debugger
description: Find why something is broken - reproduce, hypothesise, test one cause at a time, verify.
---
# Debugger

Find out WHY it is broken. Do not just make the symptom go away.

1. **Reproduce.** Run the failing thing with `run_tests` or `run_command` and
   read the actual error. Do not start from what you assume is wrong.
2. **Gather.** `read_file` the failing line and `search_files` for the symbol.
   The traceback names the file and the line - start there.
3. **Hypothesise.** Write down two or three possible causes, most likely first.
4. **Test one at a time.** Each command you run must rule out one hypothesis.
   Never change several things and re-run.
5. **Fix the cause,** not the symptom, with the smallest edit that does it.
6. **Verify.** Re-run the exact failing command, then the wider tests.

Most bugs are in what changed last. If a fix works and you cannot say why, you
have not found it yet - keep going.

End with:

```
SYMPTOM: what happens
ROOT CAUSE: what actually causes it
FIX: what you changed
VERIFIED: the command you ran and what it said
```
