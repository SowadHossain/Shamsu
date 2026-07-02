# SHAMSU Progress Tracker

This is the living implementation ledger. Every agent should update this file
after completing a feature slice, changing priorities, or discovering a
blocker.

## Current State

- Status: Milestone 4 implemented locally on top of `develop`; Day 1 scaffold complete; Day 2 indexing/PRD extraction complete; deterministic Django template and ProjectSpec slice complete; install/run scripts, safer workspace CLI, internal command runner, patch validation/preview, patch apply/rollback, post-patch re-indexing, read-only git tooling, code edit workflow, real indexed QA fallback, live QA integration, audit workflow, documentation proposal/apply workflow, bug fix workflow, test generation workflow, CLI workflow routing, native local Ollama runtime bootstrap, TXT/PDF PRD input, PRD extractor v2, project plan preview/approval, generation resume state, deterministic Django backend generation, approval-backed project writer, backend consistency checker, workspace-local session logging/resume, M5 generated-project setup/test/fix loop, M6 full pipeline fixtures/benchmark/docs, Dev A frontend consistency checking, and dirty-worktree edit warnings complete.
- Tests: `183 passed`
- Lint: `python -m ruff check shamsu tests` passes.
- Last verified: 2026-07-02
- Current next focus: finish remaining Milestone 6 Dev C status/log/progress, release docs, final safety audit, then release cut.

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
- [x] Extended install scripts with safe runtime bootstrap flags while avoiding PowerShell profile, PATH, registry, shell startup files, and global Python edits.
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

## In Progress

- [ ] Milestone 6 status/log/progress views, safety audit, release docs, and final release cut.

## Next Queue

1. Open PR for Issue #27 from `feature/dev-a/frontend-consistency-checker` into `develop`.
2. Rebuild or replace stale/conflicting PR #54 from fresh `develop` for Issues #33/#34/#38/#39.
3. Finish Issue #40 release cut after all Issues #1-#39 are merged and verified.

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
.\.venv\Scripts\python.exe -m ruff check shamsu tests
.\scripts\run-shamsu.ps1
```

## Update Rule For Agents

Before ending a task, update this file if any of these changed:

- completed feature list
- current state
- next queue
- known blockers
- verification status
