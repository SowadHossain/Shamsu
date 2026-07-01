# SHAMSU Dev Task Division

Last updated: 2026-07-01

This file is the issue-ready build plan for the rest of SHAMSU. Use it to create
GitHub issues, assign work, open branches, and track PRs.

Current baseline:

- Repo install/run docs are complete.
- CLI workspace sandbox is complete.
- Indexing, Python symbols, FTS search, PRD parsing, `ProjectSpec`, fixed Django
  template constants, command runner, patch validation, and Rich patch preview
  are complete.
- Current verified state: `60 passed`, `ruff check shamsu tests` clean.
- Next implementation focus: patch application and rollback behind approval.

## Already Done - Do Not Create Issues

These features are already implemented and verified in the current codebase.
Do not create new GitHub issues for them unless a bug/regression is found.

- [x] Day 1 scaffold unpacked and project structure created.
- [x] `pyproject.toml`, editable package setup, baseline CI config, and PR
  template added.
- [x] Core shared contracts in `shamsu/types.py` and `shamsu/interfaces.py`
  exist. Keep them stable.
- [x] SQLite storage schema with FTS5 tables exists.
- [x] `SearchAgent` and `SearchAgentStub` exist.
- [x] Context builder exists with snippet packing and middle truncation.
- [x] LLM manager exists with router JSON parsing and repair fallback.
- [x] Workspace sandbox exists and blocks path traversal.
- [x] Command risk classifier and secret redaction helpers exist.
- [x] Recursive file walker exists with ignore rules and streamed sha256 hashing.
- [x] Python AST symbol parser exists.
- [x] Indexer stores files, imports, classes, functions, methods, docstrings,
  signatures, line ranges, and searchable snippets.
- [x] Indexer removes stale rows when files move or are deleted.
- [x] Markdown PRD parser exists.
- [x] Rule-based PRD entity extractor exists.
- [x] `ProjectSpec` assembly from parsed PRDs exists.
- [x] Deterministic Django fixed-template constants exist.
- [x] Fixed Django template renderer exists.
- [x] Rich approval prompt exists.
- [x] Thin coordinator exists with safe QA fallback when Ollama is unavailable.
- [x] QA workflow preview exists using indexed search when `.shamsu/index.db`
  exists.
- [x] REPL supports `index`, `status`, `search <query>`, `symbols <name>`,
  `parse-prd <file.md>`, and QA context preview.
- [x] README includes install, run, safety, usage, and troubleshooting docs.
- [x] PowerShell and Bash install scripts create/use repo-local `.venv`.
- [x] PowerShell and Bash run scripts execute SHAMSU from repo-local `.venv`.
- [x] CLI supports `--workspace <path>`.
- [x] `parse-prd` file inputs are validated through `Sandbox.validate()`.
- [x] Planning and agent-memory docs live in `agent context/`.
- [x] Internal `CommandRunner` exists with workspace validation,
  blocked-command rejection, approval gates, timeouts, captured output, and
  redaction.
- [x] `CommandRunner.run_tests()` parses pytest summaries.
- [x] Internal `PatchEngine` validates unified diff headers, hunks, line counts,
  and workspace-safe paths.
- [x] Rich patch preview exists with changed-file summary and colorized diff
  body.
- [x] Patch `apply()` and `rollback()` are intentionally non-mutating stubs until
  Issue 1.

The issue list below starts from remaining work only.

## Team Ownership

Dev A / Dev 1: `Masturajannat`

Dev A owns the code-understanding and edit engine:

- `indexer/`, `retriever/`, `patch/`, `storage/`
- Patch apply/rollback, git awareness, incremental indexing, consistency checks,
  benchmark/RAM profiling, integration reliability.

Dev B / Dev 2: `SowadHossain`

Dev B owns the LLM and workflow brain:

- `core/`, `llm/`, `context/`, `agents/`
- Routing, specialist prompts, code-edit workflow, bug-fix workflow, audit,
  test generation, documentation generation, error feedback loop.

Dev C / Dev 3: `abidjan00`

Dev C owns product generation, CLI, and safety:

- `prd/`, `tools/`, `safety/`, `cli/`, `templates/`
- PRD extraction, Django project writer, fixed templates, migrations/test command
  wrappers, CLI progress/status/log commands, docs and release packaging.

## GitHub Workflow

- Create one GitHub issue per task below.
- Use branch names exactly as listed unless the issue scope changes.
- Every PR should include `Closes #ISSUE_NUMBER`.
- Keep PRs small enough to review in one sitting.
- Do not change `shamsu/types.py` or `shamsu/interfaces.py` unless the issue
  explicitly says "contract change" and all devs agree.
- Every PR must update `agent context/PROGRESS.md` when it changes completed
  work, verification status, or next focus.
- Every PR must pass:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
.\.venv\Scripts\python.exe -m ruff check shamsu tests
```

Recommended labels:

- `owner:dev-a`, `owner:dev-b`, `owner:dev-c`
- `area:patch`, `area:cli`, `area:prd`, `area:django`, `area:llm`,
  `area:safety`, `area:index`, `area:tests`, `area:docs`
- `type:feature`, `type:bug`, `type:integration`, `type:hardening`
- `priority:p0`, `priority:p1`, `priority:p2`
- `milestone:mvp`

GitHub assignee map:

- Dev A / Dev 1 / `Masturajannat`: Issues 1, 2, 3, 23, 27, 35, 36, 40.
- Dev B / Dev 2 / `SowadHossain`: Issues 4, 5, 6, 7, 8, 9, 14, 17, 18,
  20, 21, 22, 25, 29, 30, 31, 32, 37.
- Dev C / Dev 3 / `abidjan00`: Issues 10, 11, 12, 13, 15, 16, 19, 24, 26,
  28, 33, 34, 38, 39.

## Milestone 1: Safe Edit Loop

Goal: SHAMSU can generate or receive a unified diff, validate it, preview it,
ask for approval, apply it inside the workspace, roll back on failure, and
re-index changed files.

### Issue 1: Patch Apply And Rollback Behind Approval

- Owner: Dev A
- Labels: `owner:dev-a`, `area:patch`, `area:safety`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-a/patch-apply-rollback`
- Depends on: completed patch validation and Rich preview
- Summary: Implement real `PatchEngine.apply()` and `rollback()` with approval,
  backups, workspace safety, and failure restore.
- Acceptance criteria:
  - Valid diffs are validated before any write.
  - Rich preview is shown before approval.
  - `ApprovalRequest(action_type="file_edit")` is used before applying.
  - All touched paths are validated through `Sandbox`.
  - Existing files get `.bak` backups before modification.
  - Failed apply restores all backups.
  - New files can be created only inside workspace.
  - Deletes require explicit approval and are either blocked or backed up.
  - Tests cover success, denied approval, invalid diff, partial failure rollback,
    new file, delete behavior, and outside-workspace path.

### Issue 2: Re-Index Changed Files After Patch Apply

- Owner: Dev A
- Labels: `owner:dev-a`, `area:index`, `area:patch`, `type:feature`,
  `priority:p1`
- Branch: `feature/dev-a/reindex-after-patch`
- Depends on: Issue 1
- Summary: After a patch applies, update `.shamsu/index.db` for changed files.
- Acceptance criteria:
  - Modified files are re-hashed and re-parsed.
  - Deleted files are removed from `files`, `symbols`, and snippets.
  - New Python files get symbols indexed.
  - Tests prove search results reflect the post-patch state.

### Issue 3: Git Status And Diff Read-Only Tooling

- Owner: Dev A
- Labels: `owner:dev-a`, `area:patch`, `area:safety`, `type:feature`,
  `priority:p1`
- Branch: `feature/dev-a/git-awareness`
- Depends on: command runner
- Summary: Add read-only git helpers for dirty-worktree awareness.
- Acceptance criteria:
  - `git status --short` and `git diff` run through safe command execution.
  - SHAMSU warns before editing files with uncommitted changes.
  - No git write commands are introduced.
  - Tests cover clean repo, dirty file, and non-git workspace behavior.

### Issue 4: Code Edit Workflow End To End

- Owner: Dev B
- Labels: `owner:dev-b`, `area:llm`, `area:patch`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-b/code-edit-workflow`
- Depends on: Issue 1
- Summary: Build workflow: search, context pack, coder specialist, diff output,
  validate, preview, approve, apply, and report.
- Acceptance criteria:
  - Coder prompt requires unified diff only.
  - Malformed LLM diffs fail safely with a clear message.
  - Valid diffs flow through `PatchEngine`.
  - Workflow reports changed files and test suggestion.
  - Tests mock the LLM response and assert the whole flow without needing Ollama.

## Milestone 2: Real Workflows On Existing Projects

Goal: SHAMSU can answer questions, edit code, fix bugs, audit projects, generate
tests, and generate docs against an indexed workspace.

### Issue 5: Real Indexed QA As Default

- Owner: Dev B
- Labels: `owner:dev-b`, `area:llm`, `area:index`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-b/real-indexed-qa`
- Depends on: existing indexing/search
- Summary: Make natural-language QA use the real `.shamsu/index.db` whenever it
  exists, with a clear fallback when no index exists.
- Acceptance criteria:
  - QA searches real snippets and symbols.
  - Context pack includes relevant files, paths, and snippets.
  - CLI output says when no index exists and suggests `index`.
  - Tests cover indexed and unindexed workspaces.

### Issue 6: Bug Fix Workflow

- Owner: Dev B
- Labels: `owner:dev-b`, `area:llm`, `area:patch`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-b/bugfix-workflow`
- Depends on: Issues 1, 5
- Summary: Accept a traceback, failing test output, or bug report; gather targeted
  context; produce and apply a proposed diff.
- Acceptance criteria:
  - Parses `file:line` from Python tracebacks.
  - Uses nearby snippets and related symbols in context.
  - Produces a unified diff through bugfix specialist.
  - Applies only after validation, preview, and approval.
  - Tests cover traceback parsing and mocked bugfix diff flow.

### Issue 7: Audit Workflow

- Owner: Dev B
- Labels: `owner:dev-b`, `area:llm`, `area:safety`, `type:feature`,
  `priority:p1`
- Branch: `feature/dev-b/audit-workflow`
- Depends on: Issue 5
- Summary: Add an audit agent for code smells, likely bugs, missing tests, and
  common security issues.
- Acceptance criteria:
  - Uses indexed search and targeted file reads, not full-codebase prompts.
  - Produces findings with file paths, reasons, and severity.
  - Does not apply changes.
  - Tests verify output structure from mocked LLM responses.

### Issue 8: Test Generation Workflow

- Owner: Dev B
- Labels: `owner:dev-b`, `area:tests`, `area:llm`, `type:feature`,
  `priority:p1`
- Branch: `feature/dev-b/test-generation-workflow`
- Depends on: Issue 4
- Summary: Generate tests for selected files/functions/API endpoints through the
  same diff validation and patch apply path.
- Acceptance criteria:
  - Supports at least Python pytest test generation.
  - Generated test output is a unified diff.
  - Applies through `PatchEngine`.
  - Offers to run tests through `CommandRunner`.
  - Tests mock the LLM and command runner.

### Issue 9: Documentation Generation Workflow

- Owner: Dev B
- Labels: `owner:dev-b`, `area:docs`, `area:llm`, `type:feature`,
  `priority:p2`
- Branch: `feature/dev-b/doc-generation-workflow`
- Depends on: Issue 5
- Summary: Generate README/module docs from indexed project context.
- Acceptance criteria:
  - Can generate a project summary without reading private code into web search.
  - Can propose README updates as a diff.
  - Applies through approval-backed patch flow.
  - Tests cover mocked doc output.

### Issue 10: CLI Workflow Routing

- Owner: Dev C
- Labels: `owner:dev-c`, `area:cli`, `area:llm`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-c/cli-workflow-routing`
- Depends on: Issues 4, 5, 6
- Summary: Route REPL prompts to QA, code edit, bug fix, audit, test gen, doc gen,
  or PRD generation.
- Acceptance criteria:
  - `help` lists available workflow examples.
  - Natural prompt routing uses `LLMManager.route()` with safe fallback.
  - CLI never exposes arbitrary shell execution.
  - Tests cover route selection with mocked manager/workflows.

## Milestone 3: PRD To Project Planning

Goal: SHAMSU converts Markdown/TXT/PDF PRDs into a structured plan and shows it
to the user before any files are written.

### Issue 11: TXT And PDF PRD Input

- Owner: Dev C
- Labels: `owner:dev-c`, `area:prd`, `type:feature`, `priority:p1`
- Branch: `feature/dev-c/prd-txt-pdf-input`
- Depends on: existing Markdown PRD parser
- Summary: Extend PRD input support beyond Markdown.
- Acceptance criteria:
  - `.md`, `.txt`, and `.pdf` PRDs are accepted.
  - All file paths are validated through `Sandbox`.
  - PDF text extraction handles empty/unreadable PDFs with friendly errors.
  - Tests cover Markdown, TXT, PDF fallback/error paths.

### Issue 12: PRD Extractor V2 Relationships And Field Types

- Owner: Dev C
- Labels: `owner:dev-c`, `area:prd`, `area:django`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-c/prd-extractor-v2`
- Depends on: existing extractor
- Summary: Extract entities, fields, relationships, endpoints, pages, auth rules,
  and non-functional requirements from PRDs.
- Acceptance criteria:
  - Detects `ForeignKey`, `ManyToManyField`, choices, optional fields, dates,
    decimals, booleans, text fields, and auth user references.
  - Infers CRUD endpoints when endpoints are missing.
  - Infers dashboard/list/detail pages when pages are missing.
  - Tests cover Todo, Expense Tracker, and Blog PRDs.

### Issue 13: Project Plan Preview And Approval

- Owner: Dev C
- Labels: `owner:dev-c`, `area:prd`, `area:cli`, `area:safety`,
  `type:feature`, `priority:p0`
- Branch: `feature/dev-c/project-plan-preview`
- Depends on: Issue 12
- Summary: Add CLI preview for `ProjectSpec` and generated file plan before
  writing a project.
- Acceptance criteria:
  - REPL supports `plan-prd <file>`.
  - Displays project name, app name, entities, endpoints, pages, and files to
    generate.
  - Requires approval before moving into generation.
  - Tests cover preview output and denied approval.

### Issue 14: File Generation Order And Resume State

- Owner: Dev B
- Labels: `owner:dev-b`, `area:core`, `area:django`, `type:feature`,
  `priority:p1`
- Branch: `feature/dev-b/file-gen-order-resume`
- Depends on: Issue 13
- Summary: Define the ordered 22-step pipeline and persist step progress for
  resume.
- Acceptance criteria:
  - Pipeline order is deterministic.
  - State is stored inside workspace `.shamsu/`.
  - Restart can resume from the last completed step.
  - Tests cover fresh run, interrupted run, and resume.

## Milestone 4: Django Project Generation

Goal: A PRD can generate a runnable Django + DRF + DaisyUI + HTMX project.

### Issue 15: Approval-Backed Django Project Writer

- Owner: Dev C
- Labels: `owner:dev-c`, `area:django`, `area:safety`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-c/django-project-writer`
- Depends on: Issues 1, 13
- Summary: Write rendered project files into a target directory behind approval.
- Acceptance criteria:
  - Creates project/app directory structure inside workspace only.
  - Uses deterministic fixed templates where available.
  - Refuses to overwrite existing files without approval.
  - Records written files for resume/logging.
  - Tests cover new project, existing file, denied approval, path escape.

### Issue 16: Fixed Django Template Completion

- Owner: Dev C
- Labels: `owner:dev-c`, `area:django`, `type:feature`, `priority:p0`
- Branch: `feature/dev-c/fixed-django-templates`
- Depends on: existing template constants
- Summary: Complete fixed templates for `manage.py`, `settings.py`, `wsgi.py`,
  `asgi.py`, `requirements.txt`, `.env.example`, `base.html`, `login.html`,
  and `register.html`.
- Acceptance criteria:
  - Templates render with project/app/theme/nav substitutions.
  - `settings.py` includes Django, DRF, simplejwt, crispy forms, and SQLite.
  - Templates do not require Node or a build step.
  - Tests assert key imports/settings/HTML tags are present.

### Issue 17: Django Models Generator

- Owner: Dev B
- Labels: `owner:dev-b`, `area:django`, `area:llm`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-b/django-models-generator`
- Depends on: Issues 12, 14
- Summary: Generate `models.py` from entity specs.
- Acceptance criteria:
  - Uses Qwen coder specialist when available, safe mocked path in tests.
  - Validates output with `ast.parse()`.
  - Includes `__str__` methods.
  - Correctly maps extracted fields and relationships.
  - Tests cover Todo, Expense, Blog model specs.

### Issue 18: Django Serializers Generator

- Owner: Dev B
- Labels: `owner:dev-b`, `area:django`, `area:llm`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-b/django-serializers-generator`
- Depends on: Issue 17
- Summary: Generate DRF `serializers.py` using generated model content.
- Acceptance criteria:
  - Uses `ModelSerializer`.
  - Lists fields explicitly; no `fields = "__all__"`.
  - Validates serializer fields against model fields.
  - Tests cover field matching and invalid field rejection.

### Issue 19: Django Forms Generator

- Owner: Dev C
- Labels: `owner:dev-c`, `area:django`, `area:llm`, `type:feature`,
  `priority:p1`
- Branch: `feature/dev-c/django-forms-generator`
- Depends on: Issue 17
- Summary: Generate `forms.py` with one `ModelForm` per entity.
- Acceptance criteria:
  - Uses `forms.ModelForm`.
  - Lists fields explicitly.
  - Validates fields against generated models.
  - Tests cover normal fields and relationship fields.

### Issue 20: Django Views Generator

- Owner: Dev B
- Labels: `owner:dev-b`, `area:django`, `area:llm`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-b/django-views-generator`
- Depends on: Issues 17, 18, 19
- Summary: Generate DRF ViewSets and Django template views.
- Acceptance criteria:
  - Uses `ModelViewSet` for API resources.
  - Filters user-owned resources by `request.user` when applicable.
  - Uses `@login_required` for private template views.
  - Validates imports and referenced classes.
  - Tests cover public vs private page behavior at code level.

### Issue 21: Django URLs Generator

- Owner: Dev B
- Labels: `owner:dev-b`, `area:django`, `type:feature`, `priority:p0`
- Branch: `feature/dev-b/django-urls-generator`
- Depends on: Issue 20
- Summary: Generate app and project URL configuration.
- Acceptance criteria:
  - Registers DRF routers for ViewSets.
  - Adds `path()` entries for template pages.
  - Adds auth/login/logout and JWT paths where needed.
  - Validates all referenced views exist.
  - Tests cover URL name extraction for templates.

### Issue 22: Django Admin Generator

- Owner: Dev B
- Labels: `owner:dev-b`, `area:django`, `type:feature`, `priority:p1`
- Branch: `feature/dev-b/django-admin-generator`
- Depends on: Issue 17
- Summary: Generate `admin.py` for extracted models.
- Acceptance criteria:
  - Registers every generated model.
  - Uses deterministic template path where possible.
  - Validates output with `ast.parse()`.
  - Tests cover multiple model registration.

### Issue 23: Backend Consistency Checker

- Owner: Dev A
- Labels: `owner:dev-a`, `area:django`, `area:tests`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-a/backend-consistency-checker`
- Depends on: Issues 17-22
- Summary: Validate generated Python files reference matching models,
  serializers, views, forms, and URLs.
- Acceptance criteria:
  - Uses AST/symbol parsing, not LLM.
  - Finds missing serializer classes, missing model imports, bad URL view names,
    and bad form fields.
  - Produces actionable diagnostics.
  - Tests cover passing project and each failure type.

## Milestone 5: Frontend, Migrations, Tests, Error Loop

Goal: Generated projects render real pages, run migrations/tests, and can
self-correct common generated-code mistakes.

### Issue 24: Frontend Prompt And Theme Selection

- Owner: Dev C
- Labels: `owner:dev-c`, `area:django`, `area:frontend`, `type:feature`,
  `priority:p1`
- Branch: `feature/dev-c/frontend-prompt-theme`
- Depends on: Issue 12
- Summary: Add reusable DaisyUI/HTMX frontend prompt rules and simple domain
  theme selection.
- Acceptance criteria:
  - Prompt requires DaisyUI semantic classes.
  - Prompt forbids raw manual form inputs when crispy forms can render them.
  - Theme is selected by PRD/domain keywords.
  - Tests cover finance, blog, technical, and default domains.

### Issue 25: Dashboard Generator

- Owner: Dev B
- Labels: `owner:dev-b`, `area:django`, `area:frontend`, `area:llm`,
  `type:feature`, `priority:p1`
- Branch: `feature/dev-b/dashboard-generator`
- Depends on: Issues 20, 21, 24
- Summary: Generate `dashboard.html` with DaisyUI stats and recent-resource
  tables.
- Acceptance criteria:
  - Extends `base.html`.
  - Uses URL names from generated URL config.
  - Uses DaisyUI stats/table/card classes.
  - Tests verify no raw unsupported design system drift.

### Issue 26: Resource List And HTMX Partial Generators

- Owner: Dev C
- Labels: `owner:dev-c`, `area:django`, `area:frontend`, `area:llm`,
  `type:feature`, `priority:p1`
- Branch: `feature/dev-c/resource-list-htmx-generators`
- Depends on: Issues 19, 21, 24
- Summary: Generate resource list pages and `_item.html` partials.
- Acceptance criteria:
  - Uses `{{ form|crispy }}` for forms.
  - Uses HTMX add/delete interactions.
  - Uses DaisyUI table/modal/button classes.
  - Tests verify URL names and object field names are consistent.

### Issue 27: Frontend Consistency Checker

- Owner: Dev A
- Labels: `owner:dev-a`, `area:django`, `area:frontend`, `area:tests`,
  `type:feature`, `priority:p1`
- Branch: `feature/dev-a/frontend-consistency-checker`
- Depends on: Issues 25, 26
- Summary: Validate generated templates against URLs, fields, HTMX targets, and
  frontend rules.
- Acceptance criteria:
  - Every `{% url %}` name exists.
  - Every obvious model field reference exists.
  - HTMX targets match IDs in the same page or partial.
  - No raw `<input>` tags in generated forms.
  - Tests cover passing and failing templates.

### Issue 28: Migrations Runner And Dependency Installer

- Owner: Dev C
- Labels: `owner:dev-c`, `area:tools`, `area:django`, `area:safety`,
  `type:feature`, `priority:p0`
- Branch: `feature/dev-c/migrations-runner`
- Depends on: command runner, Issue 15
- Summary: Install generated requirements and run Django migrations through
  approval-backed command execution.
- Acceptance criteria:
  - `pip install -r requirements.txt` requires approval.
  - `python manage.py makemigrations` and `migrate` run in generated project cwd.
  - Output is captured and redacted.
  - Failures are structured for bug-fix workflow.
  - Tests mock command runner outputs.

### Issue 29: Django Test Runner

- Owner: Dev B
- Labels: `owner:dev-b`, `area:tests`, `area:django`, `type:feature`,
  `priority:p0`
- Branch: `feature/dev-b/django-test-runner`
- Depends on: command runner
- Summary: Run `python manage.py test --verbosity=2` and parse Django test
  results into `TestRunResult`.
- Acceptance criteria:
  - Parses `OK`, failures, errors, test names, and traceback file lines.
  - Redacts secrets from output.
  - Works independently of pytest runner.
  - Tests cover success, failure, error, and malformed output.

### Issue 30: Error Feedback Loop

- Owner: Dev B
- Labels: `owner:dev-b`, `area:llm`, `area:tests`, `area:patch`,
  `type:feature`, `priority:p0`
- Branch: `feature/dev-b/error-feedback-loop`
- Depends on: Issues 1, 6, 29
- Summary: Run tests, parse failures, generate targeted bugfix diff, apply, and
  retry up to three times.
- Acceptance criteria:
  - Stops after success or three failed iterations.
  - Each generated diff goes through validation, preview, approval, and apply.
  - Reports final pass/fail summary.
  - Tests mock two failing iterations followed by success.

### Issue 31: Django Test File Generator

- Owner: Dev B
- Labels: `owner:dev-b`, `area:tests`, `area:django`, `area:llm`,
  `type:feature`, `priority:p1`
- Branch: `feature/dev-b/django-test-file-generator`
- Depends on: Issues 20, 21
- Summary: Generate Django `TestCase`/DRF `APIClient` tests per resource.
- Acceptance criteria:
  - Covers list, create, retrieve, update, and delete for ViewSets.
  - Creates an authenticated user in setup.
  - Validates output with `ast.parse()`.
  - Tests mock model/view/url context.

## Milestone 6: Full Pipeline, CLI, Logs, Release

Goal: SHAMSU can generate, validate, run, fix, and document complete Django
projects from multiple PRDs, with clear progress and safety logs.

### Issue 32: Full PRD To Django Pipeline Orchestrator

- Owner: Dev B
- Labels: `owner:dev-b`, `area:core`, `area:django`, `type:integration`,
  `priority:p0`
- Branch: `feature/dev-b/full-django-pipeline`
- Depends on: Issues 13-31
- Summary: Wire the full ordered pipeline from PRD to generated project, checks,
  migrations, tests, error loop, and final summary.
- Acceptance criteria:
  - CLI command: `generate-prd <file> --output <dir>`.
  - Shows step progress.
  - Respects approval gates.
  - Supports resume state.
  - Integration test uses a small Todo PRD fixture.

### Issue 33: CLI Status, Log, And Progress Views

- Owner: Dev C
- Labels: `owner:dev-c`, `area:cli`, `area:safety`, `type:feature`,
  `priority:p1`
- Branch: `feature/dev-c/status-log-progress`
- Depends on: Issue 32
- Summary: Add user-facing progress/status/log commands.
- Acceptance criteria:
  - `status` shows workspace, index status, task status, current model, files,
    symbols, and pending approvals when available.
  - `log` tails structured local logs from `.shamsu/`.
  - Logs redact secrets.
  - Tests cover output formatting and missing log file behavior.

### Issue 34: Structured Audit Trail

- Owner: Dev C
- Labels: `owner:dev-c`, `area:safety`, `type:feature`, `priority:p1`
- Branch: `feature/dev-c/audit-trail`
- Depends on: Issues 1, 28, 32
- Summary: Record file writes, patch applies, command runs, approvals, denials,
  errors, and generated files in local JSONL logs.
- Acceptance criteria:
  - Logs stay inside workspace `.shamsu/`.
  - Secrets are redacted.
  - Each event has timestamp, action type, result, and affected paths.
  - Tests verify redaction and event shape.

### Issue 35: End-To-End PRD Fixtures

- Owner: Dev A
- Labels: `owner:dev-a`, `area:tests`, `area:django`, `type:integration`,
  `priority:p0`
- Branch: `feature/dev-a/e2e-prd-fixtures`
- Depends on: Issue 32
- Summary: Add Todo, Expense Tracker, and Blog PRD integration fixtures.
- Acceptance criteria:
  - Todo PRD generates a project that passes `python manage.py check`.
  - Expense PRD tests decimals and budgets.
  - Blog PRD tests public/private pages and M2M tags.
  - Tests may mock LLM output for CI stability.

### Issue 36: RAM And Performance Benchmarking

- Owner: Dev A
- Labels: `owner:dev-a`, `area:tests`, `type:hardening`, `priority:p1`
- Branch: `feature/dev-a/ram-performance-benchmark`
- Depends on: Issue 35
- Summary: Measure generation runtime and peak memory on representative PRDs.
- Acceptance criteria:
  - Adds a benchmark script or command.
  - Records peak memory per pipeline phase.
  - Target remains under 7 GB peak.
  - Writes results to `BENCHMARK.md`.

### Issue 37: Generated Project README And Summary Report

- Owner: Dev B
- Labels: `owner:dev-b`, `area:docs`, `area:django`, `area:llm`,
  `type:feature`, `priority:p2`
- Branch: `feature/dev-b/generated-project-docs`
- Depends on: Issue 32
- Summary: Generate a README and final summary for each generated project.
- Acceptance criteria:
  - README explains install, migrate, test, and run steps.
  - Summary lists generated files, commands run, failures fixed, and remaining
    warnings.
  - Tests mock doc generation and assert required sections.

### Issue 38: Release Docs And Demo Script

- Owner: Dev C
- Labels: `owner:dev-c`, `area:docs`, `type:hardening`, `priority:p1`
- Branch: `feature/dev-c/release-docs-demo`
- Depends on: Issues 32, 35
- Summary: Prepare final user docs, changelog, PRD guide, and demo script.
- Acceptance criteria:
  - README includes PRD format guide and generated-project run guide.
  - `CHANGELOG.md` includes MVP release notes.
  - Demo script covers PRD -> project -> migrate -> runserver -> browser path.
  - Known limitations are explicit.

### Issue 39: Final Safety Audit

- Owner: Dev C
- Labels: `owner:dev-c`, `area:safety`, `type:hardening`, `priority:p0`
- Branch: `feature/dev-c/final-safety-audit`
- Depends on: Issues 1, 28, 32, 34
- Summary: Verify all file, patch, command, PRD, and log paths obey sandbox and
  approval rules.
- Acceptance criteria:
  - Path traversal blocked across PRD parse, project write, patch apply, logs,
    and command cwd.
  - Dangerous commands remain blocked.
  - Secrets are redacted from command output and logs.
  - User-facing workflows do not expose arbitrary command execution.
  - Tests cover all safety gates.

### Issue 40: MVP Release Cut

- Owner: Dev A
- Labels: `owner:dev-a`, `type:hardening`, `priority:p0`
- Branch: `release/mvp`
- Depends on: Issues 1-39
- Summary: Final merge, clean install verification, tag release.
- Acceptance criteria:
  - Fresh clone/install passes.
  - Full test suite and Ruff pass.
  - Todo, Expense, and Blog demo runs are documented.
  - Version is updated if needed.
  - Git tag is created after all checks pass.

## Parallel Execution Order

Sprint 1:

- Dev A: Issue 1, then Issue 2.
- Dev B: Issue 5, then Issue 4 once Issue 1 lands.
- Dev C: Issue 13, then Issue 10.

Sprint 2:

- Dev A: Issue 3 and Issue 23.
- Dev B: Issues 6, 7, 8.
- Dev C: Issues 11, 12, 15, 16.

Sprint 3:

- Dev A: Issue 27.
- Dev B: Issues 17, 18, 20, 21, 22.
- Dev C: Issues 19, 24, 26, 28.

Sprint 4:

- Dev A: Issues 35, 36.
- Dev B: Issues 29, 30, 31, 32, 37.
- Dev C: Issues 33, 34, 38, 39.

Release:

- Dev A leads Issue 40.
- Dev B records/verifies workflow demos.
- Dev C finishes user docs and safety notes.

## MVP Done Criteria

- `pytest tests/ -q` passes.
- `ruff check shamsu tests` passes.
- PRD -> generated Django project works for Todo, Expense Tracker, and Blog
  fixtures.
- Generated projects pass `python manage.py check`.
- Migrations run successfully for clean PRDs.
- Main generated pages render with DaisyUI and HTMX.
- At least one code-edit loop works end to end:
  search -> context -> diff -> validate -> preview -> approve -> apply -> test.
- Error feedback loop fixes at least one intentionally broken generated project.
- Peak RAM is measured and stays under the project target of 7 GB.
- Safety audit passes for path traversal, blocked commands, approval gates, and
  secret redaction.
