# SHAMSU Requirements Specification

> **SHAMSU — Killer of API Bills from the Big Giants**  
> A local-first, sub-8GB, Claude Code–style autonomous software engineering agent that understands, edits, audits, fixes, and generates software projects using smart indexing, context engineering, safe tool execution, and small local models.

---

## Document Status

**This is the specification, not a description of the shipped system.**

- Written before implementation; most of it still holds as intent.
- Sections marked **`[SPEC-ONLY]`** are not built.
- Sections marked **`[UPDATED]`** were factually wrong against the code and have
  been corrected in place.
- Section 0 below maps every requirement group to what actually exists.
- For behavior that is built but unreliable, read `CURRENT_STATE.md` — a
  requirement can be implemented and still fail in practice.

Conformance last verified against the code on **2026-07-20**, version `0.4.0b1`.
When this file and the code disagree, the code wins and this file is the bug.

---

## Table of Contents

0. [Implementation Conformance](#0-implementation-conformance)

1. [Project Identity](#1-project-identity)
2. [Project Description](#2-project-description)
3. [Problem Statement](#3-problem-statement)
4. [Project Vision](#4-project-vision)
5. [Goals and Objectives](#5-goals-and-objectives)
6. [Target Users](#6-target-users)
7. [Target Device Profile](#7-target-device-profile)
8. [System Scope](#8-system-scope)
9. [Core Concept](#9-core-concept)
10. [Functional Requirements](#10-functional-requirements)
11. [Safety and Security Requirements](#11-safety-and-security-requirements)
12. [Non-Functional Requirements](#12-non-functional-requirements)
13. [System Architecture](#13-system-architecture)
14. [Agent Architecture](#14-agent-architecture)
15. [Context Engineering Strategy](#15-context-engineering-strategy)
16. [PRD and Document Processing](#16-prd-and-document-processing)
17. [Codebase Indexing and Retrieval](#17-codebase-indexing-and-retrieval)
18. [Project Generation Workflow](#18-project-generation-workflow)
19. [Code Editing Workflow](#19-code-editing-workflow)
20. [Long-Running Task Support](#20-long-running-task-support)
21. [Multi-Model Strategy](#21-multi-model-strategy)
22. [MCP and Skills Support](#22-mcp-and-skills-support)
23. [Web Search and External Knowledge](#23-web-search-and-external-knowledge)
24. [Command Execution Policy](#24-command-execution-policy)
25. [Data Storage Requirements](#25-data-storage-requirements)
26. [Logging Requirements](#26-logging-requirements)
27. [MVP Scope](#27-mvp-scope)
28. [Future Scope](#28-future-scope)
29. [Success Criteria](#29-success-criteria)
30. [Glossary](#30-glossary)

---

# 0. Implementation Conformance

Status key: **Done** = built and behaving · **Partial** = built, with a known
gap or model-bounded quality · **Spec-only** = not built.

## 0.1 Functional Requirements

| Req | Status | Where / note |
|---|---|---|
| FR-1 CLI interface | Done | `shamsu/cli/repl.py`, launcher at `~/.shamsu/bin`, plus headless `shamsu run` |
| FR-2 Project loading | Done | `--workspace`, `resolve_workspace()` |
| FR-3 Workspace scope enforcement | Done | `shamsu/safety/` sandbox |
| FR-4 Project indexing | Done | `indexer/walker.py`, SQLite + FTS5 |
| FR-5 Ignore unnecessary folders | Done | walker ignore rules |
| FR-6 Non-LLM code search | Done | FTS5 + re-ranking in `retriever/` |
| FR-7 Codebase Q&A | Partial | works; slow on trivial reads (~20s), and routing sometimes lands in the wrong lane |
| FR-8 Code explanation | Done | `agents/qa_workflow.py` |
| FR-9 Code editing | Partial | `agents/code_edit_workflow.py`; diff quality is bounded by the coder model |
| FR-10 Patch-based modification | Done | `patch/engine.py`, full-rewrite fallback on malformed diffs only |
| FR-11 File creation | **Partial — known bug** | works via `write_file`, but simple creation prompts can misroute to the PRD build lane |
| FR-12 File deletion protection | Done | `file_delete` is never auto-approvable |
| FR-13 Bug fixing | Partial | `agents/bugfix_workflow.py` + traceback location parsing |
| FR-14 Code audit | Done | `agents/audit_workflow.py` |
| FR-15 Test generation | Done | `agents/test_generation_workflow.py` |
| FR-16 Test execution | Done | `tools/executor.py` behind approval |
| FR-17 Documentation generation | Done | `agents/doc_workflow.py` |
| FR-18 PRD input | Done | Markdown, TXT, PDF |
| FR-19 Non-LLM PRD parsing | Done | `prd/parser.py`, `prd/extractor.py` |
| FR-20 PRD-to-project planning | Done | `prd/project.py`, preview + approval |
| FR-21 Autonomous project generation | Partial | Django pipeline solid; template scaffolds disabled by default (`SHAMSU_ENABLE_TEMPLATES=1`) |
| FR-22 Task decomposition | Done | `tasks/`, `taskmaster/`, plan mode |
| FR-23 Progress tracking | Done | `MilestoneTask` at `.shamsu/tasks/<id>.json` |
| FR-24 Resume support | Done | sessions + generation state + plan state |
| FR-25 Natural prompt understanding | **Partial — weakest area** | `_ROUTE_RULES` in `cli/repl.py`; misroutes are the top open bug class |
| FR-26 Tool calling | Done | `tools/agent_tools.py` + `llm/output.py` salvage for models without native tools |
| FR-27 Git awareness | Done | `tools/git.py`, read-only inspect + gated mutations |
| FR-28 Error handling | Done | `diagnostics/`, `repair/`, swallowed-error recording |

## 0.2 Safety Requirements

| Req | Status | Note |
|---|---|---|
| SR-1 Workspace sandbox | Done | |
| SR-2 Sensitive path blocking | Done | |
| SR-3 Dangerous command blocking | Done | |
| SR-4 Command allowlist/denylist | Done | `safety/commands.py` |
| SR-5 Confirmation for risky actions | **Partial — known bug** | approval machinery works, but "do not change files" is not enforced as a hard gate; under broad approval the agent has mutated files against an explicit read-only instruction |
| SR-6 Patch preview | Done | `patch/preview.py` |
| SR-7 Command preview | Done | |
| SR-8 Secret detection | Done | redaction in logs, summaries, exports |
| SR-9 No source code leakage | Done | |
| SR-10 MCP safety | Spec-only | MCP itself is not built |
| SR-11 Audit trail | Done | `action_ledger/` + `audit/`; every run writes a full artifact bundle |

## 0.3 Non-Functional Requirements

| Req | Status | Evidence |
|---|---|---|
| NFR-1 Low memory | Done | peak RSS 90.9 MB (`RELEASE_VALIDATION.md`) |
| NFR-2 Offline-first | Done | local Ollama only; web is opt-in and gated |
| NFR-3 Local privacy | Done | |
| NFR-4 Reasonable performance | Partial | startup 1.27s, warm answer 0.31s — but model-backed reads take ~20s |
| NFR-5 Modularity | Done | 201 modules, package-per-concern |
| NFR-6 Maintainability | Done | 1418 tests, ruff clean |
| NFR-7 Reliability | **Not met yet** | 4 of 7 real dogfood prompts failed on 2026-07-20 |
| NFR-8 Usability | Partial | |
| NFR-9 Transparency | Done | run artifacts, `/runs`, `/run show`, decisions log |
| NFR-10 Extensibility | Partial | registry + template system exists; MCP does not |

## 0.4 Sections That Are Spec-Only

- §22.1–22.2 **General MCP support** — not built. There is no MCP client, server,
  or tool registry. Two *specific* external tools are integrated by shelling out
  to their documented CLI mode, not over MCP transport:
  `tools/codebase_memory.py` (codebase-memory-mcp) and `taskmaster/adapter.py`
  (task-master). Both are optional and locally installed.
- §22.3 **Skills system** — `shamsu/skills/` is an empty `__init__.py`. No skill
  loading, discovery, or execution exists.
- §28 **Future scope** — unchanged, all still future.

---

# 1. Project Identity

## 1.1 System Name

**SHAMSU**

## 1.2 Full Name

**SHAMSU — Killer of API Bills from the Big Giants**

## 1.3 Tagline

> **Build, fix, audit, and ship software locally — without burning money on cloud AI APIs.**

## 1.4 Short Description

SHAMSU is a local-first autonomous coding agent designed for low-resource devices. It works like a lightweight Claude Code–style CLI assistant that can read requirement documents, understand existing codebases, plan software tasks, edit code, fix bugs, run tests, generate documentation, and create complete starter projects from PRDs.

Unlike cloud-based coding agents, SHAMSU is designed to run on machines with less than 8 GB RAM by using small quantized local models, non-LLM code indexing, smart retrieval, context compression, and strict safety controls.

---

# 2. Project Description

Modern AI coding assistants are powerful, but most of them depend on cloud APIs, paid subscriptions, large context windows, and continuous internet access. This creates several problems for students, indie developers, privacy-conscious users, and low-resource teams.

SHAMSU aims to solve this problem by building a local autonomous software engineering agent that can run on end-user devices without depending on expensive cloud AI services.

The system uses a divide-and-conquer architecture. Instead of forcing a small LLM to read an entire codebase, SHAMSU uses traditional software engineering tools to index, search, parse, rank, and retrieve only the most relevant parts of the project. The LLM is then used only for tasks where language understanding, reasoning, explanation, or code generation is required.

This makes SHAMSU practical for sub-8GB devices while still supporting useful software engineering tasks such as code edits, audits, bug fixes, testing, documentation, and project generation from requirement documents.

---

# 3. Problem Statement

Cloud-based AI coding tools are often expensive, internet-dependent, and privacy-sensitive. Developers may need to send source code, project structure, prompts, logs, and requirement documents to third-party servers. This is not always acceptable for academic, personal, commercial, or confidential projects.

At the same time, local devices with limited RAM cannot run large AI models or load entire codebases into the model context. A normal local LLM approach becomes slow, inaccurate, and memory-heavy when the project contains thousands or millions of lines of code.

Therefore, there is a need for a lightweight local coding agent that can:

- Run on sub-8GB devices.
- Work offline after setup.
- Avoid cloud AI API costs.
- Keep project code private.
- Understand large codebases without loading everything into the LLM.
- Safely edit, test, audit, and generate code.
- Process requirement documents and create software projects from them.

SHAMSU addresses this problem through context engineering, non-LLM indexing, retrieval-based code understanding, task decomposition, safe tool execution, and small local models.

---

# 4. Project Vision

The vision of SHAMSU is to make autonomous software engineering assistance accessible to developers who do not have powerful GPUs, large RAM, paid API credits, or cloud-based coding subscriptions.

SHAMSU should feel like a local coding teammate. The user should be able to give natural prompts such as:

```text
Fix the login bug.
Audit this project.
Add JWT authentication.
Generate a todo app from this PRD.
Explain how the backend works.
Create tests for the payment module.
```

The system should then plan, search, retrieve, generate, edit, validate, and report progress while remaining within strict safety boundaries.

---

# 5. Goals and Objectives

## 5.1 Primary Goal

To build a local CLI-based autonomous coding agent that can perform software engineering tasks on low-resource devices using smart indexing, retrieval, context engineering, and small local LLMs.

## 5.2 Objectives

The system aims to:

1. Provide a natural-language CLI interface.
2. Run on devices with less than 8 GB RAM.
3. Work offline after initial setup.
4. Load and index local software projects.
5. Search large codebases without using LLM context.
6. Parse PRDs and requirement documents.
7. Break large tasks into smaller subtasks.
8. Generate complete starter projects from requirements.
9. Edit existing code safely using patch-based modifications.
10. Fix simple bugs and explain the changes.
11. Audit projects for bugs, code smells, and security issues.
12. Generate documentation and tests.
13. Run approved terminal commands safely.
14. Maintain progress logs for long-running tasks.
15. Support a modular agent architecture.
16. Support MCP-style tools and reusable skills.
17. Support optional web search for documentation and wiki references.
18. Support sequential multi-model execution where only one model is active at a time.

---

# 6. Target Users

SHAMSU is designed for:

- Students working on academic software projects.
- Beginner developers learning project structure and debugging.
- Indie developers who want AI coding help without API bills.
- Developers using low-end laptops.
- Privacy-conscious users who do not want to send code to cloud services.
- Teams working in offline or limited-internet environments.
- Developers who want a local coding assistant for small to medium projects.

---

# 7. Target Device Profile

SHAMSU is designed for **sub-8GB devices**.

## 7.1 Minimum Target

- RAM: 4 GB to 8 GB
- CPU: Low-end or mid-range consumer CPU
- GPU: Not required
- Internet: Optional after setup
- Storage: Local disk storage for indexes, logs, and models
- Interface: CLI

## 7.2 Recommended Target

- RAM: 8 GB
- CPU: Modern laptop CPU
- GPU: Optional
- OS: Linux, macOS, or Windows
- Runtime: Local LLM runtime such as llama.cpp or Ollama

**`[UPDATED]`** As implemented, the runtime is **Ollama only**, on
`localhost:11434`. llama.cpp is not wired up. Hardware maps to the three model
tiers in §21.1: `light` for 8 GB CPU-only, `default` for the 8 GB cookbook,
`heavy` for 16 GB+.

## 7.3 Design Constraints for Low Memory

The system shall avoid:

- Running large models.
- Keeping multiple models active at the same time.
- Loading entire codebases into memory.
- Loading entire projects into LLM context.
- Running heavy GUI frameworks.
- Using large always-on vector databases.
- Performing unnecessary background tasks.

---

# 8. System Scope

## 8.1 In Scope

SHAMSU shall support:

- CLI-based interaction.
- Local project loading.
- Project indexing.
- Code search and retrieval.
- Smart context building.
- PRD parsing.
- Task planning.
- Code generation.
- Code editing.
- Bug fixing.
- Code auditing.
- Test generation.
- Documentation generation.
- Safe command execution.
- Progress logging.
- Long-running task support.
- Sequential multi-model support.
- Skills and tool-based architecture.
- Optional web search.
- MCP-style extensibility.

## 8.2 Out of Scope for Initial Version

The initial version shall not focus on:

- Full graphical user interface.
- Cloud LLM dependency.
- Mobile application support.
- Enterprise collaboration features.
- Real-time multi-user editing.
- Training a custom LLM from scratch.
- Running multiple heavy LLMs simultaneously.
- Full IDE plugin integration.

---

# 9. Core Concept

SHAMSU is based on one important design principle:

> **Do not use the LLM as a brute-force scanner. Use tools to find the right context, then use the LLM to reason and generate.**

A small local model cannot efficiently understand a million-line codebase by reading everything. Therefore, SHAMSU separates the work into two layers.

## 9.1 Tool Layer

The tool layer handles deterministic and lightweight tasks:

- File discovery
- Code indexing
- Keyword search
- Symbol search
- AST parsing
- Document parsing
- Dependency scanning
- Test command execution
- Patch application
- Logging

## 9.2 LLM Layer

The LLM layer handles reasoning-heavy tasks:

- Explaining code
- Generating code
- Summarizing selected context
- Planning tasks
- Reviewing code
- Producing final user-facing answers

This approach allows SHAMSU to work on large projects while keeping memory usage low.

---

# 10. Functional Requirements

## FR-1: CLI Interface

The system shall provide a command-line interface where the user can type natural-language prompts.

Example:

```bash
shamsu
> Load project ./inventory-api
> Add JWT authentication
> Fix the failing login test
> Generate README
```

## FR-2: Project Loading

The system shall allow the user to select a local project folder as the active workspace.

The system shall treat this folder as the project boundary.

## FR-3: Workspace Scope Enforcement

The system shall operate only inside the selected project folder.

It shall not read, write, delete, or execute files outside the active workspace unless the user explicitly changes the workspace.

## FR-4: Project Indexing

The system shall index the active project using non-LLM tools.

The index shall include:

- File paths
- File extensions
- File sizes
- Programming languages
- Functions
- Classes
- Imports
- Exports
- Comments
- Dependencies
- Searchable snippets

## FR-5: Ignore Unnecessary Folders

The system shall ignore unnecessary or heavy folders such as:

```text
.git/
node_modules/
venv/
.venv/
dist/
build/
__pycache__/
.cache/
.idea/
.vscode/
target/
coverage/
```

## FR-6: Non-LLM Code Search

The system shall search code without using the LLM.

Search methods may include:

- Keyword search
- Regex search
- Symbol search
- BM25 ranking
- TF-IDF ranking
- AST-based search
- File path search
- Dependency-based search

## FR-7: Codebase Question Answering

The system shall answer questions about the loaded project.

Example prompts:

```text
How does authentication work?
Where is the database connection created?
Explain the payment flow.
Which file handles user registration?
```

The system shall retrieve relevant files and snippets before asking the LLM to answer.

## FR-8: Code Explanation

The system shall explain:

- Entire project structure
- Specific files
- Specific functions
- Specific classes
- API routes
- Database models
- Error messages
- Stack traces

## FR-9: Code Editing

The system shall support editing existing files.

Supported edit tasks include:

- Add feature
- Modify function
- Fix bug
- Refactor code
- Update imports
- Update configuration
- Add validation
- Add error handling

## FR-10: Patch-Based Modification

The system shall apply changes using patch-based edits whenever possible.

Before applying a patch, the system shall show:

- Files to be changed
- Summary of changes
- Risk level
- Diff preview or patch summary

## FR-11: File Creation

The system shall create new files when required.

Examples:

- Source files
- Test files
- Config files
- Documentation files
- Environment template files
- Project setup files

## FR-12: File Deletion Protection

The system shall not delete files without explicit user approval.

Deletion shall be considered a high-risk action.

## FR-13: Bug Fixing

The system shall support bug-fixing workflows.

For a bug report, stack trace, or failing test, the system shall:

1. Analyze the user input.
2. Search relevant files.
3. Retrieve context.
4. Identify likely cause.
5. Propose a fix.
6. Ask for approval.
7. Apply changes.
8. Run tests if approved.
9. Report the result.

## FR-14: Code Audit

The system shall audit a project for:

- Bugs
- Security risks
- Bad architecture
- Dead code
- Duplicate code
- Missing validation
- Missing error handling
- Missing tests
- Dependency issues
- Performance problems
- Code smells

## FR-15: Test Generation

The system shall generate tests for selected files, functions, APIs, or modules.

Supported test types:

- Unit tests
- Integration tests
- API tests
- Basic regression tests

## FR-16: Test Execution

The system shall run test commands after user approval.

Examples:

```bash
pytest
npm test
go test ./...
cargo test
python manage.py test
```

The system shall analyze test output and decide whether to continue fixing or report failure.

## FR-17: Documentation Generation

The system shall generate:

- README files
- API documentation
- Setup instructions
- Usage guides
- Inline comments
- Architecture notes
- Changelogs
- Project summaries

## FR-18: PRD Input

The system shall accept PRD or requirement documents from:

- Markdown files
- Text files
- PDF files
- DOCX files, if supported

## FR-19: Non-LLM PRD Parsing

The system shall parse requirement documents using non-LLM document-processing tools first.

The system shall extract structured information such as:

- Project title
- Problem statement
- Objectives
- User roles
- Functional requirements
- Non-functional requirements
- Constraints
- Features
- Pages or screens
- API requirements
- Database requirements
- Security requirements

## FR-20: PRD-to-Project Planning

The system shall convert extracted PRD information into a project plan.

The plan shall include:

- Project architecture
- Folder structure
- Modules
- Data models
- API endpoints
- UI pages, if applicable
- Task breakdown
- Testing plan
- Execution order

## FR-21: Autonomous Project Generation

The system shall generate a complete small project from a PRD or natural-language requirement.

The system shall:

- Create the folder structure.
- Generate source code.
- Generate configuration files.
- Generate documentation.
- Generate tests.
- Track unfinished tasks.
- Run validation commands if approved.
- Continue until the planned scope is completed or blocked.

## FR-22: Task Decomposition

The system shall divide large requests into smaller subtasks.

Example:

```text
User: Build a library management system.

SHAMSU task breakdown:
1. Parse requirements
2. Decide architecture
3. Create folder structure
4. Generate database models
5. Generate API routes
6. Generate validation logic
7. Generate tests
8. Generate README
9. Run tests
10. Final summary
```

## FR-23: Progress Tracking

The system shall track task progress during long operations.

The progress state shall include:

- Current task
- Completed tasks
- Pending tasks
- Blocked tasks
- Files changed
- Commands executed
- Errors found
- Next action

## FR-24: Resume Support

The system shall be able to resume long-running tasks from saved progress logs.

If execution stops, the user should be able to continue later without losing task state.

## FR-25: Natural Prompt Understanding

The user shall not need to type internal commands.

The user can give normal instructions like:

```text
Make this project production-ready.
Fix the signup bug.
Create the backend from this PRD.
Audit the code and improve it.
```

The system shall decide which internal tools and agents are needed.

## FR-26: Tool Calling

The system shall call internal tools for specific actions, including:

- File search
- File read
- File write
- Patch apply
- Index update
- Test run
- Git diff
- Document parse
- Web search
- MCP tool call

## FR-27: Git Awareness

The system should be able to use Git information when available.

Supported Git tasks may include:

- Show changed files
- Summarize diff
- Generate commit message
- Warn before editing uncommitted files
- Track generated changes

## FR-28: Error Handling

The system shall handle errors gracefully.

If an action fails, the system shall:

- Save the error in logs.
- Explain what failed.
- Suggest a next step.
- Allow retry, skip, or abort.

---

# 11. Safety and Security Requirements

## SR-1: Workspace Sandbox

The system shall restrict file operations to the active workspace.

It shall block access to paths outside the selected project folder.

## SR-2: Sensitive Path Blocking

The system shall block access to sensitive paths such as:

### Linux/macOS

```text
/
~/
~/.ssh/
~/.gnupg/
~/.aws/
~/.config/
~/.local/share/
~/Library/
/etc/
/var/
/usr/
/bin/
/sbin/
```

### Windows

```text
C:\Windows\
C:\Program Files\
C:\Program Files (x86)\
C:\Users\<user>\AppData\
C:\Users\<user>\.ssh\
Registry paths
System32
```

## SR-3: Dangerous Command Blocking

The system shall block dangerous commands by default.

### Linux/macOS examples

```bash
sudo
su
rm -rf /
chmod -R 777 /
chown -R
mkfs
dd
shutdown
reboot
kill -9 -1
:(){ :|:& };:
```

### Windows examples

```powershell
runas
format
diskpart
bcdedit
reg delete
sc delete
Remove-Item -Recurse C:\
Stop-Computer
Restart-Computer
```

## SR-4: Command Allowlist and Denylist

The system shall maintain:

- A command allowlist for safe project-level operations.
- A command denylist for dangerous system-level operations.

Safe examples may include:

```bash
pytest
npm test
npm run build
python app.py
git diff
git status
```

## SR-5: User Confirmation for Risky Actions

The system shall ask for approval before:

- Writing files
- Editing files
- Deleting files
- Moving files
- Running commands
- Installing dependencies
- Accessing the internet
- Calling external MCP tools

## SR-6: Patch Preview

Before editing code, the system shall show a patch preview or summary.

The user must approve before changes are applied.

## SR-7: Command Preview

Before executing a command, the system shall show:

- Command
- Working directory
- Reason
- Expected result
- Risk level

## SR-8: Secret Detection

The system shall detect and mask possible secrets such as:

- API keys
- Access tokens
- Passwords
- Private keys
- `.env` values
- Database URLs
- Cloud credentials

## SR-9: No Source Code Leakage

The system shall not send private project source code to the internet.

Web search queries must not contain private code unless the user explicitly allows it.

## SR-10: MCP Safety

MCP tools shall be permission-controlled.

Each MCP tool shall have:

- Tool name
- Description
- Permission category
- Workspace boundary
- Risk level
- Allowed operations

Unknown MCP tools shall be blocked by default.

## SR-11: Audit Trail

All file modifications and command executions shall be logged.

The user should be able to review what the system did.

---

# 12. Non-Functional Requirements

## NFR-1: Low Memory Usage

The system shall be optimized for devices with less than 8 GB RAM.

It shall avoid memory-heavy operations and large models.

## NFR-2: Offline-First Operation

The core system shall work offline after setup.

Internet access shall only be optional.

## NFR-3: Local Privacy

Project code, indexes, logs, documents, and generated outputs shall remain on the local device.

## NFR-4: Reasonable Performance

The system should remain responsive during indexing, planning, editing, and testing.

Long tasks shall show progress updates.

## NFR-5: Modularity

The system shall be modular so that models, tools, agents, parsers, and skills can be replaced or extended.

## NFR-6: Maintainability

The system shall have clean separation between:

- CLI
- Agent controller
- Planner
- Indexer
- Retriever
- Context builder
- LLM interface
- Safety manager
- Tool executor
- Logger
- Storage layer

## NFR-7: Reliability

The system shall recover from errors without corrupting user projects.

## NFR-8: Usability

The system shall be easy to use through normal prompts.

The user should not need to understand internal commands.

## NFR-9: Transparency

The system shall explain what it is doing during long tasks.

Example:

```text
[1/7] Indexing project...
[2/7] Reading PRD...
[3/7] Creating task plan...
[4/7] Generating backend files...
[5/7] Creating tests...
[6/7] Running validation...
[7/7] Finalizing report...
```

## NFR-10: Extensibility

The system shall support future integration with:

- More local models
- Additional programming languages
- More document formats
- MCP tools
- Web search providers
- IDE plugins
- GUI frontends

---

# 13. System Architecture

## 13.1 High-Level Architecture

```text
User
 │
 ▼
CLI Interface
 │
 ▼
Coordinator Agent
 │
 ├── Planner Agent
 ├── Safety Manager
 ├── Project Indexer
 ├── Retrieval Engine
 ├── Context Builder
 ├── LLM Manager
 ├── Tool Executor
 ├── Logger
 └── Storage Layer
```

## 13.2 Data Flow

```text
User Prompt
   ↓
Intent Detection
   ↓
Task Planning
   ↓
Codebase / Document Indexing
   ↓
Relevant Context Retrieval
   ↓
Context Compression
   ↓
LLM Reasoning or Code Generation
   ↓
Patch / File / Command Proposal
   ↓
User Approval
   ↓
Execution
   ↓
Validation
   ↓
Progress Log + Final Report
```

---

# 14. Agent Architecture

SHAMSU shall use a coordinator-based architecture.

## 14.1 Coordinator Agent

Responsible for:

- Understanding the user request.
- Selecting the required workflow.
- Assigning tasks to internal agents.
- Managing task state.
- Producing the final response.

## 14.2 Planner Agent

Responsible for:

- Breaking large requests into subtasks.
- Creating execution order.
- Estimating required files/tools.
- Tracking incomplete work.

## 14.3 Search Agent

Responsible for:

- Searching project files.
- Finding symbols.
- Finding references.
- Retrieving relevant snippets.

This agent shall not rely on the LLM for raw searching.

## 14.4 Context Builder Agent

Responsible for:

- Selecting relevant context.
- Removing irrelevant text.
- Chunking large files.
- Summarizing previous progress.
- Creating compact prompts for the LLM.

## 14.5 Code Writer Agent

Responsible for:

- Generating new code.
- Modifying existing code.
- Creating patches.
- Preserving project style.

## 14.6 Review Agent

Responsible for:

- Reviewing generated code.
- Checking risks.
- Finding likely bugs.
- Suggesting improvements.

## 14.7 Test Agent

Responsible for:

- Detecting test commands.
- Running approved commands.
- Reading test output.
- Reporting failures.

## 14.8 Documentation Agent

Responsible for:

- Creating README files.
- Writing setup instructions.
- Explaining architecture.
- Generating API docs.

## 14.9 Safety Agent

Responsible for:

- Checking workspace boundaries.
- Blocking dangerous commands.
- Checking file operation risk.
- Masking secrets.
- Requiring approval for risky actions.

---

# 15. Context Engineering Strategy

SHAMSU shall use context engineering to reduce pressure on the local LLM.

## 15.1 Context Sources

The system may use:

- User prompt
- Retrieved code snippets
- Symbol definitions
- File summaries
- PRD extracts
- Previous progress logs
- Test output
- Error messages
- Git diff

## 15.2 Context Reduction Techniques

The system shall reduce context size using:

- Relevance ranking
- Chunking
- File-level filtering
- Function-level extraction
- Symbol-based retrieval
- Snippet deduplication
- Progress summarization
- Dependency-aware retrieval

## 15.3 Context Budgeting

The system shall maintain a context budget.

Example:

```text
User request: 10%
Relevant code snippets: 50%
PRD/task requirements: 20%
Test output/errors: 10%
System instructions: 10%
```

## 15.4 No Full-Codebase Prompting

The system shall never send the entire codebase into the LLM prompt.

---

# 16. PRD and Document Processing

## 16.1 Supported Formats

The system shall support:

- `.md`
- `.txt`
- `.pdf`

Optional future support:

- `.docx`
- `.html`
- `.csv`

## 16.2 Parsing Strategy

Document parsing shall be performed using non-LLM tools first.

Examples:

- Markdown parser
- PDF text extractor
- Heading parser
- Keyword extractor
- Rule-based requirement detector

## 16.3 Extracted Data

The system shall extract:

- Title
- Overview
- Goals
- Features
- Actors
- User stories
- Functional requirements
- Non-functional requirements
- Constraints
- Acceptance criteria
- Technology preferences

## 16.4 LLM Usage

The LLM shall be used after structured extraction to:

- Resolve ambiguity.
- Create implementation plan.
- Generate code.
- Explain decisions.

---

# 17. Codebase Indexing and Retrieval

## 17.1 Index Storage

The system shall store indexes locally.

Possible storage:

- SQLite database
- JSON files
- Lightweight inverted index

## 17.2 Indexed Information

The index should contain:

- File path
- Language
- Symbols
- Imports
- Function names
- Class names
- Snippets
- Last modified time
- Hash for change detection

## 17.3 Incremental Indexing

The system should update only changed files when possible.

## 17.4 Retrieval Ranking

The system should rank files and snippets using:

- Keyword match
- Symbol match
- File path match
- Import relationship
- Recent edits
- Error stack trace references

---

# 18. Project Generation Workflow

When asked to generate a project from a PRD, SHAMSU shall follow this workflow:

```text
1. Read PRD
2. Parse requirements
3. Extract structured requirements
4. Create project plan
5. Create folder structure
6. Generate core files
7. Generate configuration files
8. Generate tests
9. Generate documentation
10. Ask approval before writing
11. Apply files
12. Run validation commands if approved
13. Fix simple errors if possible
14. Final summary
```

The system shall continue until the project is completed or blocked by missing information, failed dependencies, or user denial.

---

# 19. Code Editing Workflow

For editing an existing project, SHAMSU shall follow this workflow:

```text
1. Understand user request
2. Search relevant files
3. Retrieve relevant snippets
4. Build compact context
5. Plan required changes
6. Generate patch
7. Review patch
8. Ask user approval
9. Apply patch
10. Re-index changed files
11. Run tests if approved
12. Report final result
```

---

# 20. Long-Running Task Support

SHAMSU shall support tasks that run for long periods.

## 20.1 Progress State

The system shall store:

- Task ID
- User request
- Current phase
- Completed steps
- Pending steps
- Blocked steps
- Files created
- Files edited
- Commands executed
- Test results
- Errors
- Next action

## 20.2 Resume Behavior

If interrupted, the system should resume from the last saved checkpoint.

## 20.3 Progress Output

The CLI should show progress continuously.

Example:

```text
[SHAMSU] Planning project...
[SHAMSU] Creating backend structure...
[SHAMSU] Writing API routes...
[SHAMSU] Generating tests...
[SHAMSU] Waiting for approval to run tests...
```

---

# 21. Multi-Model Strategy

SHAMSU shall support multiple local models, but only one model shall be active at a time to reduce memory usage.

## 21.1 Model Roles

Possible model roles:

- Planner model
- Code generation model
- Summarization model
- Review model
- Documentation model

**`[UPDATED]` — as implemented (`shamsu/runtime/models.py`).** The role list
collapsed into a **two-anchor** contract to minimize model swapping: every
"thinking" role (router, qa, planner, classifier, review, docs, summarizer,
chat) shares one anchor, and every "coding" role (coder, frontend, backend,
tests, bugfix) shares the other. Three hardware tiers provide the anchors:

**Superseded: one model now serves every role.** The role split below applies only
when `SHAMSU_MULTI_MODEL_MODE=1` restores the two-anchor layout. By default a tier
resolves every role to its single model, and roles that must not pay for
chain-of-thought are handled per call by `role_should_think()`.

| Tier | Model (all roles) | Coding anchor (multi-model mode only) |
|---|---|---|
| `light` — 8 GB, CPU-only | `qwen2.5:3b-instruct` | `qwen2.5-coder:3b-instruct` |
| `default` — 8 GB cookbook | `qwen3:8b` | `qwen2.5-coder:7b-instruct` |
| `heavy` — 16 GB+ | `mistral-nemo:12b` | `qwen2.5-coder:14b` |

Notes:

- `deepseek-r1:7b` and `gemma3:4b` are **non-anchor** allowed models — recognized
  for existing installs, never auto-pulled.
- `SHAMSU_MODEL=<name>` pins any local model for every role.
- Each `ModelSpec` carries capability flags (`supports_native_tools`,
  `is_reasoning`). SHAMSU does not assume native tool-calling: models without it
  get a prompt-level tool protocol, with `llm/output.py::parse_model_turn` as the
  primary parser.
- Active tier is process-global, persisted at `.shamsu/model_tier.json`, and
  overridable with `SHAMSU_MODEL_TIER`. `SHAMSU_SINGLE_MODEL_MODE=1` routes every
  role to the thinking anchor for zero-swap measurement.
- Models are pulled lazily on first use, not eagerly at install.

## 21.2 Sequential Execution

Models shall run sequentially.

Example:

```text
Planner Model creates task plan
        ↓
Code Model generates implementation
        ↓
Review Model checks output
        ↓
Summary Model writes final report
```

## 21.3 Knowledge Sharing

Models shall share knowledge through local artifacts:

- Task plans
- Context packs
- File summaries
- Generated patches
- Test results
- Logs
- Review notes

This avoids keeping multiple models loaded at the same time.

---

# 22. MCP and Skills Support

## 22.1 MCP Support

SHAMSU shall support MCP-style external tools in a controlled manner.

Supported tool categories may include:

- File tools
- Git tools
- Search tools
- Documentation tools
- Database tools
- Testing tools
- Browser or web-search tools

## 22.2 MCP Safety

Each MCP tool shall have:

- Tool name
- Description
- Allowed operations
- Required permissions
- Workspace restrictions
- Risk level

## 22.3 Skills System

SHAMSU shall support reusable skills.

Example skills:

- `generate-rest-api`
- `create-readme`
- `write-unit-tests`
- `fix-import-errors`
- `audit-security`
- `explain-architecture`
- `generate-database-schema`
- `prd-to-project-plan`
- `refactor-module`
- `summarize-codebase`

A skill shall define:

- Purpose
- Required inputs
- Tools used
- Prompt template, if needed
- Safety level
- Output format

---

# 23. Web Search and External Knowledge

## 23.1 Optional Web Search

The system shall support optional web search for:

- Documentation
- Wiki pages
- Framework references
- API usage
- Error explanations
- Package information

## 23.2 User Permission

The system shall ask permission before accessing the internet.

## 23.3 Privacy Rules

The system shall not send private source code to search engines.

Search queries shall be limited to generic technical information unless the user explicitly allows otherwise.

---

# 24. Command Execution Policy

## 24.1 Command Categories

Commands shall be categorized as:

- Safe
- Medium-risk
- High-risk
- Blocked

## 24.2 Safe Commands

Safe commands may include:

```bash
git status
git diff
pytest
npm test
npm run build
python -m pytest
go test ./...
cargo test
```

## 24.3 Medium-Risk Commands

Medium-risk commands require explicit approval:

```bash
npm install
pip install
poetry add
cargo add
git checkout
```

## 24.4 Blocked Commands

Blocked commands include system-level destructive commands, privilege escalation, disk formatting, registry editing, and commands outside project scope.

---

# 25. Data Storage Requirements

The system shall store all data locally.

## 25.1 Local Storage Items

- Project index
- Task logs
- Progress state
- Model configuration
- Tool configuration
- Skill definitions
- File summaries
- Context packs
- Test results

## 25.2 Suggested Folder

```text
.shamsu/
  index.db
  tasks/
  logs/
  context/
  skills/
  config.json
```

---

# 26. Logging Requirements

## 26.1 Logs Must Include

- Timestamp
- User prompt
- Selected workflow
- Agent actions
- Files read
- Files edited
- Commands proposed
- Commands executed
- Test results
- Errors
- Final summary

## 26.2 Logs Must Avoid

- Raw secrets
- Full private keys
- Passwords
- Tokens
- Unnecessary private code dumps

---

# 27. MVP Scope

**`[UPDATED]`** Every MVP item below is now implemented. Checked boxes mean the
capability exists and is tested; the notes flag where it is not yet *reliable*.
MVP scope is met; MVP quality is not — see `CURRENT_STATE.md`.

The MVP version of SHAMSU shall include:

1. [x] CLI interface. — interactive REPL plus headless `shamsu run`
2. [x] Project folder loading.
3. [x] Workspace sandbox.
4. [x] Non-LLM project indexing. — incremental, SQLite + FTS5
5. [x] Search and retrieval. — FTS5 with additive re-ranking
6. [x] Smart context builder. — packing, truncation, token budgeting
7. [x] Local small LLM integration. — three tiers, lazy pull
8. [x] Markdown/TXT/PDF PRD parsing.
9. [x] Task planning from PRD.
10. [x] Code generation. — *quality bounded by the coder model*
11. [x] File creation. — *known bug: simple creation prompts can misroute*
12. [x] Patch-based code editing. — with full-rewrite fallback on malformed diffs
13. [x] Patch preview and approval.
14. [x] Dangerous command blocking.
15. [x] Test command execution with approval.
16. [x] Progress logging. — full per-run artifact bundle
17. [x] Basic documentation generation.
18. [x] Basic bug fixing.
19. [x] Basic code audit.

Beyond MVP and also built: sessions/resume, run inspection, mutation ledger with
rollback, permission memory, autonomy mode, plan mode, web + browser tools,
council mode, diagnostics/`doctor`, Django end-to-end generation, and an eval
harness.

The remaining gap to a usable release is **behavioral reliability**, not missing
MVP features.

---

# 28. Future Scope

Future versions may include:

- GUI application.
- IDE integration.
- Advanced MCP marketplace.
- Visual project dashboard.
- Screenshot understanding.
- More programming language parsers.
- Advanced dependency graph.
- Advanced vulnerability scanning.
- Local embeddings.
- Distributed local agents.
- Voice interface.
- Remote repository analysis.
- Automated pull request generation.

---

# 29. Success Criteria

The project shall be considered successful if SHAMSU can:

1. Run on a sub-8GB device.
2. Load and index a local project.
3. Answer codebase questions using retrieved context.
4. Parse a PRD from Markdown, TXT, or PDF.
5. Generate a small complete project from requirements.
6. Edit existing code safely.
7. Ask permission before risky actions.
8. Block dangerous commands.
9. Run approved tests.
10. Save progress logs.
11. Resume or report long-running task state.
12. Work without cloud LLM APIs.

---

# 30. Glossary

## LLM

Large Language Model. In SHAMSU, a small local quantized model is used for reasoning, explanation, and code generation.

## Context Engineering

The process of selecting, compressing, ranking, and organizing only the most relevant information before sending it to the LLM.

## PRD

Product Requirements Document. A document that describes the features, goals, requirements, and constraints of a software product.

## MCP

Model Context Protocol. A tool integration pattern that allows AI systems to connect with external tools in a structured way.

## Skill

A reusable module designed to perform a specific task, such as generating tests or auditing security.

## Workspace

The selected project folder where SHAMSU is allowed to operate.

## Patch

A structured code change that can be previewed before being applied.

## Retrieval

The process of finding relevant files, functions, classes, or snippets from the project index.

---

# Final One-Line Pitch

> **SHAMSU is a sub-8GB local autonomous coding agent that kills API bills by using smart indexing, context engineering, safe tool execution, and small local models to build, fix, audit, and ship software projects offline.**
