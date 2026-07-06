
You are working inside the SHAMSU repo.

Task:
Implement SHAMSU’s deterministic Diagnostic/Error Digest layer using proven external/local tools where possible.

Goal:
SHAMSU should NOT send raw build/test/runtime logs directly to the LLM first.

Instead:

raw command output
→ existing local diagnostic parser/compactor tools
→ normalized ErrorPacket
→ Codebase-Memory MCP lookup
→ targeted snippets
→ planner/coder model gets only relevant context

This should improve bug fixing accuracy, reduce tokens, and prevent the model from guessing from giant noisy logs.

Important:
Do NOT build a full custom log parser system from scratch.
Do NOT use the LLM as the first log parser.
Do NOT send huge raw logs to the model by default.
Do NOT replace Codebase-Memory MCP.
Do NOT replace Graphiti.
Do NOT replace Haystack/context pipeline if already planned.
Do NOT upload logs/code/errors anywhere.

Correct architecture:
SHAMSU is the orchestrator/glue/safety layer.
External/local tools handle parsing, normalization, and compaction where possible.

Use this direction:

- native JSON/SARIF/test outputs first
- reviewdog/errorformat-style parsing for compiler/linter text
- SARIF-like normalized internal diagnostic records
- Drain3-style runtime log compaction for noisy logs
- LLMLingua-style compression later only for prose/logs, not exact code/errors
- small fallback regex parsers only for gaps

Scope:
Implement ONLY the diagnostics/error parsing and compaction layer in this pass.
Do NOT implement full context engineering in this pass.
Do NOT implement Graphiti in this pass.
Do NOT reimplement Codebase-Memory MCP in this pass.
Do NOT replace existing CommandRunner or workflows.

This should be implemented after the Codebase-Memory MCP / abstract implementation exists.

============================================================

1. REQUIRED BEHAVIOR
   ============================================================

Whenever SHAMSU runs a command such as:

- npm run build
- npm test
- npm run dev
- tsc
- vite
- eslint
- pytest
- python script.py
- ruff
- go test
- cargo test
- django test

SHAMSU should capture stdout/stderr and run it through DiagnosticDigest before sending anything to the model.

DiagnosticDigest should:

1. detect the tool/language from command and output
2. try native structured output first when available
3. use external/local parsing helpers when available
4. fall back to small deterministic parsers only when needed
5. extract file paths, line numbers, columns, error codes, symbols, modules
6. group repeated errors
7. remove noisy boilerplate
8. identify likely root diagnostics
9. produce a compact ErrorPacket
10. query Codebase-Memory MCP for related code facts
11. recommend target files/snippets to inspect
12. feed the AI compact facts, not raw logs

============================================================
2. EXTERNAL TOOL POLICY
=======================

SHAMSU should rely on existing tools where possible.

Preferred tools/concepts:

1. Native structured output
   Use native structured outputs when supported by the tool.

Examples:

- eslint JSON
- ruff JSON
- go test -json
- cargo JSON diagnostics
- pytest JSON/JUnit output if available
- SARIF output if a tool already supports it

2. reviewdog/errorformat-style parsing
   Use reviewdog/errorformat-style parsing for compiler/linter text output.

Use it for patterns like:

- file:line:column: message
- file:line: message
- file(line,column): error CODE: message
- common linter/compiler diagnostics

Do not build a full custom errorformat engine if an existing local package/tool can do it.

3. SARIF-like normalization
   Use a SARIF-like internal model for diagnostics:

- tool
- rule/error code
- severity
- message
- file/location
- related locations
- stack frames
- category

A full SARIF implementation is not required in the first version unless it is easy.
But the internal model should be compatible with SARIF-style concepts.

4. Drain3-style runtime log compaction
   Use Drain3 or a similar local log-template miner for noisy runtime logs.

Use this for:

- dev server spam
- backend request logs
- browser console noise
- repeated warnings
- repeated stack traces
- repeated runtime events

Do NOT use Drain3 for exact compiler diagnostics where line numbers, symbols, and error codes matter.

5. LLMLingua-style compression later
   LLMLingua or similar compression may be added later only for:

- long natural-language logs
- old session summaries
- repeated prose
- noisy non-code output

Do NOT use prompt compression for:

- exact code snippets
- diffs
- TypeScript errors
- stack trace root frames
- import/export names
- file paths
- line numbers
- command exit codes

Final policy:
Use proven external/local tools first.
SHAMSU builds adapters, routing, normalization, and safety checks.
SHAMSU does not reinvent parser algorithms unless a tiny fallback parser is necessary.

============================================================
3. SHAMSU-MANAGED TOOL SETUP
============================

Add setup/doctor support for diagnostic helper tools.

Suggested managed tool/cache layout:
~/.shamsu/tools/diagnostics/

Workspace diagnostic metadata:
<workspace></workspace>/.shamsu/diagnostics/

Suggested files:

- status.json
- config.json
- last-error-packet.json
- diagnostic-events.jsonl

Do not install tools globally.
Do not use sudo/admin installs.
Do not clone tools into target user projects.
Do not upload logs anywhere.

Doctor should check:

- native structured-output support where applicable
- reviewdog/errorformat helper availability if configured
- Drain3 availability if configured
- optional LLMLingua availability if enabled
- diagnostics config validity

If optional diagnostic helpers are missing:

- show clear repair/setup instructions
- do not silently use the LLM first
- use deterministic fallback parsers where safe

Add commands:

- /diagnostics setup
- /diagnostics repair
- /diagnostics status

============================================================
4. NORMALIZED TYPES
===================

Create a normalized DiagnosticRecord type.

Suggested fields:

- tool
- language
- severity
- code
- category
- message
- file
- line
- column
- symbol
- module
- stack_frame
- related_locations
- raw_excerpt
- parser_name
- confidence

Create an ErrorPacket type.

Suggested fields:

- command
- cwd
- exit_code
- tool
- parser_chain
- summary
- root_diagnostics
- secondary_diagnostics
- repeated_noise_removed
- target_files
- target_symbols
- related_code_facts
- recommended_snippets
- compact_log
- raw_log_path
- created_at

The raw log should remain saved in session logs.
The model should receive ErrorPacket by default.

============================================================
5. PACKAGE STRUCTURE
====================

Create diagnostics package:

shamsu/diagnostics/
  __init__.py
  digest.py
  types.py
  compact.py
  normalize.py
  root_cause.py
  setup.py
  doctor.py
  adapters/
    __init__.py
    native_json.py
    reviewdog_errorformat.py
    sarif.py
    drain3_compactor.py
    llmlingua_optional.py
  parsers/
    __init__.py
    typescript_fallback.py
    python_fallback.py
    pytest_fallback.py
    node_runtime_fallback.py
    generic_fallback.py

Important:
The parsers folder is only for small fallback parsers.
The first choice should be external/native structured parsing.

============================================================
6. PARSING ORDER
================

DiagnosticDigest must use this order:

1. Native structured output
2. SARIF output if available
3. reviewdog/errorformat-style parser
4. tool-specific fallback parser
5. generic fallback parser
6. Drain3/noisy-log compaction for runtime logs
7. LLM only as a last-resort explanation step, never as first parser

Do not use LLM before deterministic parsing.

============================================================
7. FIRST LANGUAGES / TOOLS TO SUPPORT
=====================================

Prioritize JavaScript/TypeScript because SHAMSU’s current failures are there.

Support:

A. TypeScript / tsc errors

Example:
src/game/rules.ts(71,17): error TS1005: ')' expected.

Extract:

- file = src/game/rules.ts
- line = 71
- column = 17
- code = TS1005
- message = ')' expected
- category = syntax_error

B. TypeScript import/export errors

Examples:
Module '"./rules"' has no exported member 'World'.
Module '"./loop"' has no exported member named 'GameLoop'. Did you mean 'gameLoop'?
The requested module '/src/game/loop.ts' does not provide an export named 'GameLoop'

Extract:

- missing symbol
- module path
- suggested symbol if present
- category = missing_export / import_export_mismatch

C. Vite/browser runtime module errors

Example:
Uncaught SyntaxError: The requested module '/src/game/loop.ts' does not provide an export named 'GameLoop'

Extract:

- file/module path
- missing export
- category = runtime_missing_export

D. ESLint

Prefer JSON formatter.
Fallback to errorformat/generic parser.

E. Python tracebacks

Extract:

- final exception type
- final exception message
- relevant user-code frames
- file/line/function
- category

F. Pytest failures

Prefer structured/JUnit/JSON output when available.
Fallback parser should extract:

- failing test file
- failing test name
- assertion message
- traceback user-code frame

G. Generic compiler format

Support:

- path/to/file:line:column: message
- path/to/file:line: message

============================================================
8. ROOT CAUSE HEURISTICS
========================

Add root cause selection.

Rules:

1. Prefer first real compiler error over npm boilerplate.
2. Prefer user workspace files over node_modules/vendor files.
3. Prefer syntax errors before downstream type errors.
4. Prefer missing export/import errors before cascading missing symbol errors.
5. Group identical diagnostics.
6. Remove repeated npm lifecycle boilerplate.
7. For TypeScript, group by:
   - code
   - file
   - line
   - message
8. If many errors come from one file, prioritize that file.
9. If one import/export mismatch causes many errors, mark it root.
10. Preserve exact file paths, symbols, line numbers, and error codes.

Example:
If errors mention:

- session.ts imports GameLoop
- loop.ts exports gameLoop

Then root diagnostic should be:
missing export GameLoop from loop.ts

Do not let compaction rewrite this into vague prose.

============================================================
9. CODEBASE-MEMORY MCP INTEGRATION
==================================

After creating ErrorPacket, query Codebase-Memory MCP.

For diagnostics with file/symbol/module:

- get exports of exporter file
- get imports of importer file
- get who-uses target file
- get references to missing symbol
- get similar symbols if available
- get impact of editing target file

For import/export errors:

1. resolve importer file
2. resolve exporter module path
3. query exporter exports
4. query importer imports
5. add recommended fix strategy:
   - alias export if case mismatch or similar symbol exists
   - update importer only if safe and limited
   - preserve existing public exports

If Codebase-Memory MCP is required by current SHAMSU design and is unavailable:

- report the failure clearly
- do not fake code facts
- do not continue normal code-agent bugfix mode unless existing startup rules allow it

============================================================
10. TARGETED SNIPPET SELECTION
==============================

DiagnosticDigest should recommend snippets, not read huge files.

For each root diagnostic:

- include file path
- line window, e.g. line ±30
- include related importer/exporter file if import/export error

Examples:

- rules.ts line 71 syntax error → read rules.ts lines 41-101
- session.ts imports GameLoop from loop.ts → read session.ts import lines and loop.ts export lines

Do not send full files unless necessary.

============================================================
11. COMMANDRUNNER INTEGRATION
=============================

Integrate with SHAMSU’s existing CommandRunner.

After every command:

1. capture stdout/stderr
2. save raw output to session log as already done
3. run DiagnosticDigest
4. save ErrorPacket to session
5. show compact diagnostic summary in CLI
6. pass ErrorPacket to bugfix/error_feedback_loop workflows

Do not remove raw logs.
Raw logs should remain available in session logs.
But model context should prefer ErrorPacket.

============================================================
12. BUGFIX WORKFLOW INTEGRATION
===============================

Update bugfix/error feedback loop.

Before asking the model to fix:

1. parse latest command output into ErrorPacket
2. use ErrorPacket root diagnostics
3. get related Codebase-Memory facts
4. read recommended snippets
5. build bugfix prompt from:
   - exact user request
   - ErrorPacket
   - code facts
   - targeted snippets
   - edit constraints
6. do not send full raw logs unless needed

After patch:

1. run verification command
2. parse output again
3. compare old/new ErrorPacket
4. continue if diagnostics changed or decreased
5. stop if same root diagnostic repeats with same file hash/patch

============================================================
13. CLI COMMANDS
================

Add commands:

/diagnostics setup
/diagnostics repair
/diagnostics status
/diagnostics last
/diagnostics parse
/diagnostics explain
/diagnostics sources

Behavior:

/diagnostics status

- show available diagnostic helpers
- show parser/compactor availability
- show latest ErrorPacket path
- show whether fallback parsers are being used

/diagnostics setup

- install/configure local diagnostic helpers under SHAMSU-managed tools path
- no global installs
- no cloud services

/diagnostics repair

- rerun diagnostic helper checks
- repair local config if possible
- print manual steps if needed

/diagnostics last

- show last ErrorPacket summary
- root diagnostics
- target files
- recommended snippets

/diagnostics parse

- parse latest command output again

/diagnostics explain

- explain deterministic root cause selection without chain-of-thought

/diagnostics sources

- show which parser/helper handled the latest log

============================================================
14. VISIBLE PROGRESS
====================

When commands fail, show compact progress.

Example:

Build failed. Parsing diagnostics...

Root diagnostic:

- TS2305 missing export GameLoop
- importer: client/src/session.ts
- module: client/src/game/loop.ts

Related code facts:

- loop.ts exports gameLoop
- session.ts imports GameLoop

Next:

- inspect session.ts import
- inspect loop.ts exports
- prefer alias export if safe

Do not show raw huge logs unless user asks.

============================================================
15. SAFETY
==========

Never store or display secrets in diagnostics:

- API keys
- passwords
- tokens
- private keys
- .env values
- credentials

Redact secrets in:

- compact logs
- ErrorPacket
- CLI display
- model context

Do not expose chain-of-thought.
Do not use LLM to parse logs before deterministic parsers run.
Do not bypass CommandRunner.
Do not break session logging.
Do not upload logs/code/errors.
Do not use remote diagnostic services.

============================================================
16. TESTS
=========

Use fake Codebase-Memory adapter in tests.
Do not require real Codebase-Memory MCP in unit tests.
Do not require real external diagnostic tools in unit tests unless integration tests are explicitly marked.

Add tests:

External tool policy:

1. native structured output is preferred over fallback parser
2. reviewdog/errorformat adapter is used when configured
3. fallback parser is used only when external parser is unavailable
4. Drain3 compaction is used only for noisy runtime logs
5. LLMLingua compression is disabled by default
6. exact diagnostics are never compressed with LLMLingua

TypeScript parser/fallback:
7. parses tsc file(line,column) errors
8. extracts TS error code
9. extracts syntax error category
10. extracts missing export symbol
11. extracts “Did you mean” suggestion
12. parses browser missing export runtime error

Python/parser fallback:
13. parses final exception type/message
14. extracts user-code traceback frame

Generic parser:
15. parses file:line:column format
16. ignores npm boilerplate

Root cause:
17. syntax error prioritized before cascade errors
18. missing export grouped as root cause
19. repeated errors deduped
20. node_modules/vendor frames deprioritized
21. exact file paths/symbols/error codes are preserved

ErrorPacket:
22. command output converts to compact ErrorPacket
23. raw log is stored separately
24. compact log removes repeated npm lifecycle noise
25. ErrorPacket includes parser_chain
26. ErrorPacket includes raw_log_path

Workflow:
27. CommandRunner calls DiagnosticDigest after command
28. bugfix workflow uses ErrorPacket before LLM
29. Codebase-Memory lookup is called for import/export errors
30. targeted snippets are recommended from file/line
31. repeated same ErrorPacket triggers stall guard

CLI:
32. /diagnostics status shows helper availability
33. /diagnostics last shows latest packet
34. /diagnostics sources shows parser/helper name

Safety:
35. secrets are redacted
36. raw huge logs are not sent to the model by default
37. LLM is not called before deterministic parsing

============================================================
FINAL REQUIREMENTS
==================

Do not rewrite the whole project.
Do not replace CommandRunner.
Do not replace Codebase-Memory MCP.
Do not implement Graphiti.
Do not implement full Context Engineering yet.
Do not build a full custom parser engine.
Do not use LLM first for error parsing.
Do not use cloud APIs.
Do not upload logs/code.
Do not send raw huge logs to the model by default.
Do not compress exact code/error truth.

Deliverables:

1. Explain current SHAMSU command-output/bugfix flow.
2. Explain which existing local tools/helpers will be used for diagnostics.
3. Implement DiagnosticDigest and ErrorPacket.
4. Add adapters for native structured output, reviewdog/errorformat-style parsing, SARIF-like normalization, and Drain3-style log compaction.
5. Add small fallback parsers only for gaps.
6. Integrate with CommandRunner and bugfix workflow.
7. Add /diagnostics commands.
8. Add tests.
9. Run targeted tests.
10. Summarize exactly what changed and remaining limitations.

Final rule:
SHAMSU must use existing local diagnostic parsing/compaction tools first, then normalize results into a compact ErrorPacket. The AI should receive focused diagnostics plus relevant code facts/snippets, not giant raw logs and not guessed parser output.
