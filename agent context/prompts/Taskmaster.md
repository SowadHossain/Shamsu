
You are working inside the SHAMSU repo.

Task:
Integrate a real external local PRD/task planning tool into SHAMSU.

Chosen tool:
Taskmaster

Goal:
SHAMSU should be able to take a PRD/spec/feature request and turn it into an executable task graph instead of trying to build the whole project in one giant prompt.

Flow:

PRD / feature spec
→ Taskmaster parses it into tasks/subtasks/dependencies
→ SHAMSU picks the next task
→ SHAMSU executes task safely using existing architecture
→ verification runs
→ task status is updated
→ next task continues only when previous task is done or intentionally skipped

Important:
Taskmaster is the PRD/task planner.
Graphiti is NOT the task planner.
Codebase-Memory MCP is NOT the task planner.
ActionLedger is NOT the task planner.
SHAMSU still controls execution, safety, tools, patching, verification, and logging.

Correct boundary:

Taskmaster:

- PRD parsing
- task breakdown
- subtasks
- dependencies
- task status
- execution order
- task queue

Graphiti:

- long-term project/user memory
- durable architecture decisions
- workflow lessons
- user preferences
- not task queue

Codebase-Memory MCP:

- codebase facts
- imports/exports
- impact checks
- not task planning

PatchEngine:

- safe file mutation
- rollback
- verification
- not task planning

ActionLedger:

- debug/audit logs for humans
- not memory
- not task planning

SHAMSU:

- orchestrator
- local-first safety layer
- runs the task step-by-step

============================================================

1. EXTERNAL TOOL POLICY
   ============================================================

Use real upstream Taskmaster.
Do NOT build SHAMSU’s own PRD parser/task planner from scratch.
Do NOT fake Taskmaster results.
Do NOT store random JSON and call it Taskmaster.
Do NOT replace SHAMSU’s orchestrator with Taskmaster.
Do NOT let Taskmaster bypass SHAMSU safety systems.

Taskmaster should be installed/configured as a SHAMSU-managed external local tool.

Suggested managed tool path:

~/.shamsu/tools/taskmaster/

Per-workspace Taskmaster state should stay in the project workspace, using Taskmaster’s own expected local project structure.

Likely workspace state:

<workspace></workspace>/.taskmaster/

But inspect Taskmaster docs and current CLI behavior before coding.
Do not guess commands or file formats.

============================================================
2. LOCAL-FIRST REQUIREMENTS
===========================

Everything must run locally by default.

Allowed:

- local CLI
- local files
- local project task state
- local Ollama model provider
- localhost / 127.0.0.1 / ::1 if needed

Rejected by default:

- OpenAI
- Anthropic
- Gemini
- hosted cloud providers
- remote research mode
- remote telemetry
- remote task storage
- uploading PRDs/code/tasks anywhere

If Taskmaster supports multiple providers, SHAMSU must configure it for local-only usage.

Preferred model provider:

- Ollama or existing SHAMSU local model provider

If Taskmaster cannot run a certain operation locally:

- fail clearly
- explain what is missing
- do not silently switch to cloud

============================================================
3. REQUIRED BEHAVIOR
====================

SHAMSU should support PRD-driven execution.

User examples:

"Build this project from this PRD"
"Parse this PRD and make tasks"
"Continue the next task"
"Show remaining tasks"
"Execute task 4"
"Mark this task blocked"
"Regenerate tasks from the updated PRD"

Expected behavior:

1. SHAMSU detects PRD/spec planning request.
2. Taskmaster is checked.
3. If Taskmaster is missing or unhealthy, SHAMSU shows setup/repair instructions.
4. Taskmaster parses PRD into task graph.
5. SHAMSU stores/reads task state from Taskmaster.
6. SHAMSU shows task summary to user.
7. SHAMSU executes one task at a time unless user approves batch execution.
8. Each task execution uses:
   - Codebase-Memory MCP for code facts
   - Graphiti for durable rules/preferences
   - DiagnosticDigest for errors
   - PatchEngine for file mutation
   - Verifier for build/test
   - ActionLedger for debug trace
9. Task is marked done only after verification passes or user explicitly accepts it.
10. Failed tasks are marked failed/blocked with reason.
11. SHAMSU should not re-plan every prompt.
12. Re-plan only when PRD changes, user asks, or current task graph becomes invalid.

============================================================
4. RESOURCE POLICY
==================

Taskmaster should be event-based, not always running.

Good:

- run when PRD is parsed
- run when tasks are expanded
- run when user asks for next task
- run when task status changes
- run when PRD changes

Bad:

- run Taskmaster on every user prompt
- re-parse full PRD repeatedly
- re-plan the whole project after every tiny edit
- send huge PRD/task graph to local model every time

Cache parsed tasks and reuse them.

Use local model calls only when needed.

============================================================
5. SETUP / DOCTOR
=================

Add setup and health checks.

Commands to add:

/taskmaster status
/taskmaster setup
/taskmaster repair

Behavior:

/taskmaster status

- show whether Taskmaster is installed
- show managed tool path
- show current workspace task state
- show local provider config
- show whether cloud providers are disabled/rejected

/taskmaster setup

- install/configure Taskmaster as external local tool
- use SHAMSU-managed tool path
- no global install unless existing SHAMSU installer standard allows it
- no sudo/admin
- no cloud provider config
- configure local/Ollama provider if needed

/taskmaster repair

- re-check install
- fix config if possible
- print exact manual repair steps if blocked

Do not silently install without user/setup approval if current SHAMSU policy requires approval.

============================================================
6. TASK COMMANDS
================

Add user commands:

/prd parse <file-or-path></file>
/prd status
/prd reparse
/tasks
/tasks next
/tasks show <id></id>
/tasks execute <id></id>
/tasks continue
/tasks mark-done <id></id>
/tasks mark-blocked <id></id> <reason></reason>
/tasks mark-failed <id></id> <reason></reason>
/tasks dependencies <id></id>
/tasks plan

Behavior:

/prd parse

- parse PRD through Taskmaster
- create/update Taskmaster project state
- show generated task summary
- do not execute automatically unless user asked

/tasks

- list tasks with status, priority, dependencies

/tasks next

- show next executable unblocked task

/tasks execute <id></id>

- execute selected task through SHAMSU workflow
- use PatchEngine/Verifier
- update task status only after verification

/tasks continue

- execute next task from queue
- stop after one task by default unless batch mode approved

/tasks plan

- show execution order and dependency graph summary

============================================================
7. TASK EXECUTION FLOW
======================

When executing a Taskmaster task:

1. Load task details from Taskmaster.
2. Check dependencies are complete or intentionally skipped.
3. Build compact task context:
   - task title
   - task description
   - acceptance criteria
   - dependencies
   - relevant PRD section
   - relevant Graphiti memories
   - relevant Codebase-Memory facts
   - latest diagnostics if continuing a failed task
4. Planner model creates task execution plan.
5. Coder model creates patch/file operations.
6. PatchEngine applies safely.
7. Formatter runs if configured.
8. Verification command runs.
9. DiagnosticDigest parses failures.
10. Task status updates:
    - done if verified
    - failed if verification fails and retry limit reached
    - blocked if dependency/context/tool missing
11. ActionLedger records the full run for manual debugging.
12. Graphiti stores only durable lessons/decisions, not every task event.

============================================================
8. STATUS UPDATE RULES
======================

Do not mark a task done just because the model says it is done.

Task done requires one of:

- verification command exit code 0
- tests pass
- build passes
- user explicitly accepts non-verifiable task

If verification fails:

- keep task in failed/in-progress state
- attach ErrorPacket summary
- attach reason
- suggest next action

If patch fails:

- do not mark done
- record patch failure
- rollback if needed

If task cannot run because dependency is incomplete:

- mark blocked
- explain dependency

============================================================
9. GRAPHITI INTEGRATION
=======================

Use Graphiti only for durable memory.

Before planning/executing a task:

- retrieve relevant project memories
- retrieve user preferences
- retrieve workflow rules
- retrieve past bug lessons

After task completion:
store only useful durable facts, such as:

- architecture decision made
- user preference discovered
- recurring bug lesson
- important workflow rule
- verified implementation milestone

Do NOT store every task event in Graphiti.
Do NOT store Taskmaster task graph in Graphiti.
Do NOT use Graphiti as task queue.
Do NOT use Graphiti to replace Taskmaster.

============================================================
10. CODEBASE-MEMORY MCP INTEGRATION
===================================

Use Codebase-Memory MCP during task execution.

Before editing:

- query relevant files/symbols
- query imports/exports
- query impact of target files
- query who uses changed files/symbols

After successful code mutation:

- mark code memory stale
- refresh affected index/facts

Do not use Codebase-Memory MCP as task planner.

============================================================
11. PATCHENGINE / DIAGNOSTICS INTEGRATION
=========================================

All file changes from task execution must go through PatchEngine.

Do not let Taskmaster or model directly write files.

After verification:

- DiagnosticDigest parses failures
- ErrorPacket is attached to task run
- same repeated failure should trigger stall guard

If task execution fails:

- leave task status clear
- provide compact diagnostic reason
- rollback if policy says so

============================================================
12. ACTIONLEDGER INTEGRATION
============================

ActionLedger should record:

- PRD parsed
- tasks created
- task selected
- task execution started
- model calls
- code facts queried
- patches applied
- commands run
- task done/failed/blocked
- final user output

ActionLedger remains debug-only.
Do not feed ActionLedger back into model context automatically.

============================================================
13. BATCH EXECUTION POLICY
==========================

By default, SHAMSU should execute one task at a time.

For batch execution:

- require explicit user approval
- verify each task before moving to next
- stop on first failed/blocking task
- show progress
- record each task separately in ActionLedger

No blind long-running execution without checkpoints.

============================================================
14. TESTS
=========

Use fake/mocked Taskmaster adapter in unit tests.
Do not require real Taskmaster in unit tests unless integration tests are explicitly marked.

Add tests:

Setup/status:

1. detects missing Taskmaster
2. setup uses managed external tool path
3. rejects cloud provider config
4. accepts local provider config
5. status shows workspace task state

PRD parsing:
6. /prd parse calls Taskmaster adapter
7. parsed tasks are listed
8. PRD is not reparsed on every prompt
9. reparse only happens when requested or PRD changed

Task queue:
10. /tasks lists tasks
11. /tasks next returns unblocked task
12. dependencies block execution
13. blocked task is reported clearly

Execution:
14. /tasks execute loads task details
15. execution uses Codebase-Memory MCP
16. execution retrieves Graphiti memories
17. file changes go through PatchEngine
18. verification command runs
19. DiagnosticDigest parses failed verification
20. task marked done only when verification passes
21. failed verification does not mark task done
22. blocked dependency marks task blocked
23. failed patch does not mark task done
24. successful task updates Taskmaster status

Graphiti boundary:
25. Graphiti is not used as task queue
26. task graph is not stored in Graphiti
27. only durable lessons are stored in Graphiti

ActionLedger boundary:
28. task execution events are logged
29. ActionLedger is not used as memory
30. ActionLedger is not automatic context

Resource behavior:
31. Taskmaster is not called on every normal prompt
32. cached task graph is reused
33. batch mode stops on first failed task

Safety:
34. no cloud provider is used by default
35. no code is uploaded
36. no direct model file writes
37. no task is marked done without verification or explicit acceptance

============================================================
FINAL REQUIREMENTS
==================

Do not build a custom PRD/task planner.
Do not fake Taskmaster.
Do not use Graphiti as task planner.
Do not use Codebase-Memory MCP as task planner.
Do not let Taskmaster bypass SHAMSU safety.
Do not run Taskmaster on every prompt.
Do not use cloud APIs.
Do not upload PRDs/code/tasks.
Do not mark tasks done without verification.
Do not break existing SHAMSU workflows.

Deliverables:

1. Inspect current SHAMSU planning/task workflow.
2. Inspect Taskmaster docs/CLI before coding.
3. Implement Taskmaster adapter/service.
4. Add setup/status/repair commands.
5. Add PRD/task commands.
6. Add task execution workflow through SHAMSU orchestrator.
7. Integrate Graphiti, Codebase-Memory MCP, PatchEngine, DiagnosticDigest, and ActionLedger boundaries.
8. Add tests with fake Taskmaster adapter.
9. Run targeted tests.
10. Summarize changes, conflicts fixed, tests run, and limitations.

Final rule:
Taskmaster owns PRD-to-task planning and task status. SHAMSU owns execution, safety, context, patching, verification, memory boundaries, and logging.
