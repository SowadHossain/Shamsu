# SHAMSU

Local-first autonomous coding agent for low-resource machines.

SHAMSU is being built as a lightweight coding teammate that can inspect,
index, search, explain, parse PRDs, and eventually edit, fix, test, document,
and generate software projects without depending on expensive cloud AI APIs.

The core rule:

> Use deterministic tools to find the right context, then use a small local
> model to reason over that context.

SHAMSU should not dump a whole codebase into an LLM prompt. It indexes and
retrieves relevant files first, then builds a compact context pack.

## Current Capability


Working now:

- CLI REPL through `shamsu`
- Workspace-scoped indexing into `.shamsu/index.db`
- SQLite FTS5 snippet search
- Python symbol extraction with `ast`
- Stale index cleanup when files move or disappear
- Markdown PRD parsing
- Rule-based PRD entity extraction
- `ProjectSpec` assembly from PRDs
- Fixed Django template constants and renderer
- QA context preview using real indexed search when an index exists
- Workspace path sandbox for file inputs such as `parse-prd`
- Command risk classifier and secret redaction helpers
- Internal command runner with workspace checks, blocked-command rejection,
  approval gates, timeouts, captured output, and redaction
- Internal patch validation and Rich diff preview for unified diffs
- Agent progress tracking in `agent context/PROGRESS.md`

Planned next:

- Patch application and rollback behind approval
- Writing generated Django files behind approval
- Full PRD-to-Django project generation
- Local Ollama-backed specialist responses beyond preview mode

## Requirements

- Python 3.11 or newer
- PowerShell on Windows, or Bash on Linux/macOS
- Optional: Ollama for local model calls

The current CLI works without Ollama. If Ollama is not running, SHAMSU falls
back to a safe QA preview instead of crashing.

## Safe Install

The recommended install uses a repo-local virtual environment:

- Creates `.venv/` inside this repository
- Installs SHAMSU into that `.venv`
- Does not install packages into global Python
- Does not edit PATH, shell profiles, registry, or system files

This is dependency isolation plus SHAMSU's workspace sandbox. It is not a full
Docker or OS-level sandbox.

### Windows PowerShell

From the SHAMSU repo root:

```powershell
.\scripts\install.ps1
```

If PowerShell blocks script execution on your machine, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

### Linux/macOS Bash

From the SHAMSU repo root:

```bash
bash scripts/install.sh
```

If your Python command is not `python3`, choose one explicitly:

```bash
PYTHON=python3.11 bash scripts/install.sh
```

## Run SHAMSU Safely

SHAMSU treats the selected workspace as the project boundary. Indexes and
local state are written under that workspace's `.shamsu/` folder.

### Run From The Current Folder

Go to the project folder you want SHAMSU to inspect, then run the repo script.

Windows:

```powershell
cd F:\Work\some-project
& "F:\Work\PROJECTS\shamsu\Shamsu\scripts\run-shamsu.ps1"
```

Linux/macOS:

```bash
cd /path/to/some-project
/path/to/Shamsu/scripts/run-shamsu.sh
```

### Run With An Explicit Workspace

Windows:

```powershell
& .\scripts\run-shamsu.ps1 -Workspace "F:\Work\some-project"
```

Direct Python:

```powershell
.\.venv\Scripts\python.exe -m shamsu.cli.repl --workspace "F:\Work\some-project"
```

Bash:

```bash
SHAMSU_WORKSPACE=/path/to/some-project scripts/run-shamsu.sh
```

## CLI Commands

Start the REPL:

```powershell
.\scripts\run-shamsu.ps1
```

Inside the REPL:

```text
index
status
search <query>
symbols <name>
parse-prd <file.md>
help
exit
```

### `index`

Indexes the selected workspace.

```text
shamsu> index
```

Creates or updates:

```text
.shamsu/index.db
```

The index includes file metadata, Python symbols, and searchable text snippets.

### `status`

Shows index counts.

```text
shamsu> status
Files: 53
Symbols: 313
Snippets: 181
```

### `search <query>`

Searches indexed snippets with SQLite FTS5.

```text
shamsu> search authentication flow
```

### `symbols <name>`

Looks up indexed symbols.

```text
shamsu> symbols build_project_spec
```

### `parse-prd <file.md>`

Parses a Markdown PRD inside the workspace.

```text
shamsu> parse-prd "agent context/SHAMSU_10day_dev_plan.md"
```

Paths outside the workspace are rejected.

### Natural-Language Request

Any other text builds a routed QA context preview:

```text
shamsu> how does project spec work?
```

If an index exists, SHAMSU uses real indexed search to assemble the preview. If
Ollama is unavailable, the routing step falls back to safe QA mode.

## Smoke Test

From the SHAMSU repo root after install:

```powershell
@'
index
status
search EntitySpec
symbols build_project_spec
parse-prd "agent context/SHAMSU_10day_dev_plan.md"
exit
'@ | .\.venv\Scripts\python.exe -m shamsu.cli.repl --workspace .
```

## Verify Development Setup

Run tests:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

Run lint:

```powershell
.\.venv\Scripts\python.exe -m ruff check shamsu tests
```

Expected current result:

```text
60 passed
All checks passed!
```

On Bash:

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check shamsu tests
```

## Safety Model

SHAMSU currently has two safety layers.

Dependency isolation:

- Install scripts use only `.venv/`.
- No global `pip install`.
- No PATH or shell profile edits.

Workspace sandbox:

- The CLI resolves one workspace at startup.
- `parse-prd` validates file paths with `Sandbox.validate()`.
- Paths outside the workspace are rejected.
- Index data stays inside `<workspace>/.shamsu/`.

Internal command execution:

- `CommandRunner` validates the requested working directory inside the
  workspace before running anything.
- Blocked commands are rejected without approval or execution.
- Medium-risk and unknown commands require approval.
- Captured command output is redacted before it is returned.
- This runner is available internally for future workflows such as tests and
  patch validation. It is not exposed as a general REPL command yet.

Internal patch review:

- `PatchEngine` validates unified diff structure before any apply workflow can
  use it.
- Patch paths are normalized and checked against the workspace sandbox.
- `patch.preview` renders a Rich diff summary and colorized diff body.
- Patch application and rollback are intentionally not enabled yet.

Important limitation:

- This is not a full OS sandbox.
- This is not Docker isolation.
- Future file writes, patch application, rollback, and user-facing command
  execution still need approval gates before they become public workflows.

## Troubleshooting

### Python Not Found

Check your Python version:

```powershell
python --version
```

Use Python 3.11 or newer. On Bash, try:

```bash
python3 --version
```

### PowerShell Blocks Scripts

Use a one-time bypass for this command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

This does not permanently change your execution policy.

### Reinstall Dependencies

Remove the local venv and reinstall.

Windows:

```powershell
Remove-Item -Recurse -Force .\.venv
.\scripts\install.ps1
```

Bash:

```bash
rm -rf .venv
bash scripts/install.sh
```

### Rebuild The Index

Inside the REPL:

```text
shamsu> index
```

Or delete the workspace index and re-run `index`:

```powershell
Remove-Item .\.shamsu\index.db*
```

Only do this inside the workspace you intend to re-index.

## Project Layout

```text
shamsu/            Python package
scripts/           Install and run wrappers
tests/             Test suite
agent context/     Planning docs, agent context, and progress tracker
.shamsu/           Local SHAMSU state for this repo workspace
```

Key agent docs:

- `agent context/AGENTS.md`
- `agent context/PROGRESS.md`
- `agent context/REQUIREMENTS.md`
- `agent context/SHAMSU_10day_dev_plan.md`
- `agent context/SHAMSU_week2_milestone_v2.md`

## Contributor Notes

- Keep `shamsu/types.py` and `shamsu/interfaces.py` stable unless the team
  explicitly agrees to change the shared contract.
- Prefer deterministic tooling before LLM calls.
- Keep memory use low; avoid loading full projects into memory.
- Add tests with each feature slice.
- Run tests and lint before handoff.
- Update `agent context/PROGRESS.md` whenever a feature slice is completed or
  the next task changes.
