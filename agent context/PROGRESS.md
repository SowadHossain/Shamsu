# SHAMSU Progress Tracker

This is the living implementation ledger. Every agent should update this file
after completing a feature slice, changing priorities, or discovering a
blocker.

## Current State

- Status: Day 1 scaffold complete; Day 2 indexing/PRD extraction complete; deterministic Django template and ProjectSpec slice complete; install/run scripts, safer workspace CLI, internal command runner, and patch validation/preview complete.
- Tests: `60 passed`
- Lint: `python -m ruff check shamsu tests` passes.
- Last verified: 2026-07-01
- Current next focus: patch application and rollback behind approval.

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
- [x] Kept patch `apply()` and `rollback()` as non-mutating stubs until the approval-backed apply slice.
- [x] Added `agent context/DEV-TASK-DIVI.MD` with remaining project work split into GitHub-issue-ready Dev A/B/C tasks.

## In Progress

- [ ] Patch application and rollback behind approval.

## Next Queue

1. Add patch application and rollback behind approval:
   - validate and preview before apply
   - ask approval with patch summary
   - create `.bak` backups before writes
   - restore backups on apply failure
2. Add real indexed QA workflow as the default after `index`.
3. Add deterministic Django project writer that can write rendered fixed templates into a target directory behind approval.
4. Add `ProjectSpec` JSON preview command for PRDs.

## Known Notes

- Keep `shamsu/types.py` and `shamsu/interfaces.py` stable unless the team explicitly agrees to change the contract.
- The root README is for humans; `agent context/AGENTS.md` and this file are for future agent handoff.
- `agent context/DEV-TASK-DIVI.MD` is the issue/PR planning board for the remaining MVP work.
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
