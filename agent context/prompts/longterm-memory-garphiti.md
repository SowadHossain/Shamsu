
You are working inside the SHAMSU repo.

Task:
Integrate the real open-source Graphiti project as a REQUIRED external local tool for SHAMSU long-term memory.

Use the real upstream project:
https://github.com/getzep/graphiti

Important:
Do NOT build SHAMSU’s own graph memory.
Do NOT create fake Graphiti behavior.
Do NOT store everything in JSON and call it Graphiti.
Do NOT replace SHAMSU’s agent loop.
Do NOT use cloud APIs.
Do NOT use hosted Zep.
Do NOT upload code, chat, memory, files, logs, or project data anywhere.

Correct architecture:
SHAMSU is the orchestrator.
Graphiti is the required external local long-term memory backend.
Codebase-Memory MCP is the required external local codebase-memory backend.
Ollama is the required local LLM backend.

Graphiti remembers:

- user preferences
- durable project decisions
- workflow rules
- bug lessons
- architecture notes
- long-term constraints
- useful task summaries

Graphiti must NOT store:

- secrets
- API keys
- passwords
- private keys
- full source files
- full command logs
- raw chain-of-thought
- every random chat message
- temporary one-off errors

Scope:
Do ONLY Graphiti long-term memory integration in this pass.
Do NOT implement Codebase-Memory MCP in this pass.
Do NOT implement the full Context Engineering layer in this pass.

============================================================

1. REQUIRED TOOL BEHAVIOR
   ============================================================

Graphiti must behave like a required SHAMSU runtime dependency.

On SHAMSU install:

1. Add setup/bootstrap support for Graphiti.
2. Install/configure Graphiti as a SHAMSU-managed external local tool.
3. Store it under:
   ~/.shamsu/tools/graphiti/
   or the existing SHAMSU-managed tools directory if one exists.
4. Do not vendor Graphiti into SHAMSU source.
5. Do not clone Graphiti into target user workspaces.
6. Do not use sudo/admin/global installs.
7. Use existing SHAMSU installer/doctor/approval patterns.

On SHAMSU startup:

1. Check Graphiti availability.
2. Check local config.
3. Check Graphiti health.
4. If healthy, enter normal SHAMSU agent mode.
5. If missing/broken, block normal agent mode and enter limited repair mode.

Allowed commands when Graphiti is broken:

- /doctor
- /memory setup
- /memory repair
- /memory status
- /help
- uninstall/repair-related commands

Normal prompts should not run without Graphiti.

Startup failure UX:

Graphiti memory backend is required but not available.

Run:
  /memory setup

or:
  shamsu doctor

SHAMSU will not start normal agent mode until local Graphiti memory is ready.

============================================================
2. LOCAL-ONLY SETUP POLICY
==========================

Graphiti must run locally.

Allowed:

- localhost
- 127.0.0.1
- ::1
- local file paths
- local Docker/container endpoints if SHAMSU already treats them as local

Rejected:

- cloud Zep
- remote Graphiti
- remote graph database
- remote embedding service
- remote LLM service
- OpenAI API
- any non-local memory endpoint

If Graphiti needs LLM/embedding configuration, wire it to local providers only:

- local Ollama model
- local embedding model
- local database/graph backend

Do not guess install/start commands.
Inspect upstream Graphiti docs and implement the documented local setup path.

Support config/env overrides:

- SHAMSU_GRAPHITI_PATH
- SHAMSU_GRAPHITI_CMD
- SHAMSU_GRAPHITI_CONFIG
- SHAMSU_GRAPHITI_URI

Reject remote/non-local URIs by default.

============================================================
3. STORAGE LAYOUT
=================

External tool/cache:
~/.shamsu/tools/graphiti/

Global SHAMSU tool config:
~/.shamsu/config.json
or existing SHAMSU config location

Workspace memory metadata:
<workspace></workspace>/.shamsu/memory/

Suggested files:

- status.json
- config.json
- last-sync.json
- memory-events.jsonl

Raw session logs remain in:
<workspace></workspace>/.shamsu/sessions/

Codebase facts remain in:
<workspace></workspace>/.shamsu/abstract/

Keep boundaries strict:

- Graphiti = long-term chat/project memory
- Codebase-Memory MCP = codebase graph facts
- sessions = raw logs/tool output
- context packs = selected runtime context

============================================================
4. GRAPHITI ADAPTER
===================

Add a thin adapter around the real Graphiti tool/API.

Suggested files:

- shamsu/memory/graphiti_adapter.py
- shamsu/memory/service.py
- shamsu/memory/policy.py
- shamsu/memory/types.py

Adapter methods:

- is_available(workspace) -> bool
- healthcheck(workspace) -> dict
- status(workspace) -> dict
- setup(workspace) -> dict
- repair(workspace) -> dict
- add_episode(workspace, text, metadata=None) -> dict
- remember(workspace, text, kind, metadata=None) -> dict
- search(workspace, query, limit=8, filters=None) -> dict
- get_relevant(workspace, user_prompt, task_type=None, limit=8) -> list
- forget(workspace, memory_id_or_query) -> dict
- summarize_session(workspace, session_id) -> dict

Use the real Graphiti API/client/CLI according to upstream docs.
Do not fake success.
Do not return made-up memory results.

============================================================
5. INSTALLER / DOCTOR INTEGRATION
=================================

Update SHAMSU install and doctor flows.

Installer:

- Install/setup Graphiti as part of SHAMSU setup.
- If install is interactive, ask:
  “Install required local Graphiti memory tool? yes/no”
- If user says no, warn that SHAMSU normal agent mode will not work until Graphiti setup is completed.
- If unattended install mode exists, support a flag/config for installing required tools.

Doctor:

- Check Graphiti tool path.
- Check Graphiti local config.
- Check local database/backend availability if required.
- Check local LLM/embedding configuration if required.
- Print exact repair steps.
- Support /memory repair if possible.

============================================================
6. STARTUP HEALTH GATE
======================

Implement a startup gate.

Normal SHAMSU agent mode requires:

- Ollama healthy
- Graphiti healthy
- workspace state initialized

If Graphiti is unhealthy:

- do not enter normal agent loop
- allow repair/setup/status commands only
- show clear reason and setup command

Pseudo behavior:

if not graphiti.healthcheck().ok:
    show_required_tool_error("Graphiti", repair="/memory setup")
    enter_limited_repair_mode()
else:
    enter_normal_agent_mode()

Do not silently downgrade to fallback memory for normal use.

============================================================
7. MEMORY POLICY
================

Create MemoryPolicy.

Memory kinds:

- user_preference
- project_decision
- workflow_rule
- bug_lesson
- architecture_note
- task_summary
- safety_rule

Store automatically only if durable and useful.

Store when:

1. user explicitly says “remember”
2. user says “always”, “from now on”, or “going forward”
3. a durable project decision is made
4. a workflow rule is established
5. a repeated bug lesson should affect future repairs
6. a task summary is useful across sessions

Do not store:

1. random chatter
2. temporary compile errors
3. huge logs
4. secrets
5. full source code
6. raw chain-of-thought
7. sensitive personal data unless explicitly requested

Before storing, redact:

- API keys
- passwords
- tokens
- private keys
- .env values
- credentials

============================================================
8. AUTOMATIC RETRIEVAL BEFORE TASKS
===================================

Normal users should never need to manually search memory.

Before model calls, SHAMSU must automatically retrieve relevant Graphiti memories.

Integrate into:

- AgentOrchestrator
- AgentChatLoop
- QA workflow
- code edit workflow
- bugfix workflow
- error feedback loop
- docs workflow
- audit workflow
- PRD/Django pipeline
- dev server workflow

Memory context should be compact:

- max 5-8 memories by default
- include kind + text
- dedupe repeated memories
- prefer recent/high-confidence memories
- no huge provenance unless needed

Example model context section:

Relevant long-term memory:

- [user_preference] User wants dev servers opened in a new CMD window.
- [workflow_rule] Never claim build success unless command exit code is 0.
- [bug_lesson] Preserve TypeScript exports before editing modules.

============================================================
9. AUTOMATIC WRITE AFTER TASKS
==============================

After meaningful tasks:

1. summarize durable lessons
2. apply MemoryPolicy
3. store useful memories in Graphiti
4. log memory write event to session

Do not write after every message.
Do not store raw conversation.
Do not store raw chain-of-thought.

Examples worth storing:

- User wants visible progress instead of only Thinking.
- Project uses Codebase-Memory MCP for code facts.
- Bug lesson: for TS import/export errors, prefer alias exports before rewriting importers.
- Rule: never claim build success unless verification command exits with code 0.

============================================================
10. SLASH COMMANDS
==================

Add commands:

/memory status
/memory setup
/memory repair
/memory remember <text></text>
/memory search <query></query>
/memory recent
/memory forget <id-or-query></id>
/memory summarize-session

Behavior:

/memory status

- show Graphiti health
- show local config
- show workspace memory path
- show whether normal agent mode is allowed

/memory setup

- install/configure real Graphiti locally
- use SHAMSU-managed tool cache
- no cloud config
- no remote services

/memory repair

- rerun health checks
- fix missing local config if possible
- print exact manual repair steps if needed

/memory remember <text></text>

- store explicit memory using Graphiti
- infer kind using MemoryPolicy
- do not store secrets

/memory search <query></query>

- search Graphiti
- show compact results

/memory forget <id-or-query></id>

- delete/mark memory forgotten
- if ambiguous, show candidates

============================================================
11. TESTS
=========

Use fake/mocked Graphiti adapter.
Do not require real Graphiti in unit tests.

Add tests:

1. startup blocks normal agent mode when Graphiti unavailable
2. startup allows repair commands when Graphiti unavailable
3. startup enters normal mode when Graphiti healthy
4. installer/setup uses SHAMSU-managed external tool path
5. /memory setup uses approval/managed install path
6. /memory repair reports missing local config
7. remote Graphiti URL is rejected
8. local Graphiti URL is accepted
9. /memory remember stores explicit memory
10. /memory search returns fake adapter results
11. /memory forget handles matching memory
12. MemoryPolicy stores “remember this”
13. MemoryPolicy stores “from now on”
14. MemoryPolicy rejects transient errors
15. MemoryPolicy rejects secrets
16. workflows retrieve relevant memories before LLM call
17. task completion writes durable bug_lesson
18. raw chain-of-thought is never stored
19. full source files are not stored as memory
20. duplicate memories are deduped
21. no fake success when Graphiti tool is missing

============================================================
FINAL REQUIREMENTS
==================

Do not implement Codebase-Memory MCP in this pass.
Do not implement full Context Engineering in this pass.
Do not create fake Graphiti behavior.
Do not build SHAMSU’s own graph memory.
Do not silently ignore missing Graphiti.
Do not enter normal agent mode without Graphiti healthy.
Do not use cloud APIs.
Do not upload code/chat/memory.
Do not store secrets.
Do not store raw chain-of-thought.
Do not break existing session logging.
Do not break slash commands.
Do not break bugfix/code-edit workflows.
Do not break PRD/Django pipeline.

Deliverables:

1. Explain current SHAMSU startup/session/memory path.
2. Explain how real Graphiti will be installed and used as a required external local tool.
3. Implement the smallest clean Graphiti adapter/service/policy.
4. Add startup health gate.
5. Add setup/repair/status commands.
6. Add automatic retrieval before model calls.
7. Add automatic durable-memory writes after meaningful tasks.
8. Add tests with fake adapter.
9. Run targeted tests.
10. Summarize changes and limitations.

Final rule:
SHAMSU must not run normal agent mode without local Graphiti memory healthy. Graphiti is a required external local tool managed by SHAMSU, not a fake internal fallback.
