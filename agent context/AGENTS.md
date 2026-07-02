# Agent Context: SHAMSU

This file is the quick-start context for agents working in this repository.

## Repository Snapshot

- Repo path: `F:\Work\PROJECTS\shamsu\Shamsu`
- Remote: `https://github.com/SowadHossain/Shamsu.git`
- Current branch: `main`
- Current state: Day-1 scaffold unpacked, installed, and extended with the first dev-plan slice.
- Existing source documents:
  - `agent context/REQUIREMENTS.md`: full product requirements for SHAMSU.
  - `agent context/SHAMSU_week2_milestone_v2.md`: v0.2.0 implementation milestone focused on PRD-to-Django project generation.
  - `agent context/SHAMSU_10day_dev_plan.md`: current 10-day build plan based on the scaffold zip.
  - `agent context/PROGRESS.md`: live completed-feature and next-task tracker.

## Product Identity

SHAMSU is a local-first autonomous coding agent for low-resource machines.

Core promise:

- Run on sub-8GB devices.
- Avoid cloud API bills.
- Keep source code local and private.
- Use deterministic tools for scanning, indexing, parsing, searching, and validation.
- Use small local LLMs only for reasoning, planning, summarization, and code generation.

The central engineering principle is:

> Do not use the LLM as a brute-force scanner. Use tools to find the right context, then use the LLM to reason and generate.

## MVP Scope From Requirements

The MVP should include:

- CLI interface.
- Project folder loading.
- Workspace sandbox enforcement.
- Non-LLM project indexing.
- Search and retrieval.
- Context builder.
- Local small LLM integration.
- Markdown, TXT, and PDF PRD parsing.
- Task planning from PRD.
- Code generation.
- File creation.
- Patch-based code editing.
- Patch preview and approval.
- Dangerous command blocking.
- Approved test execution.
- Progress logging.
- Basic documentation generation.
- Basic bug fixing.
- Basic code audit.

## Target Architecture

The requirements describe these main modules:

- CLI interface
- Coordinator agent
- Planner agent
- Search agent / retriever
- Project indexer
- Context builder
- LLM manager
- Code writer
- Review agent
- Test agent
- Documentation agent
- Safety manager
- Tool executor
- Logger
- Storage layer

Suggested local data folder:

```text
.shamsu/
  index.db
  tasks/
  logs/
  context/
  skills/
  config.json
```

## Current Milestone Direction

`SHAMSU_week2_milestone_v2.md` narrows v0.2.0 toward generating complete Django web projects from PRDs.

Chosen generated-app stack:

- Python 3.11+
- Django 5
- Django REST Framework
- django.contrib.auth plus simplejwt
- SQLite for development
- Django templates
- HTMX
- DaisyUI and Tailwind via CDN
- django-crispy-forms plus crispy-tailwind
- Django TestCase plus DRF APIClient

Rationale:

- Django and DRF reduce generated code volume.
- Built-in auth and ORM reduce custom security and persistence code.
- Templates plus HTMX avoid a separate frontend server, CORS, JWT-in-browser complexity, and node_modules.
- DaisyUI gives stable semantic class names for small-model-friendly UI generation.

## Week 2 Pipeline To Preserve

The milestone proposes this PRD-to-project flow:

1. Parse PRD with non-LLM tools.
2. Extract entities, endpoints, pages, and relationships.
3. Use a planner model to create a project plan.
4. Ask for approval.
5. Generate fixed template files without LLM calls.
6. Generate Django backend files in dependency order:
   - `models.py`
   - `serializers.py`
   - `forms.py`
   - `views.py`
   - `urls.py`
   - `admin.py`
7. Generate frontend templates after backend URL and view names exist.
8. Generate tests.
9. Run install, migration, and test commands behind approval gates.
10. Feed errors into a targeted bug-fix loop.
11. Generate README and final summary.

Dependency order matters. Do not generate templates before URLs/views exist, and do not generate serializers/forms/views before models exist.

## Safety Rules To Keep Front And Center

The system is safety-first by design:

- Treat the active project folder as the workspace boundary.
- Block path traversal and sensitive system paths.
- Ask before writing, editing, deleting, moving files, installing dependencies, running commands, accessing the internet, or calling external tools.
- Prefer patch-based edits with preview.
- Block dangerous commands by default.
- Redact secrets in logs and summaries.
- Do not send private project source code to external web services.
- Log file modifications and command executions.

## Practical Next Implementation Path

The scaffold package now exists. The next step is to continue the day-by-day plan from `agent context/SHAMSU_10day_dev_plan.md`.
Update `agent context/PROGRESS.md` at the end of each feature slice.

Completed first slice:

- Scaffold extracted from `SHAMSU_day1_scaffold.zip`.
- `pyproject.toml`, package files, `.github` CI config, and baseline tests are present.
- Baseline scaffold tests pass.
- `indexer/walker.py` indexes project files into SQLite using streamed sha256 hashing.
- `indexer/parser.py` extracts Python imports, classes, functions, methods, docstrings, signatures, and line ranges.
- The file walker now writes symbols and searchable line-window snippets.
- The file walker removes stale index rows after file moves/deletes.
- `core/coordinator.py` routes requests and falls back to QA preview if Ollama is unavailable.
- `agents/qa_workflow.py` wires `SearchAgentStub` to `ContextBuilder`.
- `prd/parser.py` parses Markdown/plain-text PRD content into `ParsedPRD`.
- `prd/input.py` accepts Markdown, TXT, and PDF PRD files.
- `prd/extractor.py` extracts `EntitySpec` values, field types, choices, optional fields, and relationships from PRD entity sections.
- `prd/project.py` assembles `ProjectSpec` values with inferred endpoints, pages, theme, and generation order.
- `prd/state.py` stores accepted generation-plan resume state under workspace `.shamsu/`.
- `templates/django/constants.py` and `templates/django/renderer.py` provide deterministic fixed Django generation.
- `templates/django/generators.py` deterministically generates backend `models.py`, `serializers.py`, `forms.py`, `views.py`, app `urls.py`, and `admin.py`.
- `templates/django/writer.py` writes generated Django files inside the workspace behind approval and updates generation resume state.
- `templates/django/checker.py` statically checks backend model/serializer/form/view/url/admin references.
- `safety/approval.py` displays Rich approval panels.
- `cli/repl.py` supports `--workspace <path>`, `index`, `status`, `search <query>`, `symbols <name>`, `parse-prd <file>`, `plan-prd <file>`, `generate-django <file>`, and QA context preview.
- `scripts/install.ps1` and `scripts/install.sh` install into repo-local `.venv`.
- `scripts/run-shamsu.ps1` and `scripts/run-shamsu.sh` run SHAMSU from that `.venv` while preserving the caller workspace.
- `parse-prd` and `plan-prd` file inputs are validated through `Sandbox.validate()`.
- `tools/executor.py` provides an internal `CommandRunner` with workspace-bound `cwd` validation, blocked-command rejection, approval gates, timeout handling, output capture, and redaction.
- `patch/engine.py` validates unified diffs, checks hunk structure and counts, and rejects unsafe patch paths.
- `patch/preview.py` renders Rich patch summaries and colorized diff previews.

Recommended next slice:

1. Add frontend page generation for dashboard/list/detail/form templates.
2. Add migration/dependency/test runner flow for generated Django projects.
3. Add generated-project feedback loop that uses test/check failures to produce fixes.
4. Keep `types.py` and `interfaces.py` frozen unless the team explicitly agrees to change them.

## Suggested Initial File Layout

```text
src/shamsu/
  __init__.py
  cli.py
  config.py
  specs.py
  safety.py
  workspace.py
  logging.py
  prd/
    __init__.py
    parser.py
    extractor.py
  generation/
    __init__.py
    django_project.py
    templates.py
    validators.py
  tools/
    __init__.py
    command_runner.py
    patcher.py
  indexing/
    __init__.py
    file_walker.py
    symbols.py
  llm/
    __init__.py
    manager.py
    prompts.py
tests/
```

## Development Notes

- Use Python-first tooling unless the project direction changes.
- Keep generated-app templates deterministic whenever possible.
- Use local files and indexes as the handoff mechanism between models.
- Keep memory usage low; avoid always-on heavy services.
- Keep implementation small and testable. The project is trying to help small models, so the codebase itself should be boring in the best way.
- Some existing markdown text appears to contain mojibake/encoding artifacts. Preserve meaning when editing docs, but avoid broad formatting churn unless the user asks for cleanup.

## Useful Commands

Current repository inspection:

```powershell
git status --short --branch
git log --oneline -5
rg -n "^(#|##|###) " "agent context"
python -m pytest tests/ -v
python -m ruff check shamsu tests
python -m shamsu.indexer.walker
.\scripts\install.ps1
.\scripts\run-shamsu.ps1
```

REPL smoke checks:

```powershell
@'
index
how does login work?
exit
'@ | python -m shamsu.cli.repl

@'
parse-prd "agent context/SHAMSU_10day_dev_plan.md"
exit
'@ | python -m shamsu.cli.repl
```
