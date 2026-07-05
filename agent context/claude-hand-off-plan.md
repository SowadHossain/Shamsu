# SHAMSU — Codex Hand-Off Plan (Round 2): approval prompt is skipped in interactive use

> **Round 1 is DONE** (web answers, PRD milestone build, tokenizer, council in
> CodeEditWorkflow, input/toolbar fixes) — see `agent context/codex-handoff.md`
> and `PROGRESS.md`. This document replaces that plan with the next bug to fix.
> Repo root: `F:\Work\PROJECTS\shamsu\Shamsu`. Keep the suite green
> (`.\.venv\Scripts\python.exe -m pytest tests/ -q`, currently 379) and
> `ruff check shamsu tests scripts` clean.

## Context — the bug (interactive-only, high severity)

Reproduction (real terminal, NOT piped):

```
shamsu> please build me the game based on the prd file in the folder
<PRD Build Plan panel prints correctly>
<Approval Required panel prints>
Do you want to proceed?
  1. Yes
  2. No
>
PRD build not approved. Nothing was built.      <-- printed WITHOUT the user typing anything
shamsu> 1                                        <-- the user's "1" becomes the NEXT prompt
intent=qa ... "I cannot access files..."         <-- routed to QA, hallucinates
```

**Symptom:** the approval prompt does not wait for input. `input()` returns
immediately, `ask_approval_menu` reads it as an empty answer → "No" → the action
is auto-denied. The user's real answer ("1") is then consumed as the next REPL
prompt and routed to Q&A.

**Root cause:** the interactive approval prompt runs **while a Rich `Live`
spinner is active**. The main loop wraps every natural-language request in
`with console.status(_thinking_status_for_input(user_input)) as thinking:`
(`shamsu/cli/repl.py:2867`). The approval prompt (`ask_approval_menu` →
`input("> ")` in `shamsu/safety/approval.py`) is called deep inside that
`console.status` context. Rich's `Live`/`status` display and Python's built-in
`input()` cannot share a real TTY — the spinner's terminal control makes
`input()` return an empty string instead of blocking. (Streaming answers work
because `_stream_answer` explicitly calls `thinking_status.stop()` on the first
token; approval prompts never stop the spinner, so they break.)

**Why earlier smoke tests missed it:** every prior manual test piped stdin
(`"...\n1" | shamsu`). Piped (non-TTY) `input()` reads the pipe and is not
affected by the Live. The bug only appears on an interactive terminal.

**Second, path-specific aggravator:** `_handle_prd_build_request`
(`repl.py:1866`) builds its approval on a throwaway console:
`ApprovalManager(ask_approval, session_logger).ask(...)` → `ask_approval` with
`console=None` → a fresh `Console()`. That fresh console cannot even see the main
loop's live spinner, so it could never stop it. Every other approval site already
uses `_make_approval_manager(workspace, session_logger, console)` with the shared
main console.

This bug class affects **all** interactive approvals reached under the spinner:
PRD build, web search + read-results, agent-loop `write_file`/`run_command`,
patch approvals, `/plan-prd`, `/generate-prd`, and the `ask_remember_choice` /
`ask_clarifying_question` prompts.

## Fix (recommended: minimal + robust)

Goal: **no Rich `Live` may be active while any interactive `input()` runs**, and
**every approval prompt must use the one shared REPL console** so it can stop that
Live.

1. **One shared console for output AND prompts.**
   - `_handle_prd_build_request` (`repl.py:1866`): replace
     `ApprovalManager(ask_approval, session_logger)` with
     `_make_approval_manager(workspace, session_logger, console)` (needs the
     `workspace` + `console` already in scope). This routes the prompt through the
     numbered menu on the **main** console (and gains permission memory for free).
   - Audit the other direct `ApprovalManager(ask_approval, ...)` / `ask_approval`
     call sites that run under a `console.status` (`/plan-prd`, `/generate-prd`,
     the Django writer path) and make them use the shared console the same way.

2. **Stop the active spinner before reading input.** Add a tiny guarded helper
   (e.g. in `shamsu/safety/approval.py`):
   ```python
   def _pause_console_live(console) -> None:
       live = getattr(console, "_live", None)
       if live is not None:
           try:
               live.stop()
           except Exception:
               pass
   ```
   Call it immediately before the `input(...)` in `ask_approval_menu`,
   `ask_remember_choice` (both in `approval.py`), and `ask_clarifying_question`
   (`shamsu/safety/clarify.py`). Stopping the live is safe: the outer
   `with console.status(...)` calls `Live.stop()` again on exit and it is
   idempotent. After a prompt the spinner simply stays off for the rest of that
   turn (correct — we are now interacting/printing, not "thinking").

   > `console._live` is a private Rich attribute but has been stable for years;
   > the `getattr` + `try/except` keeps it safe. If preferred, Codex may instead
   > adopt Rich's global console (`rich.get_console()`) everywhere and gate on its
   > live — but the helper above is the smaller change.

3. **Confirm the mechanism first.** Before/after the change, verify the hypothesis
   by temporarily commenting out the `with console.status(...)` wrapper at
   `repl.py:2867` and checking the approval now blocks correctly on a real
   terminal. This proves the Live is the cause and that the fix targets it.

### Optional larger cleanup (only if time allows)
Replace the single broad `console.status` wrapper around `_handle_request`
(`repl.py:2867`) with short-lived spinners scoped to the **non-interactive**
model calls only (routing in `_route_prompt`; the model call inside
`_run_code_edit`/`_run_bug_fix`/`_run_audit`/`_run_test_generation`/`_run_docs`),
and none around prompt-bearing/streaming handlers. This removes the whole class of
Live-vs-input conflicts structurally and lets the `thinking_status` threading
added for streaming be simplified. Higher risk; not required if steps 1–2 verify.

## Verification (MANDATORY — must be interactive, not piped)

Unit tests cannot reproduce the TTY `input()` behaviour, so a **real terminal**
check is required:
1. Open a fresh terminal, `cd` to a scratch folder containing a PRD, run `shamsu`.
2. `build the product from this prd` → at "Do you want to proceed?" the prompt
   **blocks**; typing `1`/`y`/Enter approves and the milestone build starts;
   typing `2`/`n` denies and the next prompt is NOT eaten.
3. `whats the weather in Dhaka today?` → both the search and the "read top
   results" approvals block and accept `1`.
4. During an agent build, a `write_file`/`run_command` approval blocks and accepts
   input.
5. Confirm `.\.venv\Scripts\python.exe -m pytest tests/ -q` still green (379+) and
   ruff clean.

Add a lightweight guard test where feasible (monkeypatch `builtins.input` to
return "1", call `ask_approval_menu` inside a `console.status(...)` on a
`force_terminal=True` console, assert it returns approved AND that
`getattr(console, "_live", None)` is stopped afterwards). Note in the test that it
guards the "spinner is paused before input" wiring, not the raw TTY behaviour.

## Update after implementing
- Append a short entry to `agent context/PROGRESS.md` ("Round 2 fix: interactive
  approval prompt skipped under the thinking spinner — fixed").
- Update `agent context/codex-handoff.md` (or add a Round-2 section) with the files
  changed and the interactive verification result.
