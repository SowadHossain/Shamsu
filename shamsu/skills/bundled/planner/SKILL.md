---
name: planner
description: Turn a vague request into an ordered list of small, checkable steps before any code is written.
---
# Planner

Plan first when the job has several parts. A plan is an ordered list of steps
small enough to finish one at a time.

1. **Say what done looks like** as claims someone could check - "the page
   loads and shows the list", not "improve the UI".
2. **List the files** each step touches. A step that names no file is not a
   step yet; split it until it does.
3. **Order by dependency.** What must exist before the next thing can work?
4. **Keep each step to one file** where you can. One file per step is what
   actually finishes.
5. `contract_create` with those claims, then build.

Rules:

- No step larger than one file, or one function in a large file.
- Write the plan down with `write_file` to `PLAN.md` if it has more than five
  steps, so it survives the conversation.
- Do not plan past what you know. Three solid steps beat ten guesses.
- Never mark a step done because you wrote the code. Done is checked.
