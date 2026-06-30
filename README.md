# SHAMSU

Local-first autonomous coding agent for low-resource machines.

SHAMSU is designed to work like a lightweight coding teammate that can inspect,
index, understand, edit, audit, fix, test, document, and eventually generate
software projects from PRDs without relying on expensive cloud AI APIs.

The core idea is simple:

> Use tools to find the right context, then use a small local model to reason
> over that context.

SHAMSU should not throw an entire codebase into an LLM prompt. It should use
deterministic indexing, search, parsing, retrieval, validation, patching, and
safety checks first, then call local models only where language reasoning or
generation is useful.

## Current Status

This repository now contains the Day-1 scaffold from the development plan plus
the first working implementation slice.

Included and working:

- Python package scaffold in `shamsu/`
- CLI entrypoint: `shamsu`
- SQLite storage schema with FTS5 tables
- Search agent stub and initial FTS5 search implementation
- Context builder with snippet packing and middle truncation
- LLM manager with routing JSON parsing and repair fallback
- Workspace sandbox and command risk classification
- Recursive project file walker
- Python AST symbol parser
- Searchable snippet indexing through SQLite FTS5
- Markdown PRD parser
- Rule-based PRD entity extractor
- Rich approval prompt
- Thin coordinator and QA context preview workflow
- Baseline tests and CI configuration

The repo also includes product and milestone docs:

- `REQUIREMENTS.md`
- `SHAMSU_10day_dev_plan.md`
- `SHAMSU_week2_milestone_v2.md`
- `AGENTS.md`

## Install

Use Python 3.11 or newer.

```powershell
python -m pip install -e ".[dev]"
```

## Verify

Run the test suite:

```powershell
python -m pytest tests/ -v
```

Run lint:

```powershell
python -m ruff check shamsu tests
```

Expected current result:

```text
32 passed
All checks passed!
```

## Run The CLI

```powershell
python -m shamsu.cli.repl
```

or, after editable install:

```powershell
shamsu
```

Available Day-1 commands:

```text
index
parse-prd <file.md>
help
exit
```

Any other text is treated as a natural-language request. For now, the
coordinator routes it and builds a QA context preview. If Ollama is not running,
the coordinator falls back safely to QA mode instead of crashing.

Example smoke test:

```powershell
@'
index
how does login work?
exit
'@ | python -m shamsu.cli.repl
```

Parse a Markdown PRD-like document:

```powershell
@'
parse-prd SHAMSU_10day_dev_plan.md
exit
'@ | python -m shamsu.cli.repl
```

Run the file walker directly:

```powershell
python -m shamsu.indexer.walker
```

## Project Layout

```text
shamsu/
  agents/          Workflow agents, currently QA preview
  cli/             REPL entrypoint
  context/         Context packing and token budget helpers
  core/            Coordinator
  indexer/         File walking and future symbol parsing
  llm/             Ollama-backed LLM manager
  patch/           Future patch engine
  prd/             PRD parsing and future extraction
  retriever/       Search agents
  safety/          Sandbox, command risk, approvals
  skills/          Future reusable skills
  storage/         SQLite schema
  templates/       Future Django/frontend templates
  tools/           Future command and git tools
tests/
```

## Development Plan

The active plan is `SHAMSU_10day_dev_plan.md`.

Completed Day-1 work:

- Unpacked scaffold and verified baseline tests.
- Added `indexer/walker.py`.
- Added `core/coordinator.py`.
- Added `agents/qa_workflow.py`.
- Added `prd/parser.py`.
- Added `safety/approval.py`.
- Updated `cli/repl.py` to exercise the working slices.

Completed next slice:

- Added `indexer/parser.py` using Python `ast` to extract functions, classes,
  methods, imports, docstrings, signatures, and line ranges.
- Updated the walker to write extracted symbols into the existing `symbols`
  table.
- Updated indexing to write line-window snippets so `SearchAgent` can search
  real repository content through FTS5.
- Added `prd/extractor.py` to turn `## Entities` sections into `EntitySpec`
  objects.

Recommended next slice:

1. Add fixed Django template constants under `shamsu/templates/django/`.
2. Add `ProjectSpec` assembly from parsed PRDs and extracted entities.
3. Add a deterministic Django settings/base template renderer.
4. Add CLI commands for status and real indexed search.

## Safety Principles

SHAMSU is safety-first:

- Treat the active workspace as the boundary.
- Block path traversal.
- Block dangerous commands.
- Require approval for risky commands and writes.
- Prefer patch previews before edits.
- Redact secrets in logs and outputs.
- Do not send private code to external services.

## Notes For Contributors

- Keep `shamsu/types.py` and `shamsu/interfaces.py` stable unless the team
  explicitly agrees to change the shared contract.
- Prefer deterministic tooling before LLM calls.
- Keep memory use low; avoid loading full projects into memory.
- Add tests with each feature slice.
- Run `pytest` and `ruff` before handing off.
