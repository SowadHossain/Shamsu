
You are working inside the SHAMSU repo.

Task:
Integrate the real open-source Codebase-Memory MCP project as a REQUIRED external local tool for SHAMSU codebase memory.

Important correction:
Codebase-Memory MCP is NOT optional.
It should be installed/configured as part of SHAMSU setup.
SHAMSU should rely on it automatically when running inside a codebase.
Normal code-agent workflows should not run in a codebase without Codebase-Memory MCP healthy.

Use the real upstream project:
https://github.com/DeusData/codebase-memory-mcp

Do NOT:

- build SHAMSU’s own code graph
- build SHAMSU’s own AST parser
- create a fake dependency graph
- fake Codebase-Memory results
- copy Codebase-Memory source into SHAMSU source
- clone Codebase-Memory inside target user projects
- silently ignore missing Codebase-Memory during code workflows
- use cloud APIs
- upload source code anywhere

Correct architecture:
SHAMSU is the orchestrator.
Codebase-Memory MCP is the required external local code-intelligence backend.
Graphiti is the required external local long-term memory backend.
Ollama is the required local LLM backend.

Codebase-Memory MCP handles:

- parsing
- indexing
- code graph creation
- files
- symbols
- imports
- exports
- references
- call/dependency graph if available
- who uses what
- edit impact

SHAMSU source should contain only:

- adapter code
- setup/status/repair commands
- startup health gate
- query wrappers
- workflow hooks
- tests/mocks

Preferred tool layout:

- External managed tool:
  ~/.shamsu/tools/codebase-memory-mcp/
- Per-workspace code-memory metadata:
  <workspace></workspace>/.shamsu/abstract/
- SHAMSU source repo:
  integration code only

Scope:
Implement ONLY required Codebase-Memory MCP integration in this pass.
Do NOT implement Graphiti in this pass.
Do NOT implement full Context Engineering in this pass.

============================================================

1. REQUIRED TOOL BEHAVIOR
   ============================================================

Codebase-Memory MCP must behave like a required SHAMSU runtime dependency for codebase work.

On SHAMSU install:

1. Add setup/bootstrap support for Codebase-Memory MCP.
2. Install/configure Codebase-Memory MCP as a SHAMSU-managed external local tool.
3. Store it under:
   ~/.shamsu/tools/codebase-memory-mcp/
   or the existing SHAMSU-managed tools directory if one already exists.
4. Do not vendor it into SHAMSU source.
5. Do not clone it into target user workspaces.
6. Do not use sudo/admin/global installs.
7. Use existing SHAMSU installer/doctor/approval patterns.

On SHAMSU startup inside a codebase:

1. Check Codebase-Memory MCP availability.
2. Check local config.
3. Check whether the current workspace has a code-memory index.
4. If missing or stale, build/refresh it automatically.
5. If Codebase-Memory MCP is healthy, enter normal code-agent mode.
6. If Codebase-Memory MCP is missing/broken, block normal code-agent workflows and show repair instructions.

Allowed commands even when Codebase-Memory MCP is broken:

- /doctor
- /abstract setup
- /abstract repair
- /abstract status
- /help
- uninstall/repair-related commands

Normal code prompts should not run without Codebase-Memory MCP.

Startup failure UX example:

Codebase-Memory MCP is required for SHAMSU codebase mode but is not available.

Run:
  /abstract setup

or:
  shamsu doctor

SHAMSU will not run normal code-agent workflows in this workspace until local code memory is ready.

============================================================
2. LOCAL-ONLY SETUP POLICY
==========================

Everything must run locally.

Allowed:

- local process
- local MCP server
- localhost
- 127.0.0.1
- ::1
- local file paths
- local Docker/container endpoints if SHAMSU already treats them as local

Rejected:

- remote code indexing
- remote MCP server
- cloud APIs
- SaaS code analysis
- uploading source code anywhere

Do not guess install/start commands.
Inspect the upstream Codebase-Memory MCP README/docs and implement the documented local setup path.

Support config/env overrides:

- SHAMSU_CODEBASE_MEMORY_PATH
- SHAMSU_CODEBASE_MEMORY_CMD
- SHAMSU_CODEBASE_MEMORY_CONFIG
- SHAMSU_CODEBASE_MEMORY_URI

Reject remote/non-local URIs by default.

============================================================
3. STORAGE LAYOUT
=================

External tool/cache:
~/.shamsu/tools/codebase-memory-mcp/

Global SHAMSU tool config:
~/.shamsu/config.json
or existing SHAMSU config location

Workspace code-memory metadata:
<workspace></workspace>/.shamsu/abstract/

Suggested workspace files:

- status.json
- config.json
- last-index.json
- workspace-snapshot.json
- code-memory-events.jsonl

Raw session logs stay in:
<workspace></workspace>/.shamsu/sessions/

Graphiti memories stay in:
<workspace></workspace>/.shamsu/memory/

Keep boundaries strict:

- Codebase-Memory MCP = objective codebase facts
- Graphiti = long-term project/chat memory
- sessions = exact raw logs/tool output
- context packs = selected runtime context

============================================================
4. CODEBASE-MEMORY ADAPTER
==========================

Add a thin adapter around the real Codebase-Memory MCP tool/API.

Suggested files:

- shamsu/tools/codebase_memory.py
- shamsu/abstract/service.py
- shamsu/abstract/types.py

Adapter methods:

- is_available(workspace) -> bool
- healthcheck(workspace) -> dict
- status(workspace) -> dict
- setup(workspace) -> dict
- repair(workspace) -> dict
- start_server(workspace) -> dict
- index_workspace(workspace) -> dict
- refresh_workspace(workspace) -> dict
- query(workspace, query, limit=20) -> dict
- get_exports(workspace, path) -> dict
- get_imports(workspace, path) -> dict
- get_symbols(workspace, query_or_path) -> dict
- get_references(workspace, symbol_or_path) -> dict
- get_impact(workspace, path_or_symbol) -> dict
- get_module_contract(workspace, path) -> dict

Use the real upstream MCP/API/CLI according to docs.
Do not fake success.
Do not return made-up graph facts.

============================================================
5. INSTALLER / DOCTOR INTEGRATION
=================================

Update SHAMSU install and doctor flows.

Installer:

- Install/setup Codebase-Memory MCP as part of SHAMSU setup.
- If install is interactive, ask:
  “Install required local Codebase-Memory MCP tool? yes/no”
- If user says no, install should warn that SHAMSU codebase mode will not work until setup is completed.
- If unattended install mode exists, support a flag/config for installing required tools.

Doctor:

- Check Codebase-Memory MCP tool path.
- Check local MCP/server availability if needed.
- Check current workspace index status.
- Check whether index is stale.
- Print exact repair steps.
- Support /abstract repair if possible.

============================================================
6. STARTUP HEALTH GATE
======================

Implement a startup gate for codebase mode.

Normal SHAMSU code-agent mode requires:

- Ollama healthy
- Graphiti healthy if Graphiti integration exists
- Codebase-Memory MCP healthy
- workspace code-memory initialized or buildable

If Codebase-Memory MCP is unhealthy:

- do not enter normal code-agent workflow mode
- allow repair/setup/status commands only
- show clear reason and setup command

Pseudo behavior:

if workspace_is_codebase and not codebase_memory.healthcheck().ok:
    show_required_tool_error("Codebase-Memory MCP", repair="/abstract setup")
    enter_limited_repair_mode()
else:
    ensure_code_memory_index()
    enter_normal_agent_mode()

============================================================
7. AUTO-BUILD AND AUTO-REFRESH
==============================

The user should not need to manually run code-memory commands during normal use.

On SHAMSU startup inside a codebase:

1. Check current workspace file snapshot.
2. Compare to last indexed snapshot.
3. If no index exists, build code memory automatically.
4. If stale, refresh automatically.
5. If fresh, mark ready.
6. Show concise status:
   - “Code memory: ready”
   - “Code memory: building…”
   - “Code memory: refreshing…”
   - “Code memory: failed, run /abstract repair”

Do not freeze forever on huge repos.
Use visible progress.
If background indexing is supported cleanly, use it.
Otherwise, build before first code-agent task with progress.

After every successful code change:

1. Mark code memory stale.
2. Queue refresh.
3. Debounce multiple writes in one task.
4. Prefer incremental refresh if supported by upstream.
5. Log refresh result.

Hook into:

- write_file tool
- PatchEngine apply
- code_edit workflow
- bugfix workflow
- PRD/Django generation
- generated file writer
- Markdown write fallback

Do not refresh if write/patch failed.

============================================================
8. AUTOMATIC USE IN WORKFLOWS
=============================

Normal users should never need to say “use code memory.”

Before code-related LLM calls, SHAMSU must automatically query Codebase-Memory MCP.

Integrate into:

- AgentOrchestrator
- AgentChatLoop
- QA workflow for repo questions
- code edit workflow
- bugfix workflow
- error feedback loop
- test generation workflow
- audit workflow
- docs workflow
- PRD/Django pipeline after generation

Before editing a file, query:

- target file exports
- target file imports
- who imports target file
- impacted files/symbols
- public API names to preserve
- module contract if available

For import/export errors, query:

- importer file
- exporter file
- missing symbol
- similar exported symbols
- all references/importers

Then use facts to make minimal safe patches.

Example:
If `session.ts` imports `GameLoop` from `loop.ts`, and code memory says `loop.ts` exports `gameLoop`, prefer:

export { gameLoop as GameLoop };

instead of rewriting files.

============================================================
9. SLASH COMMANDS
=================

Add commands for status/debug/manual repair:

/abstract status
/abstract setup
/abstract repair
/abstract build
/abstract refresh
/abstract query <query></query>
/abstract exports <file></file>
/abstract imports <file></file>
/abstract symbols <query-or-file></query>
/abstract who-uses <file-or-symbol></file>
/abstract impact <file-or-symbol></file>

Behavior:

/abstract status

- show Codebase-Memory health
- show local config
- show workspace index state
- show stale/fresh status
- show whether normal code-agent mode is allowed

/abstract setup

- install/configure real upstream Codebase-Memory MCP locally
- use SHAMSU-managed tool cache
- no remote/cloud config

/abstract repair

- rerun health checks
- fix local config if possible
- rebuild index if needed
- print exact manual repair steps if blocked

============================================================
10. TOKEN-SAVING RULE
=====================

For code tasks, context priority should be:

1. Codebase-Memory MCP facts
2. targeted snippets
3. full files only if necessary

Never send huge files to the model if compact graph facts are enough.

============================================================
11. TESTS
=========

Use fake/mocked CodebaseMemoryAdapter.
Do not require the real upstream repo in unit tests.

Add tests:

1. startup blocks normal code-agent mode when Codebase-Memory unavailable
2. startup allows /abstract setup/status/repair when unavailable
3. startup enters normal code-agent mode when Codebase-Memory healthy
4. installer/setup uses SHAMSU-managed external tool path
5. /abstract setup uses approval/managed install path
6. /abstract repair reports missing local config
7. remote Codebase-Memory URI is rejected
8. local Codebase-Memory URI is accepted
9. startup builds index automatically when missing
10. startup refreshes index when stale
11. startup does not refresh when fresh
12. successful write_file marks code memory stale
13. failed write_file does not mark stale
14. successful patch apply queues refresh
15. multiple writes are debounced into one refresh
16. bugfix workflow queries exports/importers before patching
17. code-edit workflow queries impact before patching
18. import/export repair uses facts to choose alias export
19. no fake success when tool is missing
20. full files are not sent when compact facts are enough

============================================================
FINAL REQUIREMENTS
==================

Do not implement Graphiti in this pass.
Do not implement full Context Engineering in this pass.
Do not reinvent Codebase-Memory.
Do not create fake graph results.
Do not silently ignore missing Codebase-Memory in codebase mode.
Do not run normal code-agent workflows without Codebase-Memory healthy.
Do not upload code.
Do not use cloud APIs.
Do not bypass sandbox, approvals, CommandRunner, or session logging.
Do not break existing slash commands.
Do not break PRD/Django pipeline.
Do not break bugfix/code-edit workflows.

Deliverables:

1. Explain current SHAMSU startup/index/edit workflow.
2. Explain how real Codebase-Memory MCP will be installed and used as a required external local tool.
3. Implement the smallest clean adapter/service.
4. Add startup health gate.
5. Add setup/repair/status commands.
6. Add automatic build/refresh on startup and after edits.
7. Add automatic workflow queries before code edits/bugfixes.
8. Add tests with fake adapter.
9. Run targeted tests.
10. Summarize changes and limitations.

Final rule:
The user prompts normally. SHAMSU must automatically rely on local Codebase-Memory MCP for codebase work, keep it updated after code changes, and refuse normal code-agent mode if the required tool is not healthy.
