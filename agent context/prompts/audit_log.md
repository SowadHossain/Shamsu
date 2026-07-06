
You are working inside the SHAMSU repo.

Task:
Implement SHAMSU’s local ActionLedger / DebugTrace system.

Goal:
SHAMSU must keep a detailed local audit/debug log of every run so the user can manually inspect:

- what prompt was given
- what SHAMSU decided to do
- what tools were called
- what context was prepared
- what files were read/written
- what patches were applied
- what commands were run
- what errors/output came back
- what final answer was shown
- why a task failed or succeeded

Important boundary:
This is NOT memory.
This is NOT Graphiti.
This is NOT Codebase-Memory MCP.
This is NOT Haystack/context retrieval.
This is NOT automatically fed back into the model.

ActionLedger is only for:

- user monitoring
- manual debugging
- audit trail
- run inspection
- replaying what happened

Do NOT:

- store ActionLedger events in Graphiti automatically
- use ActionLedger as long-term memory
- automatically retrieve ActionLedger into model context
- use ActionLedger to replace session memory
- store raw hidden chain-of-thought
- store secrets
- upload logs anywhere
- use cloud tracing by default

Final boundary rule:
Graphiti = long-term memory used by SHAMSU.
Codebase-Memory MCP = codebase facts used by SHAMSU.
Haystack/context pipeline = builds model context.
ActionLedger = local debug/audit log for humans only.

============================================================

1. STORAGE LAYOUT
   ============================================================

Store run logs separately under:

<workspace></workspace>/.shamsu/runs/<run-id></run>/

Suggested files:

manifest.json
events.jsonl
decisions.jsonl
tool-calls.jsonl
model-calls.jsonl
commands/
diagnostics/
mutations/
context-preview.json
final-output.md
summary.json

Do not store these inside:

- <workspace></workspace>/.shamsu/memory/
- <workspace></workspace>/.shamsu/abstract/
- Graphiti
- Codebase-Memory MCP storage

Each run-id should be unique and sortable, for example:

run_2026-07-06_14-31-22_ab12

============================================================
2. WHAT TO LOG
==============

Log a structured event timeline.

Event types should include:

- run_started
- run_finished
- run_failed
- user_prompt_received
- task_classified
- memory_status_checked
- graphiti_retrieved
- code_memory_queried
- diagnostics_parsed
- context_pack_built
- planner_model_called
- coder_model_called
- model_response_received
- decision_recorded
- tool_called
- tool_finished
- file_read
- file_write_requested
- patch_planned
- patch_applied
- mutation_started
- mutation_finished
- mutation_failed
- command_started
- command_finished
- verification_started
- verification_passed
- verification_failed
- rollback_performed
- final_response_written

Every important action should create an event.

Example event:

{
  "event_id": "evt_0008",
  "run_id": "run_2026-07-06_14-31-22_ab12",
  "type": "command_finished",
  "timestamp": "2026-07-06T14:32:10+06:00",
  "command": "npm run build",
  "cwd": "/path/to/workspace",
  "exit_code": 2,
  "stdout_path": "commands/cmd_003.stdout.log",
  "stderr_path": "commands/cmd_003.stderr.log",
  "diagnostics_path": "diagnostics/error_packet_003.json"
}

============================================================
3. DECISION LOGGING
===================

SHAMSU should log decision summaries, not raw hidden chain-of-thought.

Create DecisionRecord.

Suggested fields:

- decision_id
- run_id
- timestamp
- decision
- reason_summary
- evidence
- alternatives_considered
- chosen_action
- confidence
- outcome

Example:

{
  "decision": "run_build_after_patch",
  "reason_summary": "The patch changed TypeScript source files, so verification is required before claiming success.",
  "evidence": [
    "changed_files: client/src/game/loop.ts",
    "policy: code mutations require verification"
  ],
  "alternatives_considered": [
    "skip verification",
    "run full test suite"
  ],
  "chosen_action": "npm run build",
  "confidence": 0.86,
  "outcome": "verification_failed"
}

Do NOT store raw private reasoning or hidden chain-of-thought.

============================================================
4. RAW OUTPUT HANDLING
======================

Do not put huge logs directly inside events.jsonl.

For command outputs:

- save stdout/stderr as files under commands/
- reference paths from events.jsonl
- save compact summaries in events

Example:

commands/
  cmd_001.stdout.log
  cmd_001.stderr.log

For diagnostics:

- save ErrorPacket JSON under diagnostics/
- reference it from events

For mutations:

- reference PatchEngine transaction ids and paths
- do not duplicate full backups in ActionLedger

For model calls:

- log metadata and safe previews
- save full prompt/response only if config allows it
- redact secrets before writing

============================================================
5. CONTEXT PREVIEW
==================

When SHAMSU builds context for planner/coder, save a safe preview:

context-preview.json

It should include:

- task type
- selected memories count
- selected code facts count
- selected files/snippets
- diagnostic packet ids
- token estimate if available
- redacted prompt preview

It must NOT automatically become memory.
It must NOT be fed back into future model calls unless the user explicitly asks to inspect/reuse a run.

============================================================
6. PRIVACY AND REDACTION
========================

Before writing logs, redact secrets.

Redact:

- API keys
- passwords
- tokens
- private keys
- .env values
- database URLs with credentials
- authorization headers
- SSH keys
- cloud credentials

Do not log raw contents of secret files by default:

- .env
- .env.local
- id_rsa
- id_ed25519
- credentials.json
- service-account files

Add a redaction utility used by:

- event logging
- command logging
- model call logging
- context preview logging
- final output logging

============================================================
7. INTEGRATION POINTS
=====================

Integrate ActionLedger with:

1. AgentChatLoop

- start run on user prompt
- log task classification
- log final response

2. AgentOrchestrator

- log workflow selection
- log decisions
- log model calls

3. CommandRunner

- log command start/end
- save stdout/stderr
- log exit code
- connect diagnostics output

4. DiagnosticDigest

- log parser used
- log ErrorPacket path
- log root diagnostics summary

5. PatchEngine

- log mutation transaction id
- log files touched
- log patch status
- log rollback availability

6. Codebase-Memory MCP adapter

- log query type
- log files/symbols queried
- log result count
- do not dump huge code facts unless debug mode enables it

7. Graphiti adapter

- log retrieval/write event metadata
- do not duplicate all memories into ActionLedger unless debug mode enables safe preview

8. Context pipeline

- log context-pack creation
- save safe context preview

============================================================
8. CLI COMMANDS
===============

Add commands:

/runs
/run last
/run show <run-id></run>
/run timeline <run-id></run>
/run decisions <run-id></run>
/run tools <run-id></run>
/run commands <run-id></run>
/run context <run-id></run>
/run diff <run-id></run>
/run export <run-id></run>
/run clean

Behavior:

/runs

- list recent runs
- show run id, time, prompt preview, status

/run last

- show latest run summary

/run show <run-id></run>

- show manifest and summary

/run timeline <run-id></run>

- show chronological events

/run decisions <run-id></run>

- show decision summaries

/run tools <run-id></run>

- show tool calls and outcomes

/run commands <run-id></run>

- show commands, exit codes, stdout/stderr paths

/run context <run-id></run>

- show safe context preview

/run diff <run-id></run>

- show patches/mutation references from that run

/run export <run-id></run>

- export run folder to a zip or markdown report
- redact secrets

/run clean

- clean old runs after confirmation
- support retention config

============================================================
9. CONFIG
=========

Add config options:

action_ledger.enabled = true
action_ledger.log_model_prompts = false by default
action_ledger.log_model_responses = true
action_ledger.log_context_preview = true
action_ledger.max_inline_event_size = small limit
action_ledger.retention_days = configurable
action_ledger.redact_secrets = true
action_ledger.debug_full_trace = false by default

Default should be safe and useful:

- detailed event timeline
- command outputs saved
- diagnostics saved
- mutations referenced
- no raw hidden reasoning
- no unredacted secrets
- no cloud upload

============================================================
10. TESTS
=========

Add tests for:

Storage:

1. creates run directory
2. writes manifest.json
3. appends events.jsonl
4. writes summary.json
5. run ids are unique and sortable

Events:
6. logs run_started and run_finished
7. logs user prompt received
8. logs task classification
9. logs tool call start/end
10. logs command start/end
11. logs diagnostics parsed
12. logs patch/mutation events
13. logs final output

Decision records:
14. decision summary is saved
15. raw chain-of-thought is not saved
16. evidence/chosen action/outcome are saved

Command logs:
17. stdout/stderr are saved as separate files
18. events reference stdout/stderr paths
19. huge logs are not embedded into events.jsonl

Redaction:
20. API keys are redacted
21. .env values are redacted
22. private keys are redacted
23. authorization headers are redacted

Boundaries:
24. ActionLedger does not write to Graphiti
25. ActionLedger is not used as memory
26. ActionLedger is not automatically added to model context
27. ActionLedger storage is separate from .shamsu/memory and .shamsu/abstract

CLI:
28. /runs lists recent runs
29. /run last shows latest run
30. /run timeline shows events
31. /run decisions shows decision summaries
32. /run commands shows command outcomes
33. /run context shows safe context preview
34. /run export creates redacted export
35. /run clean asks for confirmation

Integration:
36. AgentChatLoop starts and finishes a run
37. CommandRunner writes command events
38. DiagnosticDigest writes diagnostic events
39. PatchEngine writes mutation events
40. failed run gets run_failed event

============================================================
FINAL REQUIREMENTS
==================

Do not implement Graphiti.
Do not implement Codebase-Memory MCP.
Do not implement PatchEngine.
Do not implement DiagnosticDigest.
Do not implement Haystack/context pipeline.
Only add ActionLedger integration points and safe no-op hooks where needed.

Do not use cloud tracing.
Do not upload logs.
Do not store raw hidden chain-of-thought.
Do not mix ActionLedger with memory.
Do not automatically feed ActionLedger into model context.
Do not store secrets unredacted.

Deliverables:

1. Inspect current SHAMSU session/logging flow.
2. Implement ActionLedger / DebugTrace package.
3. Add run storage under <workspace></workspace>/.shamsu/runs/.
4. Add structured event logging.
5. Add decision-summary logging.
6. Add redaction utilities.
7. Integrate with AgentChatLoop, CommandRunner, DiagnosticDigest, PatchEngine, Codebase-Memory, Graphiti, and context pipeline through clean hooks.
8. Add /run and /runs commands.
9. Add tests.
10. Run targeted tests.
11. Summarize changes and limitations.

Final rule:
ActionLedger is a local human-facing debug/audit system only. It records what SHAMSU did for each prompt, but it is not memory, not retrieval, and not automatic model context.
