# Codex Handoff: Claude Plan Fixes

Date: 2026-07-04

This report summarizes the implementation work done against
`agent context/claude-hand-off-plan.md`.

## Summary

Implemented the requested bug fixes and follow-ups from the Claude hand-off plan:

- Area A: Web answers now produce real synthesized answers instead of bare links.
- Area B: PRD product builds now run milestone-by-milestone when milestones are detected.
- Area C1: Token budgeting now uses a vendored Qwen3 tokenizer with a safe char/4 fallback.
- Area C2: `CodeEditWorkflow` now uses council mode for security-sensitive edit targets.
- Area D1: Enter accepts an active prompt-toolkit completion before submitting.
- Area D2: Bottom toolbar no longer reads workspace autonomy config on every redraw.

## Area A: Web Answers

Files changed:

- `shamsu/tools/web.py`
- `shamsu/cli/repl.py`
- `tests/test_web_browser_tools.py`

Fixes:

- Added DuckDuckGo redirect decoding for result URLs.
- Normalizes protocol-relative URLs such as `//duckduckgo.com/l/?uddg=...`.
- Extracts the real destination from `uddg=<encoded-url>`.
- Added `WebTool.fetch(..., require_approval=True)` so the REPL can reuse one combined approval for top-result fetches.
- `_run_web_assist()` now asks once to read the top results instead of prompting once per URL.
- `_print_web_answer()` now falls back to LLM synthesis from search titles/snippets when page bodies cannot be fetched or extracted.
- The fallback still includes a `Sources:` list.

Tests added/updated:

- DDG redirect URL decoding.
- Fetch without per-URL approval.
- Snippet-based answer fallback when `fetches` is empty.
- Single follow-up approval for top-result fetches.

## Area B: PRD Build Quality

Files changed:

- `shamsu/cli/repl.py`
- `tests/test_prd_build_flow.py`

Fixes:

- Added `PRD_BUILD_FRAMING` to require complete runnable files, no TODO-only stubs, tool-based writes, and verification where possible.
- `_handle_prd_build_request()` now detects PRD milestones using existing `_extract_prd_milestones()`.
- If milestones are found:
  - Creates a `MilestoneTask` with one `TaskStep` per milestone.
  - Persists task state under `.shamsu/tasks/<task-id>.json`.
  - Runs `_run_agent_chat()` once per milestone with `force_long_running=True`.
  - Marks each milestone step running/done.
  - Calls `advance_phase()` between milestones.
  - Shows progress lines like `Milestone 1/2: ...`.
- If no milestones are found:
  - Falls back to the existing single-pass long-running build behavior.

Tests added/updated:

- Milestone PRD runs one agent pass per milestone.
- Task state is persisted and phases advance.
- No-milestone PRD still uses one build pass and does not create a task.

## Area C1: Real Tokenizer

Files changed:

- `pyproject.toml`
- `shamsu/context/budget.py`
- `shamsu/context/assets/qwen3-tokenizer.json`
- `tests/test_context_budget.py`

Fixes:

- Added `tokenizers>=0.21` dependency.
- Vendored Qwen3 tokenizer asset at `shamsu/context/assets/qwen3-tokenizer.json`.
- `count_tokens()` now lazily loads the vendored tokenizer and caches it.
- If `tokenizers` is not installed, the asset is missing, or the asset fails to load, it falls back to the old char/4 estimate.
- No caller changes were needed.

Tests added:

- Real tokenizer path returns expected count for a code sample.
- Missing asset path falls back to char/4.
- Asset path is pinned under `shamsu/context/assets/`.

## Area C2: Council In CodeEditWorkflow

Files changed:

- `shamsu/agents/code_edit_workflow.py`
- `tests/test_code_edit_workflow.py`

Fixes:

- `CodeEditWorkflow` now mirrors `BugFixWorkflow` council behavior.
- Collects target paths from search results.
- Calls `should_convene_council(target_paths=...)`.
- Uses `run_council(..., specialist="coder")` for sensitive paths.
- Keeps direct `run_specialist("coder", pack)` for ordinary files.

Tests added:

- `settings.py` triggers coder -> reviewer -> coder council flow.
- Ordinary `app.py` edit skips council.

## Area D1: Completion Enter Behavior

Files changed:

- `shamsu/cli/repl.py`

Fix:

- `_make_input_key_bindings()` now checks `event.current_buffer.complete_state`.
- If a completion menu is open and has a highlighted completion, Enter applies the completion.
- Otherwise Enter submits as before.
- Alt+Enter still inserts a newline.

Manual verification note:

- This is primarily an interactive prompt-toolkit behavior and is hard to unit-test reliably without a terminal UI harness.

## Area D2: Bottom Toolbar Disk I/O

Files changed:

- `shamsu/cli/repl.py`

Fix:

- Added `CachedBottomToolbar`.
- `_make_prompt_session()` now accepts a toolbar callable and uses the cached toolbar by default.
- `main()` creates one `CachedBottomToolbar` per REPL session.
- After `/autonomy on|off`, the toolbar cache is refreshed.
- This avoids calling `is_long_running_enabled(workspace)` on every toolbar redraw.

## Verification

Focused tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_web_browser_tools.py tests\test_cli_routing.py tests\test_code_edit_workflow.py tests\test_prd_build_flow.py tests\test_context_budget.py -q
```

Result:

```text
48 passed
```

Full suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
```

Result:

```text
379 passed, 2 warnings
```

Lint:

```powershell
.\.venv\Scripts\python.exe -m ruff check shamsu tests scripts
```

Result:

```text
All checks passed
```

Warnings:

- Existing pytest collection warnings for the `TestRunResult` dataclass name in:
  - `tests/test_error_feedback_loop_long_running.py`
  - `tests/test_milestone_task_state.py`

These are not failures.

## Important Git Notes

Do not commit local runtime/session artifacts unless intentionally changing repo fixtures.

Currently expected local noise may include:

- `.shamsu/runtime.json`
- `.shamsu/sessions/`
- deleted `.shamsu/.gitkeep` placeholders from local runtime cleanup

The new hand-off report itself should be committed:

- `agent context/codex-handoff.md`

The Claude hand-off source doc is currently untracked and should likely be committed too:

- `agent context/claude-hand-off-plan.md`

## Current State For Next Agent

The Claude hand-off implementation is complete and verified locally.

Recommended next step:

1. Review `git status --short`.
2. Stage code/docs/tests deliberately, excluding `.shamsu/` runtime artifacts.
3. Commit the hand-off fixes.
4. Push the current branch if the team wants this work in the open PR.

## Round 2: Interactive Approval Prompt Fix

Date: 2026-07-04

Implemented the follow-up plan currently recorded in
`agent context/claude-hand-off-plan.md`.

Bug fixed:

- Interactive approvals could auto-deny without waiting for input when a Rich
  `console.status(...)` spinner was active.
- The user's typed approval, such as `1`, then became the next REPL prompt.

Root cause:

- Approval and clarify prompts call Python `input()`.
- Several prompts were reached inside the broad REPL thinking spinner.
- Current Rich versions store the active `Live` object on the `Status` object,
  not reliably on `console._live`, so the prompt layer could not stop it.
- PRD build and a few related approval paths also used throwaway consoles,
  which could not see the main REPL spinner.

Files changed:

- `shamsu/safety/approval.py`
- `shamsu/safety/clarify.py`
- `shamsu/cli/repl.py`
- `tests/test_permission_manager.py`
- `agent context/PROGRESS.md`
- `agent context/codex-handoff.md`

Implementation details:

- Added `_pause_console_live(console)` in `approval.py`.
- `ask_approval_menu()` and `ask_remember_choice()` now pause active Rich
  live/status output before calling `input()`.
- `ask_clarifying_question()` now uses the same pause guard.
- Added `_install_console_status_tracker(console)` in `repl.py`; the main REPL
  console records active status objects in `_shamsu_active_statuses`.
- `_pause_console_live()` stops both the legacy `console._live` target and any
  tracked SHAMSU status objects.
- `_handle_prd_build_request()` now uses the shared-console approval manager.
- `plan-prd` approval now uses the shared-console approval manager.
- `models pull/repair` model download approval now binds `ask_approval()` to
  the shared REPL console.
- `generate-prd` now passes a console-bound approval function into
  `FullDjangoPipeline`, so internally-created Django writers also prompt on the
  shared console.
- Preserved test injection behavior by pinning the original approval function
  as `DEFAULT_ASK_APPROVAL` and treating monkeypatched/injected approval
  functions as non-interactive test doubles.

Tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_permission_manager.py tests\test_prd_build_flow.py tests\test_cli_routing.py -q
```

Result:

```text
52 passed
```

Full suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
```

Result:

```text
392 passed, 2 warnings
```

Lint:

```powershell
.\.venv\Scripts\python.exe -m ruff check shamsu tests scripts
```

Result:

```text
All checks passed
```

Interactive verification note:

- The code fix directly addresses the mechanism by stopping the active Rich
  status before the blocking input call.
- A real-terminal verification is still recommended before release:
  approve/deny a PRD build, approve weather web search/read-results, and
  approve an agent write-file or run-command prompt.
