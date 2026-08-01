# SHAMSU

Local-first autonomous coding agent for low-resource machines.

SHAMSU is a lightweight coding teammate that can inspect, index, search,
explain, edit, fix, test, document, research, and generate software projects
without depending on cloud AI APIs for inference.

The core rule:

> Use deterministic tools to find the right context, then use a small local
> model to reason over that context.

SHAMSU should not dump a whole codebase into an LLM prompt. It indexes and
retrieves relevant files first, then builds a compact context pack.

## Current Capability

Working now:

- CLI REPL through `shamsu`
- General local chat for non-project prompts
- Automatic permission-gated web lookup for external/current questions
- Automatic browser inspection for local app preview/debug prompts
- Workspace-scoped indexing into `.shamsu/index.db`
- SQLite FTS5 snippet search
- Python symbol extraction with `ast`
- Stale index cleanup when files move or disappear
- Markdown PRD parsing
- Markdown, TXT, and PDF PRD file input
- Rule-based PRD entity extraction
- `ProjectSpec` assembly from PRDs
- PRD plan preview/approval and generation resume state
- Deterministic Django fixed templates and backend generators
- Approval-backed Django project writer and backend consistency checker
- QA, code-edit, bug-fix, audit, test-generation, and documentation workflows
- Claude-like prompt loop with local model routing and keyword fallback
- Workspace-local sessions, resume, redacted event logs, and export bundles
- Canonical per-prompt runs with decisions, tool/model calls, contexts,
  mutations, diagnostics, verification, and final output
- Noninteractive `shamsu run` harness with deterministic approvals, timeout,
  dry-run, JSON output, and artifact validation
- Workspace path sandbox for file inputs such as `parse-prd`
- Command risk classifier and secret redaction helpers
- Internal command runner with workspace checks, blocked-command rejection,
  approval gates, timeouts, captured output, and redaction
- Internal patch validation and Rich diff preview for unified diffs
- Approval-backed patch apply/rollback with post-patch re-indexing
- Agent progress tracking in `agent context/PROGRESS.md`
- Full PRD-to-Django generation with frontend templates, setup, migrations,
  tests, repair loop, acceptance checks, and local browser inspection
- First-run capability report plus read-only runtime/index/memory/web/browser
  diagnostics

Beta boundaries: generated Django projects target local SQLite development;
model quality varies by tier; and the workspace sandbox is not an OS sandbox.

## Requirements

- Python 3.11 or newer
- PowerShell on Windows, or Bash on Linux/macOS
- Ollama for local model calls. The installer can bootstrap it for you.

Runtime inference is local-only through Ollama on `localhost:11434`. SHAMSU does
not configure cloud AI APIs.

## Safe Install

The recommended install uses a repo-local virtual environment:

- Creates `.venv/` inside this repository
- Installs SHAMSU into that `.venv`
- Installs a user-local `shamsu` launcher
- Adds the launcher directory to your user PATH on Windows, unless skipped
- Does not install packages into global Python
- Does not edit shell profiles, registry, or system files

This is dependency isolation plus SHAMSU's workspace sandbox. It is not a full
Docker or OS-level sandbox.

### Windows PowerShell

From the SHAMSU repo root:

```powershell
.\scripts\install.ps1 -Yes
```

If PowerShell blocks script execution on your machine, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
```

### Linux/macOS Bash

From the SHAMSU repo root:

```bash
bash scripts/install.sh --yes
```

If your Python command is not `python3`, choose one explicitly:

```bash
PYTHON=python3.11 bash scripts/install.sh
```

Installer flags:

```text
-Yes / --yes                         approve runtime bootstrap prompts
-SkipOllamaInstall / --skip-ollama-install
-SkipModels / --skip-models
-PrefetchModels / --prefetch-models   download all required models now instead of on first use
-SkipCommandInstall / --skip-command-install
-SkipPathUpdate
-BinDir <path> / --bin-dir <path>
-ModelsPath <path> / --models-path <path>
```

By default, install does **not** download model weights. On the first
interactive run in a workspace, SHAMSU asks for a model tier and downloads
that tier with visible progress. Pass `-PrefetchModels`/`--prefetch-models`
to fetch the default tier during installation instead.

The installer may download Ollama and model weights when approved. SHAMSU itself
does not edit your PowerShell profile, registry, shell startup files, or global
Python. On Windows, it can add only the SHAMSU launcher directory to your user
PATH and records that in `$HOME\.shamsu\path.json`. If Ollama's official
installer makes normal app/service entries, that is Ollama's installer behavior,
not extra SHAMSU configuration.

The installer also writes user-local launcher files:

- Windows default: `$HOME\.shamsu\bin\shamsu.ps1` and `shamsu.cmd`
- Linux/macOS default: `$HOME/.local/bin/shamsu`

Windows install prepends `$HOME\.shamsu\bin` to your user PATH so `shamsu`
wins over any stale global Python script. Uninstall removes that PATH entry only
when the SHAMSU manifest says SHAMSU added it. If the directory was already on
PATH before install, uninstall leaves it alone. Bash install still prints the
exact direct command and PATH note instead of editing shell startup files.

The Windows installer broadcasts an environment refresh after updating user
PATH, but already-open terminal tabs can still have stale PATH values. If a
current terminal does not recognize `shamsu`, open a new terminal or run:

```cmd
set "PATH=%USERPROFILE%\.shamsu\bin;%PATH%"
```

## Run SHAMSU Safely

SHAMSU treats the selected workspace as the project boundary. Indexes and
local state are written under that workspace's `.shamsu/` folder.

### Run From Any Repo With `shamsu`

After installing the user command, go to the project you want SHAMSU to inspect:

```powershell
cd F:\Work\my-project
shamsu
```

That project folder becomes the default workspace sandbox. Workspace state is
written under:

```text
F:\Work\my-project\.shamsu\
```

All arguments are forwarded:

```powershell
shamsu --new-session "my repo"
shamsu --session 20260702
shamsu --workspace .
```

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

### Run One Prompt Noninteractively

The headless path uses the same dispatcher and writes the same run evidence:

```powershell
python -m shamsu.cli.repl run --workspace . --prompt "fix the failing test" --approval deny --output json
python -m shamsu.cli.repl run --workspace . --prompt "show the planned edits" --dry-run --output json
```

Approval modes are `deny`, `allow`, and `prompt`. Headless mode defaults to
deny, has a bounded timeout, and returns a nonzero exit code for failed,
denied, cancelled, or timed-out requests.

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
plan-prd <file.md|file.txt|file.pdf>
generate-django <file.md|file.txt|file.pdf>
models status
models pull
models repair
/web search <query>
/web open <url>
/browse open <url>
/browse read
/browse click <selector>
/browse type <selector> <text>
/browse screenshot
/doctor
/runs
/run show [run-id]
/run timeline|decisions|tools|commands|context|diff|validate [run-id]
sessions list
sessions current
sessions show <id>
sessions resume <id-or-title>
sessions rename <id> <title>
sessions close [id]
sessions export <id>
log tail
help
exit
```

Natural chat also works when you are not asking about the current workspace:

```text
shamsu> what is recursion?
shamsu> write a short status update for my team
shamsu> brainstorm names for a budgeting app
```

Natural prompts can also use the stateful ReAct agent loop. SHAMSU keeps
ordered chat/tool state, exposes safe local tools to Ollama, and only reports
file writes or command results after a tool confirms them:

```text
shamsu> create hello.py with a small hello world script
shamsu> run the tests
shamsu> what did you just create?
```

SHAMSU answers basic workspace questions with local tools before calling a
model:

```text
shamsu> what folder are you in?
shamsu> what files do I have here?
shamsu> what PRD files are in this repo?
```

Attach files or folders to a prompt with `@`:

```text
shamsu> summarize @README.md
shamsu> explain how @shamsu/cli/repl.py handles sessions
shamsu> update the docs using @"agent context/PROGRESS.md"
```

`@` paths are validated against the active workspace sandbox. SHAMSU will not
read files outside that workspace.

SHAMSU can also decide on its own when to ask for web or browser access:

```text
shamsu> look up the latest Django auth docs
shamsu> check the app and verify the dashboard
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

### `parse-prd <file>`

Parses a Markdown, TXT, or PDF PRD inside the workspace.

```text
shamsu> parse-prd "agent context/SHAMSU_10day_dev_plan.md"
```

Paths outside the workspace are rejected.

### `plan-prd <file>`

Parses a PRD, builds a `ProjectSpec`, prints a Rich preview, and asks approval
before recording the plan state.

```text
shamsu> plan-prd TODO_PRD.md
```

### `generate-django <file>`

Parses and previews a PRD, asks approval, writes deterministic Django backend
files inside the workspace, updates generation state, and runs static backend
consistency checks.

```text
shamsu> generate-django TODO_PRD.md
```

### Sessions And Logs

SHAMSU creates or resumes a workspace-local session on startup. Session data
lives under:

```text
.shamsu/sessions/
```

Start a named session:

```powershell
.\scripts\run-shamsu.ps1 --new-session "Todo PRD run"
```

Resume one later:

```powershell
.\scripts\run-shamsu.ps1 --session 20260702
```

Inside the REPL:

```text
sessions list
sessions current
sessions resume <id-or-title>
sessions export <id>
log tail
```

Exports are redacted ZIP bundles containing `session.json`, `events.jsonl`, and
a Markdown summary.

Every prompt also has a canonical run under `.shamsu/runs/<run-id>/`.
`/run show` gives the concise prompt-to-outcome report: structured decisions,
tool outcomes, changed files, verification, and final response. Detailed
subcommands expose the timeline and individual artifact groups.

SHAMSU does not persist private raw chain-of-thought. It records concise
reason summaries, evidence, alternatives, chosen actions, outcomes, tool/model
metadata, and redacted context previews. This is enough to debug how a task
was tackled without turning hidden reasoning or secrets into durable logs.

### Natural-Language Request

Any other text routes into the local assistant:

```text
shamsu> how does project spec work?
```

If an index exists, SHAMSU uses real indexed search for project-aware answers.
If no index exists, SHAMSU still handles general local chat through the
stateful local agent loop and only asks for `index` when the prompt is clearly
workspace-specific.

Slash commands are always routed locally. Unknown slash commands such as
`/inde` are rejected locally with suggestions and are never sent to the LLM.

### `models status|pull|repair`

Checks and repairs the local AI runtime.

```text
shamsu> models status
shamsu> models pull
shamsu> models repair
```

`models repair` starts local Ollama when possible and pulls missing required
models. If a workflow hits a local-runtime failure, SHAMSU can kick off this
guided repair flow from inside the chat instead of only surfacing the raw
error.

### Model Tiers

| Tier | Thinking/text model | Coding model | Intended host | Measured limitation |
|---|---|---|---|---|
| `light` | `qwen2.5:3b-instruct` | `qwen2.5-coder:3b-instruct` | 8 GB RAM / CPU-first | Faster, but the current 12-case eval is 9/12; clarification and rename/destructive-ambiguity cases remain weak. |
| `default` | `deepseek-r1:7b` | `qwen2.5-coder:7b-instruct` | 8 GB+ with one model active at a time | Current 12-case eval is 11/12 with two stochastic cases; model swaps can dominate task time. |
| `heavy` | `mistral-nemo:12b` | `qwen2.5-coder:14b` | 16 GB+ | Higher memory and download cost; not yet represented by a published three-sample benchmark. |

Choose with `/models tier light|default|heavy` or `SHAMSU_MODEL_TIER`. See
`BENCHMARK.md`, `BENCHMARK-light.md`, and `RELEASE_VALIDATION.md` for measured
results. A single stochastic model run is not a release-quality measurement.

### Local State And Retention

```text
.shamsu/
  state.json                 workspace state schema marker
  first-run-report.json      one-time six-capability readiness report
  model_tier.json            selected local model tier
  index.db                   local code/search index
  permissions.json           remembered low-risk write/edit approvals
  sessions/                  resumable conversation metadata and events
  runs/<run-id>/             canonical prompt evidence and final output
  abstract/                  code-memory health and generation metadata
  memory/                    local Graphiti/SQLite memory state
  web/ and web_cache.db      optional web provider config and cache
  browser/                   local screenshots
```

Run logs default to 30-day retention, configured in
`.shamsu/action_ledger/config.json`; `/run clean` previews stale runs and requires
approval before deleting them. Session history, indexes, memory, browser
screenshots, and web cache are not removed by run retention. Workspace state
schema upgrades are idempotent and never rewrite historical run evidence.

### `/web ...` and automatic web lookup

SHAMSU stays local-first, but if a prompt clearly needs external or current
information it can ask permission to search the web automatically.

For underspecified current-information prompts, SHAMSU asks for missing details
first. For example, weather requests need a location before web lookup.

Optional explicit commands:

```text
shamsu> what is the weather in Dhaka today?
shamsu> /web search latest Django auth docs
shamsu> /web open https://docs.djangoproject.com/
```

Web lookups require approval and are logged as redacted session events.

### `/browse ...` and browser debugging

SHAMSU can use a local Playwright browser session for preview and debugging
flows, especially when a prompt is about checking a running app or rendered UI.

Optional explicit commands:

```text
shamsu> /browse open http://127.0.0.1:8000
shamsu> /browse read
shamsu> /browse click text=Login
shamsu> /browse screenshot
```

Browser actions require approval before opening the session and before
state-changing actions like click/type.

## External MCP Servers

SHAMSU can connect to standard external MCP servers and expose their tools to
the normal coding-agent loop. It uses the official MCP Python SDK and supports:

- local `stdio` servers launched as child processes
- remote Streamable HTTP servers
- legacy remote SSE servers
- static headers whose secret values come from environment variables
- OAuth 2.1 for remote HTTP servers, with tokens stored in the OS keyring
- Claude-compatible project configuration in `.mcp.json`

Create `.mcp.json` in the workspace. A stdio server uses the same `mcpServers`
shape as Claude Desktop and Claude Code:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "F:\\Work\\my-project"
      ]
    }
  }
}
```

A remote server with a token can reference an environment variable. Do not put
the token itself in the JSON file:

```json
{
  "mcpServers": {
    "company": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${COMPANY_MCP_TOKEN}"
      }
    }
  }
}
```

For a remote server that implements MCP OAuth discovery and dynamic client
registration, use `"auth": "oauth"`. SHAMSU opens the authorization page in
the browser on first connection and stores access, refresh, and client
credentials in the operating system keyring:

```json
{
  "mcpServers": {
    "remote": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "auth": "oauth",
      "oauth_scopes": "read write"
    }
  }
}
```

Configuration is merged in this order, with later files overriding earlier
ones: `~/.shamsu/mcp.json`, `<workspace>/.shamsu/mcp.json`, then
`<workspace>/.mcp.json`. Use these REPL commands to inspect it:

```text
/mcp status
/mcp tools [server]
/mcp config
/mcp reload
/mcp auth logout <server>
```

External calls ask for approval by default. Server settings support
`"approval": "always"`, `"writes"`, or `"never"`, plus per-tool
`tool_permissions` values of `allow`, `ask`, or `deny`. MCP annotations are
untrusted hints, so SHAMSU does not use a server's `readOnlyHint` to bypass a
read-only request unless `"trust_tool_annotations": true` is explicitly set.
For stricter configuration, list reviewed tool names in `read_only_tools`.

Discovered tools are named `mcp__<server>__<tool>` in model calls and run logs.
Arguments, approvals, results, errors, and structured content pass through the
existing session and ActionLedger logging; common credential fields are
redacted by key. Direct MCP resource and prompt browsing is not exposed yet;
the current integration covers MCP tools.

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
152 passed
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
- No shell profile edits.
- Windows user PATH changes are limited to the SHAMSU launcher directory and
  tracked in `$HOME\.shamsu\path.json` for safe uninstall.

Workspace sandbox:

- The CLI resolves one workspace at startup.
- `parse-prd` validates file paths with `Sandbox.validate()`.
- Paths outside the workspace are rejected.
- Index data stays inside `<workspace>/.shamsu/`.
- Session logs and exports stay inside `<workspace>/.shamsu/sessions/`.

Local AI runtime:

- SHAMSU only allows LLM calls to `localhost`, `127.0.0.1`, or `::1`.
- Natural agent chat uses Ollama's local Python SDK and native tool-calling
  API.
- Runtime status is stored in repo-local `.shamsu/runtime.json`.
- Required model checks use Ollama's local CLI and local HTTP API.
- Setup-time downloads require installer approval or `-Yes`/`--yes`.
- Runtime inference does not call cloud AI endpoints.
- The default v2.2 model map uses two 8GB-friendly anchors: `deepseek-r1:7b`
  for routing/chat/planning/review/docs and `qwen2.5-coder:7b-instruct`
  for code, tests, and bug-fix work. Set `SHAMSU_SINGLE_MODEL_MODE=1` to
  route every role through `deepseek-r1:7b` for zero-swap measurement.

Internal command execution:

- `CommandRunner` validates the requested working directory inside the
  workspace before running anything.
- Blocked commands are rejected without approval or execution.
- Medium-risk and unknown commands require approval.
- Captured command output is redacted before it is returned.
- This runner is available internally for future workflows such as tests and
  patch validation. It is not exposed as a general REPL command yet.

Stateful agent loop:

- `ChatState` appends `system`, `user`, `assistant`, and `tool` messages in
  order and stores them in the active workspace session log.
- The ReAct loop exposes `list_files`, `read_file`, `write_file`,
  `run_command`, and `search_index` to Ollama.
- File writes and commands still go through workspace sandboxing and approval
  gates.
- If a small model returns a markdown code block instead of a native tool call,
  SHAMSU can save it through the same approved `write_file` path when the
  target file is unambiguous.

Internal patch review:

- `PatchEngine` validates unified diff structure before any apply workflow can
  use it.
- Patch paths are normalized and checked against the workspace sandbox.
- `patch.preview` renders a Rich diff summary and colorized diff body.
- Patch application requires approval, writes backups, rolls back failed
  applies, and refreshes the workspace index after success.

Session logging:

- Every request writes one human-readable
  `.shamsu/runs/<run-id>/report.md`; conversation roll-ups use
  `.shamsu/sessions/<session-id>/report.md`.
- `essential` is the default log mode. Its report contains the prompt,
  approach, tools, changed files, verification, errors, and final answer while
  omitting per-model raw prompt, reasoning, response, and context files.
- `verbose` expands that same report with model exchanges, emitted reasoning
  traces, context, tool payloads, command output, and decisions. Full redacted
  machine evidence is retained under the run's hidden `.evidence/` folder.
- Use `/logs mode essential` or `/logs mode verbose`; `SHAMSU_LOG_LEVEL` can
  override the mode for one process. `/logs` shows the active mode and paths.
- Log payloads are redacted, and large inline strings are truncated.
- Exports are meant to be shareable debugging bundles, not raw source dumps.

Important limitation:

- This is not a full OS sandbox.
- This is not Docker isolation.
- User-facing arbitrary shell execution is still not exposed as a REPL command.
- Reports and evidence are debugging aids, not a forensic or compliance audit
  system.

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

### Something Feels Broken

Editing `shamsu` source code never requires a reinstall — install uses an
editable Python install, so code changes take effect the next time you run
`shamsu`. Before reinstalling anything, run the read-only diagnostic first:

Windows:

```powershell
.\scripts\doctor.ps1
```

Bash:

```bash
bash scripts/doctor.sh
```

`doctor` checks the editable install, runtime and model cookbook, PATH setup,
index and memory health, web fallback, Playwright Chromium, state schema,
run integrity, and stray/ancestor `.shamsu` workspaces. It only reports; it
never repairs. The first interactive launch writes the six core capability
results to `.shamsu/first-run-report.json`.

### Reinstall Dependencies (last resort)

Only do this if `doctor` reports the virtual environment itself is
corrupted. This re-downloads dependencies and is slower than a normal run.

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

### Uninstall SHAMSU

Remove SHAMSU-managed files from this repo install.

Windows:

```powershell
.\scripts\uninstall.ps1
```

Bash:

```bash
bash scripts/uninstall.sh
```

This removes the repo `.venv`, the repo `.shamsu` runtime/config state, and
the user-local SHAMSU launcher. It does not remove Ollama or `.shamsu` folders
inside your other project workspaces.

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
