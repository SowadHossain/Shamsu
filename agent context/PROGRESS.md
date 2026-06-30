# SHAMSU Progress Tracker

This is the living implementation ledger. Every agent should update this file
after completing a feature slice, changing priorities, or discovering a
blocker.

## Current State

- Status: Day 1 scaffold complete; Day 2 indexing/PRD extraction complete; deterministic Django template and ProjectSpec slice complete.
- Tests: `36 passed`
- Lint: `python -m ruff check shamsu tests` passes.
- Last verified: 2026-06-30
- Current next focus: command runner with safety gates, then patch validation/preview.

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
- [x] Added README.
- [x] Moved planning and agent-memory docs into `agent context/`.

## In Progress

- [ ] Command runner with safety gates.
- [ ] Patch validation and preview.

## Next Queue

1. Add command runner with safety gates:
   - classify commands with `classify_command()`
   - block `CommandRisk.BLOCKED`
   - require approval for `CommandRisk.MEDIUM`
   - run `CommandRisk.SAFE`
   - capture stdout/stderr
2. Add patch validation and preview:
   - validate unified diff headers
   - reject malformed hunks
   - show Rich diff preview
   - prepare rollback strategy
3. Add real indexed QA workflow as the default after `index`.
4. Add deterministic Django project writer that can write rendered fixed templates into a target directory behind approval.
5. Add `ProjectSpec` JSON preview command for PRDs.

## Known Notes

- Keep `shamsu/types.py` and `shamsu/interfaces.py` stable unless the team explicitly agrees to change the contract.
- The root README is for humans; `agent context/AGENTS.md` and this file are for future agent handoff.
- `SHAMSU_day1_scaffold.zip` remains at the repo root as the original scaffold artifact.
- Some copied planning docs contain mojibake. Avoid broad formatting churn unless asked.

## Verification Commands

```powershell
python -m pytest tests/ -q
python -m ruff check shamsu tests
python -m shamsu.indexer.walker
```

## Update Rule For Agents

Before ending a task, update this file if any of these changed:

- completed feature list
- current state
- next queue
- known blockers
- verification status
