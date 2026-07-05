# SHAMSU Progress Tracker

This is the living implementation ledger. Every agent should update this file
after completing a feature slice, changing priorities, or discovering a
blocker.

## Current State

- Status: Milestone 4 implemented locally on top of `develop`; Day 1 scaffold complete; Day 2 indexing/PRD extraction complete; deterministic Django template and ProjectSpec slice complete; install/run scripts, safer workspace CLI, internal command runner, patch validation/preview, patch apply/rollback, post-patch re-indexing, read-only git tooling, code edit workflow, real indexed QA fallback, live QA integration, audit workflow, documentation proposal/apply workflow, bug fix workflow, test generation workflow, CLI workflow routing, native local Ollama runtime bootstrap, TXT/PDF PRD input, PRD extractor v2, project plan preview/approval, generation resume state, deterministic Django backend generation, approval-backed project writer, backend consistency checker, workspace-local session logging/resume, M5 generated-project setup/test/fix loop, M6 full pipeline fixtures/benchmark/docs, Dev A frontend consistency checking, dirty-worktree edit warnings, Windows-safe Ollama model pull/repair decoding, single-command install with user-local launchers, managed user PATH install/uninstall, general no-index local chat, guided in-chat runtime repair prompt, uninstall scripts, autonomous web lookup, Playwright browser debugging hooks, smarter local/web/browser routing, agent orchestrator memory/context, workspace file tools, `@file` context, central approval logging, cleaner web extraction, `qwen3:8b` main model routing, stateful Ollama ReAct loop, markdown file-write fallback, slash-command hardening, and SHAMSU v2.3 phase-one registry/template/DoD gates complete.
- Reliability/autonomy roadmap (Milestones A-G, `agent context` plan): **A-G complete — full roadmap done.**
  - **A (install/reinstall reliability):** read-only `shamsu.runtime.doctor` module + `/doctor` REPL command + `scripts/doctor.ps1`/`.sh`, checking editable-install health, Ollama status, stray nested `.shamsu` workspaces, ancestor-workspace conflicts, and PATH manifest health. Install scripts no longer abort on a flaky Playwright/winget step (warn and continue) and skip re-downloading Chromium via a marker file. `resolve_workspace()` now warns (without redirecting) when a parent directory already has a SHAMSU workspace. `uninstall.ps1`/`.sh` recursively clean up stray nested `.shamsu` folders. README's troubleshooting section leads with `doctor` instead of wipe-and-reinstall.
  - **B (lazy model downloads):** `shamsu.runtime.ollama.ensure_model_available()`; `LLMManager` lazily pulls a missing model the first time a specialist/router actually needs it (locked per-model to avoid double pulls), with a `ModelPullProgress` hook rendering the same Rich progress bar `/models pull` uses. Install defaults to skipping upfront model downloads; `-PrefetchModels`/`--prefetch-models` opts back into eager pull-everything.
  - **C (tiered permissions with memory):** new `shamsu/safety/permission_store.py` (`PermissionMemory`, session + workspace-persisted `.shamsu/permissions.json`). `ApprovalManager` can auto-approve remembered `file_write`/`file_edit` decisions (`is_auto_approvable_action` in `safety/commands.py`) and offers a one-time "remember?" follow-up (`ask_remember_choice`) after a real approval — `run_command`/`file_delete`/`web_search`/`mcp_tool` are never auto-approvable regardless of memory. Migrated `agent_tools.py`, the Django writer, and `plan-prd`'s approval off ad-hoc `approval_func` calls onto the central manager. New `/permissions list|clear`.
  - **D (invisible incremental auto-indexing):** `FileWalker.index()` now skips rehash/rebuild for files whose size+mtime haven't changed, and skips symbol/snippet rebuild (but still updates stored metadata) when the hash is unchanged; logs an `index.updated` event when given a `session_logger`. New shared `shamsu.indexer.walker.ensure_index()` (best-effort, swallows `OSError`/`sqlite3.Error`) is called transparently by `_build_search_agent`/`_build_workspace_qa_workflow`/`_handle_status`/`_handle_search`/`_handle_symbols` and by `AgentOrchestrator` before it reports "Index exists" — the `/index` command and manual "no index found" messaging are no longer required for normal use, but remain as an explicit option and a defensive fallback if indexing ever fails.
  - **E (context engineering — ranking only, tokenizer swap deliberately skipped):** `SearchAgent.search()` now over-fetches from FTS5 and re-ranks with additive boosts: file-path term match, symbol-name match (bulk query against the `symbols` table), a lazily-built `rank_bm25` layer over the 500 most-recently-touched snippets (built on first `.search()` call, never at startup), and an optional `boost_paths` hint wired from `BugFixWorkflow`'s parsed traceback locations so files that actually appear in the bug report/traceback get promoted. Deliberately did **not** swap `context/budget.py`'s `count_tokens()` for a real HF tokenizer: that needs a real vocab/tokenizer.json asset this environment can't fetch, and the file's own docstring already argues against a tokenizer in the hot path — left as a documented follow-up, not silently dropped.
  - **F (generalized multi-milestone task tracker):** new `shamsu/tasks/` package. `MilestoneTask`/`TaskStep` (the latter reused from the previously-unused generic type in `types.py`, now with an added `phase` field) track phase, blocked steps with a reason (distinct from a retryable `FAILED` step), files created/edited, commands executed, test results, and a `next_action` hint, persisted at `.shamsu/tasks/<id>.json`. `advance_phase()` gates moving to the next phase on all of the *current* phase's steps being done/skipped and (if given) its tests passing. `generation_state_to_milestone_task()` is a one-way, read-only projection of the Django pipeline's existing `GenerationState` for unified visibility — the Django pipeline's own state file remains authoritative. New `/tasks list|show <id>`.
  - **G (long-running autonomous executor + selective council mode, opt-in):** new `shamsu/llm/council.py` — sequential draft (normal specialist) -> critique (`reviewer` role) -> reconcile (only if the critique actually flags an issue), gated by `should_convene_council()` (low routing confidence, destructive action kind, or a security-sensitive target path) so the common case skips the extra calls; wired into `BugFixWorkflow` (CodeEditWorkflow wiring left as a follow-up). New `shamsu/safety/autonomy.py` opt-in toggle (`.shamsu/autonomy.json`, default **off**) exposed via `/autonomy status|on|off`. When enabled: `AgentChatLoop` raises its round ceiling from 5 to `LONG_RUNNING_MAX_TOOL_ROUNDS=40` and adds a repetition guard that asks a genuine clarifying question (new `shamsu/safety/clarify.py`) instead of silently looping or giving up when the same tool call repeats; `ErrorFeedbackLoop` raises `max_iterations` from 3 to `LONG_RUNNING_MAX_ITERATIONS=15` but stops early via stall detection (failure count not improving) rather than blindly burning the higher ceiling; `FullDjangoPipeline` attempts one bounded bugfix-and-retry pass on a Django setup failure (using the already-existing `DjangoSetupResult.bugfix_context` hook that nothing previously called) before giving up. All three default to today's exact capped behavior when the toggle is off — verified with dedicated tests pinning down both the default (unchanged) and long-running (new) behavior side by side.
- Tests: `420 passed`
- Lint: `python -m ruff check shamsu tests scripts` passes.
- Last verified: 2026-07-05
- **SHAMSU v2.2 foundation (2026-07-05):** added the two-anchor 8GB model cookbook (`qwen3:8b` for thinking/text roles and `qwen2.5-coder:7b-instruct` for code/test/bug-fix roles), `SHAMSU_SINGLE_MODEL_MODE`, off-cookbook pull refusal, doctor cookbook check, archetype metadata on `ProjectSpec`, typed-hole manifest contract types, deterministic PRD archetype classification, and an archetype template registry that wraps the existing Django generator for `web_crud`/`rest_api`.
- **SHAMSU v2.3 phase-one registry/template/DoD gates (2026-07-05):** added a disk-backed category registry (`multiplayer-game`, `portfolio-site`, `multi-tenant-admin`, `ecommerce`, `general-web`), deterministic category scoring, forced stack policies from `v2.3-techstack-recomendation.md`, registry YAML loader, `ProjectSpec` category/master-prompt metadata, a Vite + React + TypeScript + react-three-fiber + Colyseus relay + Rapier multiplayer game template, source-level Definition-of-Done checks, scaffold safety through `Sandbox`, and category-aware `generate-prd` routing for multiplayer-game PRDs. Baseline multiplayer scaffold now fails closed on required DoD failures and reports the DoD table in CLI/summary output.
- **Approval input hotfix (2026-07-05):** fixed `models pull`/other prompt-bearing commands crashing or auto-cancelling when approval input hit EOF or a blank TTY read under Rich/prompt-toolkit on Windows. Approval menus now retry accidental empty TTY reads, cancel cleanly on EOF, and prompt-bearing commands no longer run under the broad thinking spinner.
- **Windows approval fallback follow-up (2026-07-05):** when prompt-toolkit closes stdin on Windows even though the terminal is still interactive, approval prompts now fall back to a direct console key read via `msvcrt.getwch()` for `1`/`2`/`y`/`n`, so `/models pull` can actually receive the user's approval instead of cancelling and treating `1` as the next chat prompt.
- **Vague-imperative routing fix (2026-07-05):** terse action prompts like "do the task", "do it", "continue", "go" were routed to the tool-less QA specialist, which hallucinated "I cannot access files". New `_looks_like_vague_action_request()`: when exactly one PRD is present such a prompt now triggers the approval-gated PRD build; otherwise it routes to the tool-having agent loop (never the tool-less QA path). Guarded by a word-count cap so real questions are unaffected. Regression tests in `tests/test_cli_routing.py`.
- **`/models pull` no longer auto-cancels + general approval read hardened (2026-07-05):** `/models pull` (and `/models repair`) sat on a `run_command` approval menu that silently cancelled without a keypress, then the user's "1" leaked to the next REPL prompt — the fragile-`input()` disease again. Root cause: SHAMSU's PowerShell launcher runs Python so built-in `input()` sees a non-interactive stdin (prompt_toolkit drives the main prompt directly, which is why *it* works), so `input()` returned empty → treated as "No". Two fixes: (1) typing `/models pull`/`/models repair` is itself consent, so the redundant approval was removed — it now downloads directly (removed `_approve_model_download` + the unused `approval_func` param on `_handle_models`); (2) `safety/approval._read_approval_answer` now reads via prompt_toolkit when `console.is_terminal` (falling back to the msvcrt single-key reader, then `input()` for piped/test contexts), so every *other* approval menu (file writes in non-autonomy mode, etc.) stops auto-cancelling too. Tests updated in `tests/test_native_runtime_cli.py` (pull/repair download without an `input()` prompt — the monkeypatched `input()` throws if called); `tests/test_permission_manager.py` still green (non-terminal console uses the `input()` fallback path). Note: two-anchor model cookbook now needs `qwen2.5-coder:7b-instruct` for coder/test/bugfix roles.
- **Verb-based action routing so imperatives reach the tools (2026-07-05):** the phrase-list `_looks_like_vague_action_request()` only caught an exact set ("do it", "continue", …), so new phrasings like "okay you should do the thing" or "fix the code and check the requirements and fix it" fell through to the tool-less QA specialist — which *described* the fixes in prose but never applied them (`intent=qa confidence=0.35`). New `_looks_like_action_request()` is verb-based (`_ACTION_VERBS`: fix/implement/create/refactor/review/…) and routes any imperative — outside obvious question phrasing (leading what/why/how/… or a trailing `?`) — to the tool-having agent loop, while genuine questions still go to QA. The narrow phrase detector is kept only for the PRD-build trigger. The qa/explain branch now also passes `auto_approve=is_long_running_enabled(workspace)`, so with `/autonomy on` the edits run hands-free and otherwise each write is approved. Verified live: "okay you should do the thing" now `→ Reading` / `→ Writing` and actually fixed a bug (`a - b` → `a + b`) instead of printing an essay. Tests in `tests/test_cli_routing.py`.
- **PRD build no longer blocked by broken approval (2026-07-05):** the interactive inline `input()` approval could silently auto-deny on a real Windows terminal (the "PRD build not approved" with no keypress), so builds never started and stray "1"/"2" leaked to the tool-less QA path ("I cannot access files"). The PRD build now treats the build request itself as consent: it shows the plan and starts building directly (no inline approval), with `_run_agent_chat(auto_approve=True)` so the agent's file writes/commands during that consented build proceed without further prompts. Verified live end-to-end: a PRD build writes real files (`index.html`) with no approval prompt. Also softened `NO_LIVE_TOOLS_NOTICE` so the QA path stops claiming "I cannot access files" when workspace context is present. Tests updated in `tests/test_prd_build_flow.py`.
- **Free Ollama footprint when the last session exits (2026-07-05):** closing SHAMSU used to leave the router model (`qwen3:8b`, pinned by `keep_alive="-1"` in `llm/manager.py`) resident in RAM for as long as the Ollama server lived — ~6 GB held with no active session. New `shamsu/runtime/session_registry.py` tracks live REPL sessions via PID files under `~/.shamsu/runtime/sessions/` (stale entries pruned with a `psutil` liveness check) and records an ownership marker only when SHAMSU itself started `ollama serve`. `main()` registers the session and an `atexit` handler; on the *last* session's exit `shutdown_if_last_session()` (in `runtime/ollama.py`) stops the server if SHAMSU owns it, otherwise unloads only SHAMSU's own loaded models via `/api/ps` + `keep_alive=0` (so a shared/Windows-tray-app Ollama is never killed but its RAM is still freed). Multiple concurrent sessions are safe — only the final one cleans up. Verified live: after `exit`, `/api/ps` went from `qwen3:8b` (6.18 GB) to empty, session PID file removed, server left running. Tests in `tests/test_session_lifecycle.py` (8). Correction to a prior note: only the *specialist* models unload after 10 min; the router was pinned until the server stopped — this is what the change fixes.
- **PRD build output quality — orphaned-code / spinning-box fix (2026-07-05):** a live 7-milestone build produced only a spinning cube. Root cause: each milestone ran a *fresh* `AgentChatLoop` (no memory of prior milestones) whose prompt only had the PRD + milestone name, so it regenerated `script.js` from scratch each time ("File already exists → overwrite") and left Milestone 1's inline rotating-cube `<script>` inside `index.html` — the page ran the demo while every later milestone's game logic sat orphaned in an unloaded file. Fix (prompt-level, no structural change): `PRD_BUILD_FRAMING` and `_build_prd_milestone_request` now require the agent to `read_file` existing files and EXTEND them (never regenerate wholesale), and mandate that `index.html` load `script.js` via `<script src>` with no inline game logic left behind. Verified live: Milestone 2 now `→ Reading script.js` before writing and reports "All previous features remain intact"; on disk `index.html` holds only two script tags and `script.js` contains the cumulative cube + arrow-key controls. (Caveat: build correctness still depends on `qwen3:8b`, and generated pages currently load Three.js from a CDN — an offline follow-up.)
- **Duplicate `shamsu` command fix (2026-07-05):** removed the `[project.scripts] shamsu` entry point from `pyproject.toml`. It made `pip install -e` create a second `.venv/Scripts/shamsu.exe` that shadowed the managed `~/.shamsu/bin` launcher whenever the venv was on PATH (the "Plain 'shamsu' resolves to a different command" warning), and that exe skipped `PYTHONUTF8`. Now there is exactly one `shamsu` (the launcher); run otherwise via `python -m shamsu.cli.repl`. Also added `_force_utf8_stdio()` in `main()` so direct `python -m` runs never crash on non-ASCII output. Reinstalling removes the stale exe (pip drops it on the uninstall step).
- Current next focus: the full A-G reliability/autonomy roadmap and Claude hand-off fixes are done. Remaining open follow-ups: try `/autonomy on` in real use before considering it as a possible future default, then continue Milestone 6 Dev C status/log/progress, release docs, and final safety audit from the original MVP plan.
- **Claude hand-off fixes (2026-07-04):** implemented the follow-up plan from `agent context/claude-hand-off-plan.md`.
  - **Web answers:** DuckDuckGo redirect links are decoded to real URLs, web fetch can reuse a single combined top-results approval, and web answers synthesize from search titles/snippets when page extraction fails instead of printing only bare links.
  - **PRD build quality:** natural PRD product builds now run milestone-by-milestone when `Milestone N:`/`Phase N:`/`Step N:` lines are detected, persist a `MilestoneTask` under `.shamsu/tasks/`, gate phase advancement with `advance_phase()`, and keep the old single-pass long-running build for PRDs without milestones.
  - **Tokenizer:** `context/budget.py` now lazily uses a vendored Qwen3 `tokenizer.json` via the lightweight `tokenizers` package, with the old char/4 estimate as the automatic offline/minimal-install fallback.
  - **Council mode:** `CodeEditWorkflow` now mirrors `BugFixWorkflow` and convenes the sequential council for security-sensitive edit targets such as `settings.py`.
  - **REPL polish:** Enter accepts an open prompt-toolkit completion before submitting, and the bottom toolbar caches autonomy/model state instead of reading workspace config on every redraw.
- **Claude hand-off Round 2 fix (2026-07-04):** fixed the high-severity interactive approval bug where `input()` could return an empty answer while a Rich thinking spinner was active.
  - Added a Rich status tracker on the main REPL console and pause logic before `ask_approval_menu`, `ask_remember_choice`, and `ask_clarifying_question` call `input()`.
  - Routed PRD build, `plan-prd`, model download, and full PRD generation approvals through the shared REPL console instead of throwaway consoles.
  - Added a focused regression test proving an active tracked status is stopped before the approval menu reaches `input()`.
- **Post-rollout fixes (2026-07-04):** two real bugs found via live manual testing after the A-G rollout, both fixed and covered by new tests in `tests/test_cli_routing.py`:
  - `install.ps1`'s PATH self-check compared the resolved `shamsu` command only against the `.cmd` launcher, so on PowerShell (which resolves bare `shamsu` to `shamsu.ps1`, not `.cmd`) it *always* printed a false-positive "resolves to a different command" warning even on a correct fresh install. Fixed to accept either launcher file as correct.
  - A typo in a web-needing prompt (e.g. "weither" instead of "weather") missed `_looks_like_web_needed_prompt`'s exact-substring check, silently falling through to the tool-less QA/general-chat path — where the model, given zero framing about its own capabilities, fabricated a fake "let me search the web... [Note: I can't access real-time data]" narration instead of either using the real web tool or admitting it couldn't. Fixed two ways: (1) added typo-tolerant fuzzy matching (`difflib.get_close_matches`) for the core weather keywords so common misspellings still route to the real, permission-gated `WebTool`; (2) added a `NO_LIVE_TOOLS_NOTICE` guardrail string to both `QAWorkflow.build_prompt` and `_run_general_chat`'s context pack so that *even if routing misses*, the model is explicitly told it has no live tool access and must say so honestly rather than narrate a fake action — verified live that a denied/unavailable web search now yields an honest "I don't have real-time access" answer instead of a hallucinated search transcript.
- **PRD-to-product UX + CLI/permission overhaul (2026-07-04):** driven by live testing of the PRD build flow; all covered by new tests and verified live.
  - **PRD detection broadened:** new `is_prd_filename()` in `shamsu/prd/input.py` recognizes spelled-out names (e.g. `Product Requirements Document.pdf`) and the `prd` acronym, replacing the old literal-`"prd"`-substring checks in `WorkspaceTool.find_prds`, `_find_workspace_prd_files`, and orchestrator `_asks_prd_files`.
  - **Natural-language "build me the product from this PRD":** new conservatively-gated `_looks_like_prd_build_request` + `_handle_prd_build_request` in `repl.py` — auto-resolves the PRD (explicit path / `@`-mention / single workspace PRD), shows a general (non-Django) plan preview with detected milestones, gates on approval, then runs the general `AgentChatLoop` with `force_long_running=True`. Fixes the old dead-end where such prompts hallucinated.
  - **`@` mentions attach PDFs + spaced filenames:** `MentionResolver._context_for_path` extracts PDF text via `parse_prd_file`; `mention_suggestions` quotes paths containing spaces (`@"..."`).
  - **Hidden CMD window fixed:** `_ensure_model` ran `ollama list` before every LLM call via a subprocess missing Windows `CREATE_NO_WINDOW`, flashing a console on nearly every prompt. Added `_no_window_flags()` to `runtime/ollama.py` (`_run_ollama_command`, `pull_model_streaming`) and `CREATE_NO_WINDOW` to the `executor.py` shell subprocess.
  - **Claude-Code-style permission menu:** `ask_approval_menu` gives a numbered menu (`1. Yes` / `2. Yes + don't ask again` / `3. No`), folding the old separate remember-prompt into one. Wired via `ApprovalManager.menu_prompt`; legacy `approval_func`/`remember_prompt` path preserved for tests.
  - **CLI polish:** removed the doubled "Local runtime ready" (dropped the redundant `ollama status` pre-flight from `run-shamsu.ps1`/`.sh`); consolidated startup into one panel (version/workspace/model/autonomy/runtime) with a richer bottom status bar; live tool-activity lines during agent/build runs (`AgentChatLoop.on_activity`); **streaming** token-by-token QA/chat answers (`LLMManager.run_specialist_stream`/`_generate_stream`, rendered in `_run_qa`/`_run_general_chat`, gated by `hasattr` so test doubles keep non-streaming behavior; spinner stops on first token to avoid a nested Rich Live); multi-line input (prompt_toolkit `multiline=True` with Enter=submit / Alt+Enter=newline, plus a leading-BOM strip so piped/odd input and slash commands aren't broken).

## Completed Features

- [x] Unpacked `SHAMSU_day1_scaffold.zip`.
- [x] Added Python package scaffold in `shamsu/`.
- [x] Added project config in `pyproject.toml`.
- [x] Added baseline CI config and PR template under `.github/`.
- [x] Added SQLite storage schema with FTS5 tables.
- [x] Added `SearchAgent` and `SearchAgentStub`.
- [x] Added context builder with snippet packing and middle truncation.
- [x] Added LLM manager with routing JSON parsing and repair fallback.
- [x] Added workspace sandbox.
- [x] Added command risk classification and secret redaction.
- [x] Added recursive file walker with ignore rules and streamed sha256 hashing.
- [x] Added Python AST symbol parser.
- [x] Indexed Python imports, classes, functions, methods, docstrings, signatures, and line ranges.
- [x] Indexed searchable line-window snippets into SQLite FTS5.
- [x] Removed stale index rows when files are moved or deleted.
- [x] Added Markdown PRD parser.
- [x] Added rule-based PRD entity extractor.
- [x] Added `ProjectSpec` assembly from parsed PRDs and extracted entities.
- [x] Added deterministic Django fixed-template constants.
- [x] Added fixed Django template renderer.
- [x] Added Rich approval prompt.
- [x] Added thin coordinator with safe QA fallback when Ollama is unavailable.
- [x] Added QA workflow preview using `SearchAgentStub` and `ContextBuilder`.
- [x] Updated REPL with `index`, `parse-prd <file.md>`, and QA preview.
- [x] Added REPL `status`, `search <query>`, and `symbols <name>` commands.
- [x] REPL QA preview uses real indexed search when `.shamsu/index.db` exists.
- [x] Added detailed README with install, run, safety, usage, and troubleshooting sections.
- [x] Added PowerShell install/run scripts using repo-local `.venv`.
- [x] Added Bash install/run scripts using repo-local `.venv`.
- [x] Added CLI `--workspace <path>` support.
- [x] Added workspace sandbox validation for `parse-prd`.
- [x] Moved planning and agent-memory docs into `agent context/`.
- [x] Added internal `CommandRunner` with workspace validation, blocked-command rejection, approval gates, timeouts, captured output, and redaction.
- [x] Added `CommandRunner.run_tests()` pytest summary parsing.
- [x] Added internal `PatchEngine` validation for unified diff headers, hunks, line counts, and workspace-safe paths.
- [x] Added Rich patch preview with changed-file summary and colorized diff body.
- [x] Added approval-backed patch `apply()` with validation, Rich preview, `.bak` backups, workspace safety, file create/delete support, and failure rollback.
- [x] Added patch `rollback()` that restores `.bak` backups.
- [x] Added automatic full index refresh after successful patch apply so modified, created, and deleted files are reflected in `.shamsu/index.db`.
- [x] Added read-only git helper for `git status --short`, `git diff`, and dirty-worktree warnings.
- [x] Added code edit workflow that searches indexed context, calls the `coder` specialist, validates unified diffs, applies via `PatchEngine`, and reports changed files.
- [x] Added `agent context/DEV-TASK-DIVI.MD` with remaining project work split into GitHub-issue-ready Dev A/B/C tasks.
- [x] Added branch hierarchy and PR rules to `agent context/DEV-TASK-DIVI.MD`.
- [x] Created GitHub core branches: `develop`, `dev-a`, `dev-b`, and `dev-c`.
- [x] Enabled branch protection for `main` and `develop`.
- [x] Added real indexed QA as the default REPL behavior when `.shamsu/index.db` exists.
- [x] Added explicit no-index fallback message instead of silently showing stub context.
- [x] Added live QA integration through `LLMManager.run_specialist("qa", ...)` with safe preview fallback when Ollama is unavailable.
- [x] Added read-only audit workflow that uses indexed search, packs reviewer context, and parses structured findings.
- [x] Added documentation proposal workflow that uses indexed context, calls `doc_agent`, and generates README unified diffs for review.
- [x] Added bug fix workflow that parses traceback locations, gathers indexed context, calls the `bugfix` specialist, validates unified diffs, applies via `PatchEngine`, and reports changed files.
- [x] Added test generation workflow that gathers indexed context, calls the `test_gen` specialist, validates pytest-oriented unified diffs, applies via `PatchEngine`, and can run tests through `CommandRunner`.
- [x] Extended documentation workflow so README diffs can apply through approval-backed `PatchEngine` while preserving proposal-only behavior.
- [x] Added Claude-like CLI routing with prompt-toolkit input, natural-language intent dispatch, keyword fallback when Ollama routing is unavailable, and explicit workflow commands for edit/fix/test-gen/audit/docs.
- [x] Added LLM model aliases for `bugfix` and `test_gen` specialists so workflow names map to the intended local models.
- [x] Added native local runtime management for Ollama detection, local-only status, model checks/pulls, runtime config, and REPL `models status|pull|repair` commands.
- [x] Extended install scripts with safe runtime bootstrap flags while avoiding PowerShell profile, registry, shell startup files, and global Python edits.
- [x] Added managed Windows user PATH setup for `$HOME\.shamsu\bin` with a SHAMSU-owned manifest so uninstall removes only entries SHAMSU added.
- [x] Added safe Ollama-down handling in the ReAct chat loop so a stopped local runtime returns a `/models repair` message instead of crashing the REPL.
- [x] Fixed casual chat routing so greetings use the local chat loop instead of repeating the readiness banner, and filtered old status banners out of resumed chat memory.
- [x] Added unified PRD input parsing for Markdown, TXT, and PDF files.
- [x] Added friendly PRD parse errors for unsupported and empty/unreadable PDF inputs.
- [x] Extended PRD extraction for relationships, auth user references, choices, optional fields, decimals, booleans, text fields, max length, and defaults.
- [x] Extended `ProjectSpec` generation order into the future Django pipeline order.
- [x] Added REPL `plan-prd <file>` preview with entities, endpoints, pages, planned files, and approval gate.
- [x] Added natural-language PRD plan routing for prompts that mention a PRD file.
- [x] Added workspace-local `.shamsu/generation-state.json` helpers for accepted plans, step status, completed files, errors, and resume.
- [x] Extended fixed Django rendering with project/app `__init__.py` and app config files.
- [x] Added deterministic Django backend generators for models, serializers, forms, views, app URLs, and admin.
- [x] Added approval-backed `DjangoProjectWriter` with workspace sandboxing, overwrite approval, resume-state updates, and skipped later-milestone steps.
- [x] Added static backend consistency checker for generated models, serializers, forms, views, URLs, and admin references.
- [x] Added REPL `generate-django <file>` command and natural-language Django generation routing.
- [x] Added workspace-local session storage under `.shamsu/sessions/` with session metadata, JSONL events, context/export folders, and an index.
- [x] Added default session resume behavior, `--session`, `--new-session`, and REPL `sessions list/current/show/resume/rename/close/export` commands.
- [x] Added redacted session event logging for prompts, routing, context packs, local LLM calls, approvals, patches, commands, PRD parsing, project planning, and Django generation.
- [x] Added `log tail` and redacted session ZIP exports with `session.json`, `events.jsonl`, and Markdown summary.
- [x] Merged M4 deterministic Django backend generation into `develop` and closed issues #15-#23.
- [x] Merged session logging/resume PR #48 into `develop`.
- [x] Added Milestone 5 Django setup runner for generated projects: validates project cwd inside the workspace, installs generated requirements, runs `makemigrations`/`migrate` through `CommandRunner`, redacts output, and returns structured bug-fix context on failure.
- [x] Added REPL `django setup [project-dir]` command with Rich setup results and session-aware command logging.
- [x] Added Milestone 5 Django test runner for generated projects with `python manage.py test --verbosity=2`, structured OK/failure/error parsing, redaction, and REPL `django test [project-dir]`.
- [x] Added deterministic generated Django `tests.py` output with `TestCase`, DRF `APIClient`, authenticated setup, and CRUD smoke coverage for generated ViewSets.
- [x] Added deterministic dashboard/resource frontend templates for M5, including DaisyUI stats/tables/cards and crispy/HTMX form markup.
- [x] Added error feedback loop that runs generated Django tests, sends failures into `BugFixWorkflow`, applies approved diffs, and retries up to three times.
- [x] Added REPL `django fix-tests [project-dir]` command for the generated-project test/fix loop.
- [x] Added M6 full PRD-to-Django pipeline orchestrator that parses a PRD, builds a project plan, writes generated files, runs setup, runs Django tests, and invokes the error feedback loop when tests fail.
- [x] Added REPL `generate-prd <file> --output <dir>` command for the full pipeline.
- [x] Added Todo, Expense Tracker, and Blog PRD fixtures for M6 end-to-end generation coverage.
- [x] Added generated project README and `SHAMSU_SUMMARY.md` output with install, migrate, test, run, generated files, command results, and warnings.
- [x] Added MVP benchmark script and `BENCHMARK.md` recording representative PRD generation runtime and peak RSS against the 7 GB target.
- [x] Added frontend consistency checker for generated Django templates covering missing URL names, invalid model field references, missing HTMX targets, and raw generated form controls.
- [x] Added dirty-worktree warning before edit/fix/test-generation/docs workflows.
- [x] Fixed Windows `models pull`/`models repair` UnicodeDecodeError by forcing UTF-8 mode in launch/install scripts and decoding Ollama subprocess output with UTF-8 replacement.
- [x] Folded user-local launcher creation into the main install scripts so one install command sets up SHAMSU and the from-any-repo launcher.
- [x] Added approval-backed in-chat model download flow with per-model progress and rerun/resume guidance for failed Ollama pulls.
- [x] Added no-index general local chat so conversational prompts work without forcing project indexing first.
- [x] Added guided runtime-repair prompt from workflow failures so SHAMSU can kick off `models repair` from inside the chat flow.
- [x] Added uninstall scripts for Windows and Bash that remove SHAMSU-managed repo state and user-local launchers without touching Ollama or other workspace `.shamsu` folders.
- [x] Added automatic permission-gated web lookup routing plus explicit `/web` commands for external/current questions.
- [x] Added Playwright-backed browser tooling and explicit `/browse` commands for local app preview and debugging flows.
- [x] Added `AgentOrchestrator` that injects workspace facts, conversation memory, available tools, and `@file` context before model routing.
- [x] Added durable session-based conversation memory for follow-up prompts like "check on the web", "open it", and "do that".
- [x] Added read-only workspace tools for file listing, PRD discovery, file reads, folder summaries, and prompt-toolkit `@` path autocomplete.
- [x] Added central `ApprovalManager` logging for permission-gated web, browser, command, and patch actions.
- [x] Improved web page extraction with `trafilatura`, useful-page filtering, source summaries, and location clarification for weather prompts.
- [x] Replaced `phi3:mini`/`gemma3:4b` primary routing with `qwen3:8b` for router/chat/planner/docs/summarizer, keeping `gemma3:4b` as a low-resource fallback option and teaching installers to install Playwright Chromium support.
- [x] Added stateful `ChatState` plus Ollama SDK ReAct loop with native tool calls for file reads/writes, command execution, index search, and file listing.
- [x] Added markdown code-block fallback for small models that fail native tool calling on simple file creation prompts.
- [x] Hardened slash-command routing so invalid `/...` input never reaches the LLM and suggests nearby commands locally.
- [x] Added `shamsu/runtime/doctor.py` read-only diagnostics (editable-install sanity, Ollama status, stray nested `.shamsu` detection, ancestor-workspace conflicts, PATH manifest health), `/doctor` REPL command, and `scripts/doctor.ps1`/`.sh`.
- [x] Made `install.ps1`/`.sh` non-fatal on a flaky Playwright/winget/Ollama-install step (warn and continue) and skip redundant Chromium reinstall checks via a marker file.
- [x] Added an ancestor-workspace startup warning in the REPL and recursive stray-`.shamsu` cleanup in `uninstall.ps1`/`.sh`; rewrote README troubleshooting to lead with `doctor` instead of wipe-and-reinstall.
- [x] Added `shamsu.runtime.ollama.ensure_model_available()` and wired `LLMManager` to lazily pull a missing specialist/router model on first use (per-model locked, Rich progress bar), with install now defaulting to no upfront model downloads (`-PrefetchModels`/`--prefetch-models` opts back in).
- [x] Added `shamsu/safety/permission_store.py` (`PermissionMemory`) and extended `ApprovalManager` to auto-approve remembered `file_write`/`file_edit` decisions (session or workspace-persisted) while never auto-approving `run_command`/`file_delete`/`web_search`/`mcp_tool`; added the "remember this?" follow-up prompt and `/permissions list|clear`.
- [x] Migrated `agent_tools.py`'s `write_file`, the Django project writer's two approval points, and `plan-prd`'s approval off ad-hoc `approval_func` calls onto the central `ApprovalManager`.
- [x] Made `FileWalker.index()` incremental (skip rehash for unchanged size/mtime; skip symbol/snippet rebuild when the hash is unchanged) and added an `index.updated` session-log event.
- [x] Added shared `shamsu.indexer.walker.ensure_index()` (best-effort, swallows indexing errors) and wired it into every search/QA/status/search/symbols call site and into `AgentOrchestrator`, so indexing now happens transparently without a manual `/index` step or "no index found" message in normal use.
- [x] Wired `SearchAgent.search()` to over-fetch from FTS5 and re-rank with additive file-path-match, symbol-match (bulk `symbols` table query), lazily-built recent-snippet `rank_bm25` recency, and optional `boost_paths` (used by `BugFixWorkflow` for traceback-location promotion) boosts.
- [x] Added `shamsu/tasks/state.py` (`MilestoneTask`, phase gating via `advance_phase()`, `mark_step_blocked()` distinct from `FAILED`, files/commands/test-results tracking, `.shamsu/tasks/<id>.json` persistence) and `/tasks list|show <id>`.
- [x] Added `shamsu/llm/council.py` (sequential draft/critique/reconcile, `should_convene_council()` heuristic) wired into `BugFixWorkflow`.
- [x] Added `shamsu/safety/clarify.py` (free-text clarifying question, distinct from the approve/deny gate) and `shamsu/safety/autonomy.py` (per-workspace opt-in toggle, `.shamsu/autonomy.json`, default off) with `/autonomy status|on|off`.
- [x] Added `long_running` mode to `AgentChatLoop` (higher round ceiling + repetition guard that asks a clarifying question), `ErrorFeedbackLoop` (higher iteration ceiling + stall detection), and `FullDjangoPipeline` (one bounded bugfix-and-retry pass on setup failure) — all default to today's exact capped behavior when the toggle is off.
- [x] Added SHAMSU v2.2 foundation: two-anchor model cookbook, single-model mode, off-cookbook pull refusal, doctor cookbook check, archetype fields/classifier, typed-hole manifest contract, and archetype template registry.
- [x] Added SHAMSU v2.3 phase-one category registry with YAML-backed manifest/master-prompt/DoD loading, deterministic category routing, and forced tech-stack policies for each v2.3 category.
- [x] Added baseline `multiplayer-game` template with menu, lobby, player list, local/remote player rendering, Colyseus relay client/server, Rapier dependency, game loop, HUD, and end condition shell.
- [x] Added reusable Definition-of-Done runner and checks for file existence, command/build readiness, source-level element presence, WebSocket client, two-player rendering, state advancement, and reachable end state.
- [x] Wired `generate-prd` to scaffold `multiplayer-game` PRDs through the new registry path while preserving the existing Django pipeline for CRUD/API projects.

## In Progress

- [ ] v2.3 phase two: marker-based hole filling, bounded DoD repair loop, and Playwright runtime checks for the multiplayer-game template.
- [ ] Milestone 6 status/log/progress views, safety audit, release docs, and final release cut.

## Next Queue

1. Try `/autonomy on` in real use and confirm it's safe before considering it as a future default; wire council mode into `CodeEditWorkflow` as a follow-up.
2. Open PR for Issue #27 from `feature/dev-a/frontend-consistency-checker` into `develop`.
3. Rebuild or replace stale/conflicting PR #54 from fresh `develop` for Issues #33/#34/#38/#39.
4. Finish Issue #40 release cut after all Issues #1-#39 are merged and verified.

## Known Notes

- Keep `shamsu/types.py` and `shamsu/interfaces.py` stable unless the team explicitly agrees to change the contract.
- The root README is for humans; `agent context/AGENTS.md` and this file are for future agent handoff.
- `agent context/DEV-TASK-DIVI.MD` is the issue/PR planning board for the remaining MVP work.
- `agent context/MILESTONE-2-FINISH-PLAN.md` records the completed Milestone 2
  merge checklist and verification path.
- Feature work should branch from `develop` and merge back through PRs. `main`
  is protected for stable milestone merges only.
- PR #53 (`dev-a`) was reviewed on 2026-07-02 and is conflicting/stale; the safe dirty-worktree warning behavior was salvaged onto `feature/dev-a/frontend-consistency-checker`.
- PR #54 (`dev-c`) was reviewed on 2026-07-02 and is conflicting with failing CI; do not merge it directly. Reapply useful Dev C work onto fresh branches from `develop`.
- `SHAMSU_day1_scaffold.zip` remains at the repo root as the original scaffold artifact.
- Some copied planning docs contain mojibake. Avoid broad formatting churn unless asked.

## Verification Commands

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
.\.venv\Scripts\python.exe -m ruff check shamsu tests scripts
.\scripts\run-shamsu.ps1
```

## Update Rule For Agents

Before ending a task, update this file if any of these changed:

- completed feature list
- current state
- next queue
- known blockers
- verification status
