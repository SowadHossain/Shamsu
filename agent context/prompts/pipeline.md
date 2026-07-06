
You are working inside the SHAMSU repo.

Task:
Align SHAMSU’s architecture around the full intended agent workflow.

Goal:
The user should not need to repeatedly say:

- use Codebase-Memory MCP
- use Graphiti
- parse the error
- check imports/exports
- run build
- fix again
- use patch engine
- verify it
- continue next task

SHAMSU must automatically use the right internal/external tools and run a structured ReAct-style loop until the task is completed, blocked, or safely stopped.

Core architecture:

SHAMSU = orchestrator and safety layer

Required/managed tools:

- Ollama/local models = planner and coder models
- Codebase-Memory MCP = codebase facts, imports, exports, references, impact checks
- Graphiti = long-term project/user memory and durable lessons
- Taskmaster = PRD/spec to task graph and execution queue
- Haystack/context pipeline or chosen context layer = compact context assembly
- DiagnosticDigest = deterministic error/log parsing and ErrorPacket creation
- PatchEngine = safe file creation/edit/rename/delete/rollback
- ActionLedger = local human-facing debug/audit logs only
- CommandRunner/Verifier = run commands, build, tests, dev servers safely

Important:
The user prompts normally. SHAMSU decides which tools to use.
Do not wait for the user to explicitly request internal tools.
Do not make fake integrations.
Do not bypass safety systems.
Do not blindly repeat the same model prompt.
Do not claim success unless verification actually passes.

============================================================

1. REACT LOOP REQUIREMENT
   ============================================================

Implement a structured ReAct-style loop:

Reason / Plan
→ Act with tools
→ Observe tool output
→ update plan
→ Act again if needed
→ verify
→ stop with clear result

For coding tasks, the loop should look like:

1. Receive user prompt.
2. Classify task type.
3. Start ActionLedger run.
4. Retrieve relevant Graphiti memories automatically.
5. Query Codebase-Memory MCP automatically when in a codebase.
6. Parse latest errors with DiagnosticDigest if errors/logs exist.
7. Build compact context pack.
8. Planner model creates structured plan.
9. Coder model creates structured patch/file operation.
10. PatchEngine validates and applies changes.
11. Formatter runs if configured.
12. Verifier runs build/test/check command.
13. DiagnosticDigest parses failure output.
14. If verification fails and there is new evidence, loop again.
15. If same failure repeats, stop with stall reason.
16. If verification passes, report success.
17. Log everything to ActionLedger.
18. Store only durable lessons/decisions in Graphiti.

Do not repeat the same prompt without new observations.

Allowed retry reasons:

- new ErrorPacket exists
- patch failed with exact patch error
- build/test output changed
- Codebase-Memory found new impacted files
- formatter produced new diagnostics
- task dependency/status changed

Stop conditions:

- verification passes
- task is blocked
- same root diagnostic repeats
- same patch fails repeatedly
- required tool is unavailable
- destructive operation needs approval
- max iteration limit reached

============================================================
2. AUTOMATIC TOOL USE
=====================

SHAMSU must use tools automatically based on task type.

For repo/code questions:

- use Codebase-Memory MCP
- use targeted file reads only when needed
- use Graphiti for relevant project/user rules
- build compact context

For bug fixes:

- run/check relevant command if needed
- parse logs with DiagnosticDigest
- query Codebase-Memory for impacted files/symbols/imports/exports
- plan fix
- apply through PatchEngine
- verify
- retry only with new diagnostics

For file edits:

- use Codebase-Memory before edits
- use PatchEngine for all mutations
- run formatter/verifier
- refresh code memory after successful mutation

For delete/rename/move:

- require Codebase-Memory impact check
- require approval when destructive
- use PatchEngine transaction/trash/rollback
- verify after change

For PRD/spec/project generation:

- use Taskmaster to parse PRD into tasks
- execute one task at a time by default
- verify each task before marking done
- update Taskmaster status
- do not reparse PRD every prompt

For long-term preferences/decisions:

- use Graphiti
- do not store random temporary logs/errors
- do not store secrets
- do not use ActionLedger as memory

For debugging/monitoring:

- use ActionLedger
- keep it separate from memory/context
- never automatically feed ActionLedger into model context

============================================================
3. TWO-MODEL WORKFLOW
=====================

Planner model:

- understands the task
- reviews evidence
- produces structured plan
- lists risks and constraints
- chooses next action

Coder model:

- receives plan + exact snippets + code facts
- outputs structured FileChangePlan and unified diff
- does not directly write files
- does not decide everything from scratch

PatchEngine:

- validates coder output
- applies safely
- journals transaction
- can roll back

Verifier:

- decides whether the task is actually done

============================================================
4. CONTEXT RULES
================

Do not dump everything into the model.

Context should come from:

- current user prompt
- Taskmaster task details if applicable
- relevant Graphiti memories
- Codebase-Memory facts
- DiagnosticDigest ErrorPacket
- targeted snippets
- recent tool observations

Avoid:

- full chat history
- full raw logs
- full source files unless necessary
- all Graphiti memories
- all code graph facts
- ActionLedger logs by default
- repeated old errors

The context layer should select, rank, dedupe, and budget context before model calls.

============================================================
5. ERROR FIX LOOP
=================

SHAMSU must have an automatic fix loop.

When verification fails:

1. Save raw command output.
2. Parse output with DiagnosticDigest.
3. Create ErrorPacket.
4. Compare with previous ErrorPacket.
5. Query Codebase-Memory for related files/symbols.
6. Build new compact context.
7. Ask planner/coder for next patch only if there is new evidence.
8. Apply through PatchEngine.
9. Verify again.

Do not ask user to manually say “fix again” after every failure.
Do not loop forever.
Do not repeat same patch against same file hash.
Do not claim success after failed verification.

============================================================
6. TASKMASTER WORKFLOW
======================

For PRD/spec execution:

1. Parse PRD through Taskmaster.
2. Store/read task graph from Taskmaster state.
3. Show task plan to user.
4. Execute next unblocked task.
5. Use full SHAMSU toolchain for each task.
6. Verify each task.
7. Mark task done only when verification passes or user explicitly accepts.
8. Mark task blocked/failed with reason when needed.
9. Continue only if user approved batch mode.

Taskmaster owns:

- task breakdown
- dependencies
- task status
- execution order

SHAMSU owns:

- code understanding
- context
- patching
- verification
- safety
- memory boundaries
- logs

============================================================
7. ACTIONLEDGER BOUNDARY
========================

ActionLedger is debug/audit only.

It should log:

- prompt
- task classification
- decisions summaries
- tool calls
- model call metadata
- context preview
- code memory queries
- Graphiti retrieval metadata
- diagnostics
- patch transactions
- commands
- verification result
- final response

It must not:

- become memory
- automatically enter model context
- store raw hidden chain-of-thought
- store secrets
- upload logs

Log decision summaries, not raw private reasoning.

============================================================
8. REQUIRED SAFETY RULES
========================

Never:

- edit outside workspace
- bypass sandbox/approvals
- bypass PatchEngine
- let model directly overwrite files
- permanently delete files first
- use cloud APIs by default
- upload code/logs/tasks
- fake tool results
- fake patch success
- fake build success
- mark task done without verification
- silently ignore required tool failure

Always:

- use required tools automatically
- validate paths
- parse errors before giving logs to model
- apply edits through PatchEngine
- run verification after mutation
- refresh Codebase-Memory after successful code changes
- store durable lessons in Graphiti only when appropriate
- log run details in ActionLedger
- stop safely when blocked/stalled

============================================================
9. IMPLEMENTATION INSTRUCTIONS
==============================

Before coding:

1. Inspect current SHAMSU architecture.
2. Identify conflicts with this workflow.
3. Make a small implementation plan.
4. Prefer refactoring existing systems over adding duplicates.

Implementation priority:

1. Orchestrator ReAct loop
2. Automatic tool routing
3. Bugfix/error repair loop
4. Two-model planner/coder handoff
5. Taskmaster task execution flow
6. Safety boundaries between tools
7. ActionLedger events for the whole workflow
8. Tests

Do not rewrite the whole repo.
Keep changes focused and testable.
Preserve existing working behavior unless it conflicts with this architecture.

============================================================
10. TESTS
=========

Add or update tests for:

- normal prompt routes to correct workflow
- code task automatically queries Codebase-Memory MCP
- bugfix task parses diagnostics before model retry
- model is not called repeatedly without new evidence
- failed verification triggers repair loop
- same repeated ErrorPacket triggers stall guard
- PatchEngine is required for file mutations
- task is not marked successful without verification
- Taskmaster task executes through SHAMSU safety workflow
- Graphiti is used as memory, not task queue
- ActionLedger logs run events but is not used as memory/context
- required tool failure blocks unsafe workflow
- user does not need to manually request internal tools

Final rule:
SHAMSU must behave like a structured local ReAct coding agent. The user gives the goal; SHAMSU automatically plans, uses tools, observes results, fixes issues, verifies, logs, and stops safely.
