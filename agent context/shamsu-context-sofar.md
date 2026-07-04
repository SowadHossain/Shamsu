# SHAMSU Context So Far

Last updated: 2026-07-04

This file is a compact architecture and handoff map for SHAMSU as it exists
now. It explains what the system does, which tools it uses, how context is
engineered, and where future agents should look before changing behavior.

## One-Line Product Shape

SHAMSU is a local-first coding agent CLI. It runs from any project folder,
treats that folder as the workspace sandbox, stores local state in
`<workspace>/.shamsu/`, uses local Ollama models only, and combines
deterministic tools with small specialist LLM calls.

Core rule:

```text
Use deterministic tools to find and validate context first.
Use local models only after the right context is selected.
Write files only through sandboxed, approval-backed paths.
```

## What SHAMSU Can Do Now

Current working capabilities:

- Install into a repo-local `.venv`.
- Install a user-local `shamsu` launcher so the CLI can be started from any
  project folder.
- Start/resume workspace-local sessions.
- Log prompts, routing decisions, tool use, approvals, LLM calls, context
  packs, generated files, commands, patches, web, browser, and workflow
  results.
- Answer normal chat through a stateful local Ollama ReAct loop.
- List/read workspace files deterministically.
- Resolve `@file`, `@folder`, and quoted path mentions.
- Index the workspace into SQLite FTS5.
- Search code snippets and Python symbols.
- Build compact context packs for specialists.
- Route natural prompts to QA, code edit, bug fix, audit, docs, test
  generation, PRD planning, web lookup, browser inspection, Django generation,
  or general chat.
- Use web search/fetch after approval for current/external information.
- Use Playwright browser tools after approval for local app preview/debugging.
- Parse Markdown, TXT, and PDF PRDs.
- Extract rule-based project entities, endpoints, pages, and DaisyUI theme.
- Preview and approve PRD project plans.
- Generate deterministic Django projects from PRDs.
- Generate backend files, frontend templates, tests, README, and summary docs.
- Run generated Django setup/test commands through the safe command runner.
- Feed generated-project failures into a bug-fix loop.
- Use opt-in long-running/autonomy mode for deeper loops.
- Run local diagnostics with `/doctor` or `scripts/doctor.*`.
- Pull missing Ollama models lazily when the first workflow needs them.
- Ask approval with a menu and optionally remember safe file-write/file-edit
  decisions.

## Install And Runtime Model

Important files:

- `scripts/install.ps1`
- `scripts/install.sh`
- `scripts/run-shamsu.ps1`
- `scripts/run-shamsu.sh`
- `scripts/uninstall.ps1`
- `scripts/uninstall.sh`
- `scripts/doctor.ps1`
- `scripts/doctor.sh`
- `shamsu/runtime/ollama.py`
- `shamsu/runtime/models.py`
- `shamsu/runtime/doctor.py`

Install design:

- Python dependencies live in the repo `.venv`.
- SHAMSU is installed editable, so source edits take effect next run.
- Windows install creates user-local launchers in `$HOME\.shamsu\bin`.
- Bash install creates a user-local launcher in `$HOME/.local/bin`.
- Windows install can add only the SHAMSU launcher directory to user PATH and
  records this in `$HOME\.shamsu\path.json`.
- Uninstall removes only SHAMSU-managed launchers/PATH entries/repo state.
- SHAMSU does not install into global Python.
- SHAMSU does not edit PowerShell profile, shell startup files, or registry.

Runtime design:

- Ollama is the local model runtime.
- Inference is local-only: `localhost`, `127.0.0.1`, and `::1`.
- Non-local LLM URLs are rejected in `LLMManager` and `AgentChatLoop`.
- Install can bootstrap Ollama/model downloads with approval.
- Normal runtime lazily pulls missing models on first use.
- Pull output is decoded with UTF-8 replacement to avoid Windows
  `UnicodeDecodeError`.
- Subprocesses use Windows no-window flags where needed to avoid flashing
  hidden terminals.

Default v2.2 model map from `shamsu/runtime/models.py`:

- `qwen3:8b`: router, QA/chat, planner, classifier, review, docs, summarizer.
- `qwen2.5-coder:7b-instruct`: coder, frontend/backend generation, tests,
  bug fix, and repair loops.

`SHAMSU_SINGLE_MODEL_MODE=1` routes every role through `qwen3:8b` for
zero-swap measurement. The runtime cookbook refuses pulls outside these
8GB-friendly anchors.

## Workspace State Layout

Every project where SHAMSU is run gets its own state folder:

```text
<workspace>/.shamsu/
  index.db
  generation-state.json
  permissions.json
  autonomy.json
  tasks/
  sessions/
```

Session layout:

```text
<workspace>/.shamsu/sessions/
  index.json
  <session-id>/
    session.json
    events.jsonl
    context/
    exports/
```

This is intentionally similar to other coding-agent CLIs: project-specific
memory lives with the project, not in SHAMSU's source repo.

## CLI Architecture

Primary file:

- `shamsu/cli/repl.py`

Supporting file:

- `shamsu/cli/command_router.py`

CLI flow:

1. Parse `--workspace`, `--session`, and `--new-session`.
2. Resolve the workspace once at startup.
3. Create or resume a workspace-local session.
4. Print startup banner with workspace, runtime, model, and autonomy status.
5. Build prompt-toolkit input with slash-command and `@file` autocomplete.
6. For every input:
   - normalize input and strip odd BOM characters,
   - route slash commands locally first,
   - log the prompt,
   - run `AgentOrchestrator` for deterministic context/file handling,
   - route to web/browser if clearly needed,
   - route to PRD/Django/workflow commands when matched,
   - otherwise use indexed QA, specialist workflow, or ReAct general chat.

Slash-command rule:

- Any input starting with `/` is handled by `CommandRouter`.
- Unknown commands never go to the LLM.
- Typos such as `/inde` suggest close commands like `/index`.

Common commands:

- `/help`
- `/index`
- `/status`
- `/search <query>`
- `/symbols <name>`
- `/parse-prd <file>`
- `/plan-prd <file>`
- `/generate-django <file>`
- `/generate-prd <file> --output <dir>`
- `/models status|pull|repair`
- `/doctor`
- `/permissions list|clear`
- `/autonomy status|on|off`
- `/tasks list|show <id>`
- `/web search <query>`
- `/web open <url>`
- `/browse open|read|click|type|screenshot`
- `/sessions list|current|show|resume|rename|close|export`
- `/log tail`

Note: the REPL also accepts many commands without `/` for older behavior, but
the desired system-command UX is slash-prefixed.

## Main Agent Layers

Important files:

- `shamsu/agents/orchestrator.py`
- `shamsu/agents/chat_loop.py`
- `shamsu/agents/chat_state.py`
- `shamsu/agents/markdown_fallback.py`
- `shamsu/tools/agent_tools.py`
- `shamsu/session/memory.py`

Layer 1: `AgentOrchestrator`

- Runs before model routing.
- Builds workspace facts: workspace path, index presence, top-level files,
  tools, recent conversation, and `@file` context.
- Uses deterministic handlers for:
  - "what folder are you in?"
  - "what files are here?"
  - "find PRDs"
  - "read/show/summarize @file"
  - weather prompts missing a location.
- Resolves follow-up prompts such as "check on the web", "open it", "do
  that", and "continue" using recent session memory.
- Calls `ensure_index()` best-effort so normal users no longer need to run
  `/index` before every workspace question.

Layer 2: deterministic route/workflow selection

- Local file/location/PRD questions are answered without LLM.
- Web-needed prompts ask permission for web search/fetch.
- Browser-needed prompts ask permission for Playwright browser use.
- Known coding workflows are routed to dedicated classes.
- Casual/general prompts go to the ReAct loop.

Layer 3: `AgentChatLoop`

- Uses Ollama's Python SDK: `ollama.AsyncClient(...).chat(...)`.
- Sends ordered chat state and tool schemas.
- Receives native `tool_calls`.
- Executes local tools securely.
- Appends `tool` role results.
- Calls the model again until no tool calls remain or round limit is hit.
- Uses a strict local-model system prompt that discourages filler and forces
  tool use for reads/writes/search/commands.
- In long-running mode, raises the round limit and adds a repetition guard that
  asks a clarifying question instead of looping forever.

Layer 4: markdown fallback

- Small models sometimes emit fenced code instead of native tool calls.
- `MarkdownWriteFallback` detects simple file-creation requests plus exactly
  one code block and one unambiguous target path.
- It writes through the same `write_file` tool path, so sandboxing and approval
  still apply.

## ReAct Tools Exposed To Ollama

Defined in `shamsu/tools/agent_tools.py`:

- `list_files(path=".")`
- `read_file(filepath)`
- `write_file(filepath, content, overwrite=false)`
- `run_command(command, cwd=".")`
- `search_index(query)`

Tool safety:

- Paths are validated by `Sandbox`.
- `write_file` asks approval before create/overwrite.
- `run_command` delegates to `CommandRunner`.
- `search_index` only reads `.shamsu/index.db` if it exists.
- Tool results are returned as compact JSON and appended as `role="tool"`.

## Specialist Workflows

Important files:

- `shamsu/agents/qa_workflow.py`
- `shamsu/agents/code_edit_workflow.py`
- `shamsu/agents/bugfix_workflow.py`
- `shamsu/agents/test_generation_workflow.py`
- `shamsu/agents/audit_workflow.py`
- `shamsu/agents/doc_workflow.py`
- `shamsu/agents/error_feedback_loop.py`
- `shamsu/agents/full_pipeline.py`

Common workflow pattern:

1. Search indexed context.
2. Pack relevant snippets with `ContextBuilder`.
3. Call a specialist model through `LLMManager.run_specialist()`.
4. Validate output shape.
5. For diffs, use `PatchEngine`.
6. Apply only after approval.
7. Return structured result data.
8. Log workflow/context/LLM/patch events to the active session.

Workflow roles:

- QA: answers with indexed context and optional streamed output.
- Code edit: requests unified diffs from `coder`, validates, previews, applies.
- Bug fix: parses traceback paths/lines, boosts those files in search, asks
  `bugfix`, validates/applies diff.
- Test generation: asks `test_gen` for pytest/Django tests as a unified diff.
- Audit: read-only review workflow using `reviewer`.
- Docs: proposes or applies README/doc changes through unified diffs.
- Error feedback loop: runs generated-project Django tests, sends failures to
  bug fix, retries with a bounded loop.
- Full pipeline: PRD -> project spec -> write Django project -> consistency
  checks -> setup -> tests -> bug-fix loop if needed.

Council mode:

- Defined in `shamsu/llm/council.py`.
- Sequential draft -> reviewer critique -> reconciled draft.
- Used for sensitive edits such as `settings.py` and in bug-fix/code-edit
  paths when heuristics say extra review is useful.
- Skipped in normal cases to save local compute.

## LLM Manager

Primary file:

- `shamsu/llm/manager.py`

Responsibilities:

- Enforce local-only LLM base URLs.
- Lazily ensure needed Ollama models are available.
- Route prompts with schema-constrained JSON output.
- Repair invalid JSON with `json_repair` as fallback.
- Call specialists with temperature presets.
- Stream QA/chat tokens when the CLI supports it.
- Log LLM request/response/error metadata.

Prompt layout:

```text
## Relevant code
<snippets>

## Context
<prd/task context>

## Errors / test output
<errors>

## Task (read this carefully)
<user request>
```

The task is placed at the end on purpose to reduce "lost in the middle"
failure for small local models.

## Context Engineering Tricks

Important files:

- `shamsu/indexer/walker.py`
- `shamsu/indexer/parser.py`
- `shamsu/retriever/search.py`
- `shamsu/context/builder.py`
- `shamsu/context/budget.py`
- `shamsu/context/assets/qwen3-tokenizer.json`

Indexing:

- Uses SQLite with FTS5.
- Ignores `.git`, `.shamsu`, `.venv`, caches, build folders, binaries, PDFs,
  images, media, archives, and lock files.
- Streams SHA-256 hashing in chunks.
- Detects language by extension.
- Extracts Python imports/classes/functions/methods/signatures/docstrings with
  AST.
- Builds line-window snippets for searchable context.
- Removes stale rows when files disappear.
- Incremental indexing skips unchanged files using size + mtime, then hash.
- `ensure_index()` is safe/best-effort and can be called before search.

Retrieval:

- FTS5 is the primary search layer.
- Natural multi-word queries are converted to OR terms for recall.
- Search over-fetches before reranking.
- Reranking adds small boosts for:
  - file-path term matches,
  - symbol-name matches,
  - lazy BM25 over recent snippets,
  - traceback/error-location paths from bug reports.
- BM25 is lazy-built and limited to recent snippets to avoid startup/RAM cost.

Packing:

- Duplicate snippets are removed when they share at least half their lines.
- Snippets are sorted by score.
- Code gets roughly half the total context budget.
- Each snippet is middle-truncated, keeping the first lines and last lines.
- This preserves signatures, imports, returns, and closing logic better than
  cutting only from the end.
- Qwen3 tokenizer is loaded lazily from the vendored tokenizer asset.
- If `tokenizers` or the asset is unavailable, token counting falls back to
  char/4.

Prompt design:

- `LLMManager._format_pack()` restates the task last.
- Specialist prompts require strict output forms for diffs/audits/docs.
- The ReAct system prompt tells the model not to claim actions unless tools
  confirm them.
- General chat gets workspace facts before LLM calls so it knows where it is.

## Workspace Tools And Mentions

Primary file:

- `shamsu/tools/workspace.py`

Capabilities:

- List top-level files/directories with internal folders hidden.
- Read text files with truncation.
- Find files by substring.
- Find PRD-like files by extension/name heuristic.
- Suggest `@` completions.
- Resolve `@README.md`, `@src/app.py`, `@"folder with spaces/file.md"`.
- Resolve folder mentions by summarizing contained files.
- Read PDF mentions through PRD text extraction.

Excluded from suggestions/listing:

- `.git`
- `.shamsu`
- `.venv`
- caches
- `node_modules`
- `dist`
- `build`

## Web And Browser Tools

Important files:

- `shamsu/tools/web.py`
- `shamsu/tools/browser.py`
- web/browser handlers in `shamsu/cli/repl.py`

Web:

- Uses DuckDuckGo HTML search.
- Decodes DuckDuckGo redirect URLs into real target URLs.
- Uses `trafilatura` for readable page extraction, with an HTML visible-text
  fallback.
- Asks approval before search/fetch.
- Can ask one combined approval to fetch top results.
- Skips empty/blocked/navigation-heavy pages where possible.
- Falls back to answer synthesis from result titles/snippets if page fetches
  fail.
- Logs query, approval, and summaries, not raw full pages by default.

Browser:

- Uses Playwright.
- Can open local URLs, read visible page text, click, type, navigate, and take
  screenshots.
- Intended mostly for local app preview/debugging.
- Browser session start and mutating actions require approval.

## Safety Model

Important files:

- `shamsu/safety/sandbox.py`
- `shamsu/safety/commands.py`
- `shamsu/safety/approval.py`
- `shamsu/safety/approval_manager.py`
- `shamsu/safety/permission_store.py`
- `shamsu/safety/autonomy.py`
- `shamsu/safety/clarify.py`
- `shamsu/tools/executor.py`

Sandbox:

- One workspace root is resolved at startup.
- File reads/writes validate through `Sandbox.validate()`.
- Paths outside the workspace are rejected.
- Workspace-local state is written under `.shamsu/`.

Approvals:

- Central `ApprovalManager` logs request/result events.
- Approval menu can offer:
  - yes,
  - yes and remember,
  - no.
- Remembered permissions can apply to safe file write/edit only.
- Web, browser, command execution, file delete, and MCP/tool actions are never
  auto-approved by memory.

Commands:

- `CommandRunner` validates cwd inside workspace.
- Blocks dangerous commands.
- Requires approval for medium-risk or unknown commands.
- Captures stdout/stderr.
- Redacts secrets from output.
- Enforces timeout.
- Uses Windows no-window flags.
- Provides `run_tests()` for pytest summary parsing.
- User-facing arbitrary shell execution is not exposed as a generic REPL
  command.

Autonomy:

- Default is off.
- `/autonomy on` enables longer agent/tool loops and one bounded extra retry in
  the full Django pipeline.
- Repetition guard asks a clarifying question instead of looping silently.

## Patch And Git Tools

Important files:

- `shamsu/patch/engine.py`
- `shamsu/patch/preview.py`
- `shamsu/tools/git.py`

Patch behavior:

- Accepts unified diffs only.
- Validates file headers, hunk counts, line counts, and sandbox paths.
- Renders a Rich preview.
- Requires approval before apply.
- Creates backups and can roll back failed applies.
- Supports create/edit/delete.
- Refreshes the workspace index after successful apply.

Git behavior:

- Read-only helper.
- Supports `git status --short`.
- Supports `git diff`.
- Used for dirty-worktree warnings before edit/fix/test/doc workflows.
- No git write commands are exposed.

## PRD To Django Pipeline

Important files:

- `shamsu/prd/input.py`
- `shamsu/prd/parser.py`
- `shamsu/prd/extractor.py`
- `shamsu/prd/project.py`
- `shamsu/prd/state.py`
- `shamsu/templates/django/renderer.py`
- `shamsu/templates/django/generators.py`
- `shamsu/templates/django/frontend.py`
- `shamsu/templates/django/docs.py`
- `shamsu/templates/django/writer.py`
- `shamsu/templates/django/checker.py`
- `shamsu/templates/django/frontend_checker.py`
- `shamsu/tools/django.py`
- `shamsu/agents/full_pipeline.py`

Pipeline:

1. Parse PRD from Markdown/TXT/PDF.
2. Extract entities/fields/relationships/endpoints/pages.
3. Infer CRUD endpoints and dashboard/list/detail/form pages if missing.
4. Select DaisyUI theme from PRD keywords.
5. Build `ProjectSpec`.
6. Preview/approve plan.
7. Create or resume `.shamsu/generation-state.json`.
8. Render deterministic fixed files.
9. Render deterministic backend files:
   - models,
   - serializers,
   - forms,
   - views,
   - app urls,
   - admin.
10. Render frontend templates:
   - base,
   - auth,
   - dashboard,
   - resource list/detail/form.
11. Render generated Django tests.
12. Render generated README and `SHAMSU_SUMMARY.md`.
13. Run backend and frontend static consistency checks.
14. Run Django setup and tests via safe command runner.
15. If tests fail, run bounded error feedback loop.

Generation state:

- Stored at `<workspace>/.shamsu/generation-state.json`.
- Tracks ordered file generation steps, status, completed files, last error,
  and timestamps.
- Used for resume and visibility.

Task state:

- Stored at `<workspace>/.shamsu/tasks/<task-id>.json`.
- Tracks multi-milestone PRD product builds and long-running task phases.
- Uses `TaskStep` from the shared contract and local `MilestoneTask`.

## Shared Contracts

Primary file:

- `shamsu/types.py`

Rule:

- Treat `types.py` as a shared team contract.
- Avoid changing it unless the team explicitly agrees.
- New modules usually define local result dataclasses instead.

Important shared types:

- `SearchResult`
- `IndexEntry`
- `ContextPack`
- `TaskStep`
- `ProjectSpec`
- `EntitySpec`
- `EndpointSpec`
- `PageSpec`
- `DjangoFileSpec`
- `LLMResponse`
- `RoutingDecision`
- `ApprovalRequest`
- `TestRunResult`

## Session Logs And Debugging

Important files:

- `shamsu/session/manager.py`
- `shamsu/session/memory.py`

What gets logged:

- session started/resumed/closed,
- user prompts,
- assistant messages,
- route decisions,
- agent context resolution,
- search/context packs,
- LLM request/response/error metadata,
- approval request/result/remembered decisions,
- command started/finished/blocked/denied/failed,
- patch preview/applied/failed/denied,
- model pulls,
- PRD parsing/planning,
- Django generation/setup/test/fix events,
- web/browser events.

Log format:

- JSONL events in `events.jsonl`.
- Timestamps are UTC ISO strings.
- Event IDs are short random hex strings.
- Payloads are redacted and long strings are truncated.
- Context pack logs include snippet path/line/score/symbol plus a capped
  preview, not unlimited raw source.

Commands:

- `/sessions list`
- `/sessions current`
- `/sessions show <id>`
- `/sessions resume <id-or-title>`
- `/sessions export <id>`
- `/log tail`

Exports:

- Written under `.shamsu/sessions/<id>/exports/<id>.zip`.
- Include `session.json`, `events.jsonl`, and `summary.md`.

## Tests And Verification

Test suite covers:

- CLI/workspace behavior.
- Slash command routing.
- Runtime/Ollama checks.
- Install script safety.
- Session logging/resume/export.
- Agent chat loop and orchestrator.
- Workspace tools and `@` mentions.
- Web/browser routing and extraction.
- Command runner safety.
- Patch validation/apply.
- Search/index/context budget.
- PRD parsing/extraction/planning.
- Django generation/checkers/setup/test/fix pipeline.
- Autonomy, permissions, long-running loops, and council mode.

Current verified status from `agent context/PROGRESS.md`:

```text
379 passed
ruff check shamsu tests scripts: passes
```

Run locally:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
.\.venv\Scripts\python.exe -m ruff check shamsu tests scripts
```

## Current Known Caveats

- This is not a Docker/container/OS sandbox.
- It is workspace sandboxing plus dependency isolation plus approval gates.
- Local model quality still depends heavily on the installed Ollama models.
- Web/browser access is permission-gated but not yet as capable as a full
  hosted browsing agent.
- Some older docs contain mojibake; avoid broad formatting churn unless needed.
- `.shamsu/` runtime/session artifacts in the SHAMSU repo are local noise and
  should usually not be committed.
- The current progress tracker says remaining work is mostly Milestone 6
  status/log/progress views, release docs, final safety audit, and release cut.

## Where To Look First For Future Changes

If changing CLI behavior:

- `shamsu/cli/repl.py`
- `shamsu/cli/command_router.py`
- `tests/test_cli_routing.py`

If changing agent intelligence:

- `shamsu/agents/orchestrator.py`
- `shamsu/agents/chat_loop.py`
- `shamsu/tools/agent_tools.py`
- `shamsu/llm/manager.py`

If changing context quality:

- `shamsu/indexer/walker.py`
- `shamsu/retriever/search.py`
- `shamsu/context/builder.py`
- `shamsu/context/budget.py`

If changing file safety:

- `shamsu/safety/sandbox.py`
- `shamsu/safety/approval_manager.py`
- `shamsu/tools/executor.py`
- `shamsu/patch/engine.py`

If changing PRD/Django generation:

- `shamsu/prd/input.py`
- `shamsu/prd/extractor.py`
- `shamsu/prd/project.py`
- `shamsu/templates/django/*`
- `shamsu/agents/full_pipeline.py`

If changing install/runtime:

- `scripts/install.ps1`
- `scripts/install.sh`
- `scripts/uninstall.ps1`
- `scripts/uninstall.sh`
- `shamsu/runtime/ollama.py`
- `shamsu/runtime/models.py`
- `shamsu/runtime/doctor.py`

## Mental Model For The Whole System

Think of SHAMSU as five rings:

1. CLI/session ring: parse input, keep sessions, show UX.
2. Deterministic tool ring: workspace, index, search, PRD parse, web/browser,
   command runner, patch engine.
3. Context ring: retrieve, rerank, dedupe, truncate, pack.
4. Local model ring: route, chat ReAct, specialists, council.
5. Project pipeline ring: PRD -> ProjectSpec -> Django files -> setup -> tests
   -> error feedback.

The system should prefer the inner deterministic rings first and only call the
model when the prompt needs reasoning, generation, synthesis, or routing beyond
simple local facts.
