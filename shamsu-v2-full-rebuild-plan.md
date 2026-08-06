# SHAMSU v2.0.0 — Full Rebuild Plan

**Document status:** Proposed implementation plan  
**Target branch:** `shamsu-v2.0.0`  
**Architecture strategy:** Greenfield orchestration core with selective migration of tested legacy components  
**Primary model target:** Small local language models  
**Primary deployment target:** Local development workspaces and Docker-based projects  

---

# 1. Executive Decision

SHAMSU v2 should be rebuilt around a new typed runtime rather than by continuing to extend the current production loop.

The existing SHAMSU implementation should be preserved under:

```text
legacy-code/
```

The new production implementation should live under:

```text
src/shamsu/
```

The v2 runtime must not import the legacy agent loop, prompt builder, planner lifecycle, or memory orchestration.

Legacy code may be used only as:

- A reference
- A source of known failure cases
- A source of isolated, testable utilities
- A baseline for evaluations
- A donor for specific algorithms that are migrated behind clean v2 interfaces

---

# 2. Graphiti Decision

## 2.1 Graphiti is not required for v2

Graphiti should not be part of the initial SHAMSU v2 critical path.

Reasons:

- It consumes significant CPU and memory.
- It introduces another service that must remain healthy.
- It requires additional model calls for memory extraction.
- It complicates debugging.
- It is not required for current task state.
- It is not required for project state.
- It is not required for code retrieval.
- It is not required for checkpointing.
- It is not required for long-codebase context management.
- The existing integration is not trusted to be correctly wired into the live runtime.

SHAMSU v2 should work fully without Graphiti.

## 2.2 Lightweight memory replaces Graphiti

The initial memory design should use:

```text
SQLite
+ repository artifacts
+ deterministic code indexes
+ task checkpoints
+ optional semantic index
```

This is sufficient for:

- Remembering project facts
- Remembering architecture decisions
- Remembering completed tasks
- Remembering failures and lessons
- Tracking current plans and progress
- Retrieving relevant code
- Reconstructing compact model context
- Resuming interrupted tasks
- Working with large repositories

## 2.3 Graphiti becomes an optional future adapter

Graphiti may be reconsidered later only when:

- The core agent is already reliable.
- Memory benchmarks show the lightweight design is insufficient.
- Graphiti proves useful in controlled evaluations.
- Graphiti can fail without breaking the agent.
- Resource consumption is acceptable.
- Stale-memory behavior is well controlled.

Graphiti must never become authoritative for:

- Current task state
- Current plan
- Current step
- Completion evidence
- Approval state
- Current repository state
- Current code index
- Checkpoint recovery

---

# 3. Core Architecture Principle

> The model should receive a compact task packet, not the repository, not the chat history, and not the entire memory system.

SHAMSU v2 should store complete information outside the model.

The context compiler should select only the information needed for the next decision.

The runtime should hold complexity.

The small model should perform one narrow responsibility per call.

---

# 4. Main Goals

SHAMSU v2 should eventually support:

- Repository inspection
- Code search
- Symbol and reference lookup
- Planning
- Tool selection
- File modification
- Test execution
- Failure repair
- Git checkpoints
- Package installation
- Documentation-driven integration
- Database inspection and migration
- Docker environment creation
- Local deployment
- PRD-to-project workflows
- Long-running incremental project development
- Low-level projects such as a tiny operating system

These capabilities should be introduced gradually.

The initial v2 release should focus on reliable repository changes, verification, and recovery.

---

# 5. Non-Goals for the First Release

The first v2 release should not attempt to:

- Use Graphiti
- Deploy to production
- Access production databases
- Run unrestricted shell commands
- Use unrestricted network access
- Support many autonomous agents
- Modify multiple repositories simultaneously
- Build complete applications from PRDs
- Manage cloud infrastructure
- Build an operating system
- Run unlimited autonomous loops
- Self-modify its own production prompts automatically

---

# 6. Branch and Repository Migration

## 6.1 Create the new branch

Create:

```text
shamsu-v2.0.0
```

Before creating the branch:

1. Confirm the current branch.
2. Confirm the expected legacy commit.
3. Confirm working-tree status.
4. Record current dependency files.
5. Run the available legacy tests.
6. Run the available legacy evaluations.
7. Record known failures.
8. Create a final legacy baseline tag.

Suggested tag:

```text
shamsu-v1-legacy-baseline
```

## 6.2 Archive the existing implementation

Create:

```text
legacy-code/
```

Move the existing implementation into this folder, including:

- Existing SHAMSU package
- Existing tests
- Existing evaluations
- Existing scripts
- Existing documentation
- Existing configuration
- Existing Graphiti integration
- Existing codebase-memory integration
- Existing dependency files
- Existing CLI implementation
- Existing PRD pipelines
- Existing prompt definitions
- Existing repair loops
- Existing agent loops

Do not move:

- `.git/`
- Repository license
- Root contribution policy
- Root security policy
- Repository ownership metadata

## 6.3 Create a clean archival commit

The legacy move should be its own commit.

Suggested commit:

```text
chore: archive SHAMSU v1 under legacy-code
```

Do not include v2 implementation changes in this commit.

## 6.4 Add legacy documentation

Create:

```text
legacy-code/LEGACY_README.md
```

It should describe:

- The archived commit
- Why the code was archived
- Which loop was live
- Known orchestration problems
- Known memory concerns
- Known cancellation problems
- How to run legacy tests
- Which components may be migrated
- Which components must not be imported
- That legacy code is no longer the production implementation

---

# 7. Target Repository Structure

```text
SHAMSU/
├── legacy-code/
│   ├── shamsu/
│   ├── tests/
│   ├── evals/
│   ├── docs/
│   ├── scripts/
│   ├── pyproject.toml
│   └── LEGACY_README.md
│
├── src/
│   └── shamsu/
│       ├── runtime/
│       ├── state/
│       ├── agent/
│       ├── context/
│       ├── artifacts/
│       ├── tools/
│       ├── verification/
│       ├── memory/
│       ├── code_intelligence/
│       ├── models/
│       ├── security/
│       ├── interfaces/
│       └── telemetry/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── evals/
│   ├── fixtures/
│   └── adversarial/
│
├── docs/
│   ├── architecture/
│   ├── decisions/
│   ├── migration/
│   ├── development/
│   └── protocols/
│
├── examples/
├── scripts/
├── pyproject.toml
├── README.md
├── ARCHITECTURE.md
├── MIGRATION_STATUS.md
├── LEGACY_COMPONENTS.md
└── CHANGELOG.md
```

---

# 8. Strict Legacy Boundary

## 8.1 No runtime imports

Production v2 code must not import from:

```text
legacy-code/
```

The legacy folder must not be added to the production Python path.

## 8.2 Migration process for reusable components

A legacy component can enter v2 only after:

1. Identifying the exact source file and symbol
2. Reviewing its dependencies
3. Writing isolated tests
4. Defining a clean v2 interface
5. Copying or rewriting the logic
6. Removing old-loop dependencies
7. Documenting the decision
8. Passing v2 tests
9. Passing security checks
10. Passing evaluation tasks

## 8.3 Components that may be migrated

Possible migration candidates:

- Sandbox path validation
- Command risk classification
- Command timeout handling
- Model-output normalization
- Tool-call salvage
- Test-output digesting
- Error-signature generation
- Tool-result truncation
- Git utility functions
- Structural code-graph client
- Reliability metrics
- Secret-redaction utilities

## 8.4 Components that must not be migrated as architecture

Do not migrate:

- `AgentChatLoop`
- The old main loop structure
- The old task lifecycle
- The old prompt-conversation replay model
- The old planner orchestration
- The old completion logic
- The old memory orchestration
- The old two-registry tool architecture
- Large collections of inline recovery counters
- Long-running mode behavior
- Implicit success classification

---

# 9. SHAMSU v2 High-Level Architecture

```text
┌──────────────────────────────────────┐
│ User Interface / CLI / API           │
└─────────────────┬────────────────────┘
                  │
┌─────────────────▼────────────────────┐
│ Run Controller                       │
│                                      │
│ - Run registration                   │
│ - Cancellation                       │
│ - User feedback                      │
│ - Wall-clock limits                  │
│ - Status and events                  │
└─────────────────┬────────────────────┘
                  │
┌─────────────────▼────────────────────┐
│ Typed Agent Runtime                  │
│                                      │
│ - State transitions                  │
│ - Task classifier                    │
│ - Planner                            │
│ - Step executor                      │
│ - Repair controller                  │
│ - Approval controller                │
│ - Completion controller              │
└────────────┬──────────────┬──────────┘
             │              │
┌────────────▼───────┐  ┌───▼──────────────────┐
│ Context Compiler   │  │ Tool Gateway         │
│                    │  │                      │
│ - Artifacts        │  │ - Files             │
│ - Project memory   │  │ - Shell             │
│ - Code index       │  │ - Git               │
│ - Task state       │  │ - Tests             │
│ - Fresh results    │  │ - Docker            │
└────────────┬───────┘  │ - Databases         │
             │          │ - Documentation     │
┌────────────▼───────┐  └──────────┬───────────┘
│ SQLite State Store │             │
│ Artifact Store     │  ┌──────────▼───────────┐
│ Code Index         │  │ Isolated Workspace   │
└────────────────────┘  └──────────────────────┘
```

---

# 10. Agent State Machine

```text
RECEIVE_TASK
    ↓
LOAD_PROJECT_STATE
    ↓
INSPECT_PROJECT
    ↓
CLASSIFY_TASK
    ├── DIRECT
    └── PLANNED
           ↓
       CREATE_PLAN
           ↓
       VALIDATE_PLAN
           ↓
       APPROVAL_CHECK
           ↓
EXECUTE_CURRENT_STEP
    ↓
VERIFY_CURRENT_STEP
    ├── PASS → CREATE_CHECKPOINT
    ├── REPAIRABLE → REPAIR
    ├── PLAN_INVALID → REPLAN
    ├── APPROVAL_REQUIRED → WAIT
    ├── CANCELLED → STOP
    └── BLOCKED → REPORT
           ↓
CHECK_REMAINING_STEPS
    ├── MORE → EXECUTE_CURRENT_STEP
    └── NONE → FINAL_VERIFICATION
                         ↓
                    COMPLETION_GATE
                         ↓
                    FINAL_REPORT
```

The runtime controls transitions.

The model proposes decisions.

The model does not own the loop.

---

# 11. Bounded Inner Execution Loop

Each plan step uses a bounded ReAct-style loop:

```text
Compile step context
→ model selects one action
→ validate action
→ policy check
→ execute one tool
→ compress observation
→ register evidence
→ verify progress
→ continue, repair, replan, or stop
```

Initial limits:

| Limit | Value |
|---|---:|
| Actions per step | 4 |
| Repair attempts per step | 2 |
| Re-plans per task | 2 |
| Consecutive failed actions | 3 |
| Mutating tool calls per model decision | 1 |
| Model-requested logical actions per turn | 1 |
| Long-running mode | Disabled |
| Automatic production actions | Disabled |

---

# 12. Persistent State

Use SQLite for the initial version.

Required records:

```text
projects
runs
tasks
plans
plan_steps
tool_events
evidence
approvals
checkpoints
failures
architecture_decisions
project_facts
artifact_records
memory_records
```

## 12.1 Project state

Store:

- Project identifier
- Repository root
- Technology stack
- Active branch
- Service definitions
- Package managers
- Database types
- Environment requirements
- Architecture decisions
- Known problems
- Current index version
- Current artifact version

## 12.2 Task state

Store:

- Task ID
- User request
- Task status
- Current phase
- Current step
- Action count
- Repair count
- Re-plan count
- Changed files
- Required evidence
- Registered evidence
- Pending approvals
- Last checkpoint
- Final result

## 12.3 Checkpoints

Create checkpoints:

- After project inspection
- After plan creation
- After approval
- After every verified step
- After re-planning
- Before final completion
- On user interruption
- On cancellation
- Before higher-risk operations

Initial resume support only needs to resume at verified step boundaries.

---

# 13. Memory Without Graphiti

## 13.1 Memory layers

SHAMSU v2 should use four lightweight memory layers.

### Layer 1 — Authoritative runtime state

Stored in SQLite:

- Current task
- Current plan
- Current step
- Approvals
- Tool events
- Evidence
- Checkpoints

### Layer 2 — Project knowledge

Stored in SQLite and human-readable files:

- Architecture decisions
- Project conventions
- Technology stack
- Environment facts
- Known limitations
- Important dependencies
- User-approved constraints

### Layer 3 — Code artifacts

Generated from the repository:

- Repository map
- Module summaries
- Symbol cards
- Dependency maps
- Route maps
- Database schema summaries
- Test maps
- Change manifests

### Layer 4 — Optional semantic retrieval

A small local embedding index may be added later for:

- Documentation retrieval
- Old task-summary retrieval
- Natural-language code search fallback

Semantic search must not replace structural code retrieval.

---

# 14. Code Artifact System

The code artifact system is central to long-codebase support.

Artifacts should convert large, complex repository information into smaller, structured, model-readable units.

Artifacts are not model-generated chat summaries only.

They should be:

- Structured
- Versioned
- Traceable to source files
- Refreshable
- Invalidatable
- Queryable
- Small enough for prompt use
- Human-readable when practical

---

# 15. Required Artifact Types

## 15.1 Repository Manifest

File:

```text
.shamsu/artifacts/repository_manifest.json
```

Contains:

- Repository name
- Languages
- Frameworks
- Package managers
- Main entry points
- Major directories
- Service definitions
- Test frameworks
- Build commands
- Run commands
- Environment files
- Database configuration
- Docker configuration

## 15.2 Repository Map

File:

```text
.shamsu/artifacts/repository_map.md
```

Contains a compact directory map with descriptions.

Example:

```text
apps/api/
  FastAPI backend
  Entry: apps/api/main.py

apps/web/
  React frontend
  Entry: apps/web/src/main.tsx

database/migrations/
  PostgreSQL migrations

tests/integration/
  Cross-service tests
```

## 15.3 Module Cards

Directory:

```text
.shamsu/artifacts/modules/
```

One artifact per important module.

Example:

```text
modules/apps__api__auth.md
```

Contains:

- Purpose
- Public interfaces
- Main classes and functions
- Imports
- External dependencies
- Callers
- Callees
- Related tests
- Known risks
- Recently changed symbols

## 15.4 Symbol Cards

Directory:

```text
.shamsu/artifacts/symbols/
```

One artifact per important symbol.

Contains:

- Fully qualified symbol
- File path
- Line range
- Symbol type
- Signature
- Purpose
- Callers
- Callees
- Side effects
- Related tests
- Related configuration
- Last indexed source hash

## 15.5 Dependency Graph Summary

Files:

```text
.shamsu/artifacts/dependency_graph.json
.shamsu/artifacts/dependency_graph.md
```

Tracks:

- Module dependencies
- Import relationships
- Service relationships
- Package dependencies
- Database dependencies
- External API dependencies

## 15.6 Route and API Map

File:

```text
.shamsu/artifacts/api_map.json
```

Contains:

- Route
- HTTP method
- Handler symbol
- Request schema
- Response schema
- Authentication requirement
- Service dependencies
- Related tests

## 15.7 Database Artifact

Files:

```text
.shamsu/artifacts/database_schema.json
.shamsu/artifacts/database_schema.md
```

Contains:

- Tables
- Columns
- Constraints
- Relationships
- Indexes
- Migrations
- Models
- Accessing services
- Related tests

## 15.8 Test Map

File:

```text
.shamsu/artifacts/test_map.json
```

Contains:

- Test file
- Test names
- Covered symbols
- Covered routes
- Test command
- Required services
- Test category
- Last result

## 15.9 Configuration Map

File:

```text
.shamsu/artifacts/configuration_map.json
```

Contains:

- Environment variables
- Configuration files
- Defaults
- Consumers
- Secret classification
- Required services
- Development-only settings

## 15.10 Task Packet

Directory:

```text
.shamsu/artifacts/tasks/
```

One packet per active task.

Contains:

- User request
- Acceptance criteria
- Plan
- Current step
- Relevant files
- Relevant symbols
- Relevant architecture decisions
- Current evidence
- Open risks
- Latest failure packet

## 15.11 Change Manifest

File:

```text
.shamsu/artifacts/tasks/<task-id>/change_manifest.json
```

Contains:

- Changed files
- Changed symbols
- Added dependencies
- Database changes
- Configuration changes
- Tests added
- Tests run
- Verification results
- Commit hash

## 15.12 Failure Capsule

File:

```text
.shamsu/artifacts/tasks/<task-id>/failure_capsule.json
```

Contains:

- Expected result
- Actual result
- Error signature
- Relevant stack frames
- Relevant changed files
- Related symbols
- Previous repair attempts
- Suggested next probes

## 15.13 Architecture Decision Records

Directory:

```text
docs/decisions/
```

Each decision should record:

- Context
- Decision
- Alternatives
- Consequences
- Status
- Related files
- Related tasks
- Date
- Superseded decision, if any

---

# 16. Artifact Generation

Artifacts should be generated by deterministic tooling where possible.

Use:

- File scanners
- Tree-sitter
- Language-server data
- Import parsers
- Git
- Test discovery
- Package-manifest parsers
- OpenAPI parsers
- Database-schema inspection
- Docker Compose parsing

The model may add natural-language summaries, but structural facts must come from deterministic analysis.

Example:

```text
Parser determines:
- symbol name
- file
- signature
- imports
- references
- tests

Model optionally adds:
- compact purpose summary
- architectural role
- likely risk
```

Model-generated summaries must retain source references and artifact version.

---

# 17. Artifact Freshness and Invalidation

Every artifact must include:

- Source paths
- Source hashes
- Artifact version
- Generator version
- Creation timestamp
- Last refresh timestamp
- Confidence
- Status

Statuses:

```text
fresh
stale
invalidated
missing
generation_failed
```

## 17.1 Invalidation rules

When a file changes:

1. Mark its module card stale.
2. Mark contained symbol cards stale.
3. Mark dependent module summaries potentially stale.
4. Mark related test map entries stale.
5. Regenerate affected artifacts after the change is verified.
6. Do not send stale structural claims to the model without a warning.

## 17.2 Contradiction handling

If a fresh tool result contradicts an artifact:

```text
Fresh tool result wins.
Artifact is invalidated.
Contradiction is recorded.
Artifact is queued for regeneration.
```

---

# 18. Code Intelligence

The repository intelligence system should support:

- File lookup
- Exact search
- Symbol lookup
- Import lookup
- Reference lookup
- Caller lookup
- Callee lookup
- Route lookup
- Model-to-table lookup
- Related-test lookup
- Configuration-consumer lookup
- Impact analysis
- Git-history lookup
- Semantic search fallback

Recommended retrieval order:

```text
1. Exact file/path match
2. Exact text search
3. Symbol index
4. Reference graph
5. Call graph
6. Related tests
7. Dependency graph
8. Git history
9. Semantic search fallback
```

---

# 19. Context Compiler

The model should not receive the entire conversation.

Each call should receive a compiled frame.

```text
[PHASE]

[CURRENT TASK]

[CURRENT STEP]

[ACCEPTANCE CRITERIA]

[PROJECT FACTS]

[RELEVANT ARTIFACTS]

[RELEVANT SOURCE CODE]

[LATEST OBSERVATION]

[PREVIOUS STEP SUMMARY]

[ALLOWED TOOLS]

[OUTPUT CONTRACT]
```

## 19.1 Context tiers

### Hot context

Always considered:

- Current task
- Current step
- Acceptance criteria
- Latest result
- Relevant code
- Allowed tools

### Warm context

Included when useful:

- Plan summary
- Completed steps
- Architecture decisions
- Recently modified modules
- Known task failures

### Cold context

Retrieved only when necessary:

- Old task history
- Archived logs
- General documentation
- Old project decisions
- Historical failures

## 19.2 Suggested 8K token budget

| Section | Tokens |
|---|---:|
| System and phase rules | 500 |
| Task and acceptance criteria | 500 |
| Current step and plan summary | 500 |
| Project facts and artifacts | 900 |
| Relevant source code | 2,800 |
| Latest observations | 700 |
| Tool definitions | 400 |
| Output reserve | 1,700 |

---

# 20. Phase Contracts

## 20.1 Inspect phase

Allowed:

- Project inspection
- File reads
- Code search
- Symbol lookup
- Git status
- Dependency inspection
- Database schema inspection

Blocked:

- File writes
- Package installation
- Database mutation
- Docker mutation

## 20.2 Plan phase

Allowed:

- Read-only inspection
- Risk assessment
- Plan generation
- Acceptance-criteria generation
- Required-evidence definition

Blocked:

- Source changes
- Database writes
- Deployment operations

## 20.3 Author phase

Allowed:

- Read relevant files
- Patch files
- Apply formatting
- Inspect diff
- Run narrow code checks

Blocked by default:

- Production access
- Destructive shell commands
- Database migration
- Public deployment

## 20.4 Verify phase

Allowed:

- Tests
- Linting
- Type checking
- Builds
- Git diff inspection
- Health checks
- Smoke tests

Blocked:

- Source changes
- Dependency installation
- Database mutation

## 20.5 Repair phase

Allowed:

- Read failure capsule
- Read affected files
- Modify failure-related files
- Run targeted verification

Blocked:

- Unrelated architecture changes
- Broad repository rewrites
- New feature work

## 20.6 Deploy phase

Allowed:

- Docker Compose validation
- Image builds
- Local service startup
- Logs
- Health checks
- Smoke tests

## 20.7 Complete phase

Allowed:

- Generate final report from registered evidence

The model cannot set completion directly.

---

# 21. Planning Contract

Each plan step should contain:

```json
{
  "id": "STEP-03",
  "title": "Add login endpoint",
  "inputs": [
    "user model",
    "authentication service"
  ],
  "outputs": [
    "POST /auth/login"
  ],
  "constraints": [
    "Do not store plaintext passwords"
  ],
  "allowed_tools": [
    "code.search",
    "file.read",
    "file.patch",
    "test.run"
  ],
  "acceptance_criteria": [
    "Valid credentials succeed",
    "Invalid credentials return 401"
  ],
  "required_evidence": [
    "targeted authentication tests pass",
    "Git diff reviewed"
  ],
  "risk": "medium",
  "approval_required": false
}
```

Plans are stored externally.

Only the current step and a compact plan summary enter the prompt.

---

# 22. Model-Facing Tool Surface

Initial tools:

```text
project.inspect
code.search
file.read
file.patch
test.run
git.inspect
git.checkpoint
```

Later additions:

```text
docs.retrieve
package.manage
database.inspect
database.migrate
docker.manage
http.check
process.logs
```

Internally, logical tools may call several deterministic operations.

For example:

```text
git.inspect
```

may collect:

- Branch
- Status
- Changed files
- Relevant diff
- Untracked files
- Recent commits

The model should not choose among many low-level Git commands for ordinary tasks.

---

# 23. Tool Contracts

Every tool should define:

- Name
- Purpose
- Typed input
- Typed output
- Timeout
- Maximum output
- Risk level
- Required phase
- Reversibility
- Approval requirement
- Evidence produced
- Artifact invalidation behavior

Example:

```json
{
  "name": "file.patch",
  "allowed_phases": ["author", "repair"],
  "risk": "medium",
  "requires_approval": false,
  "reversible": true,
  "produces_evidence": ["file_changed"],
  "invalidates": [
    "module_card",
    "symbol_card",
    "test_map"
  ]
}
```

---

# 24. Safety

## 24.1 Workspace isolation

Requirements:

- One isolated workspace per project
- CPU limits
- Memory limits
- Disk limits
- Process limits
- Command timeouts
- Restricted filesystem
- Restricted network
- No host root access
- No direct host Docker socket
- No host credential exposure

## 24.2 Command policy

Block or require approval for:

```text
rm -rf
sudo
mount
umount
chmod outside workspace
chown outside workspace
curl | sh
wget | sh
privileged containers
host network mode
host PID mode
Docker socket mounts
destructive SQL
credential printing
fork bombs
resource exhaustion
```

## 24.3 Structured commands

Prefer:

```json
{
  "program": "pytest",
  "args": ["tests/auth", "-q"],
  "cwd": "/workspace/project",
  "timeout_seconds": 120
}
```

Avoid relying on unvalidated shell strings.

---

# 25. Evidence and Completion

The model may propose that a step is complete.

The runtime accepts completion only when required evidence exists.

| Claim | Required evidence |
|---|---|
| File modified | Successful patch and Git diff |
| Tests pass | Required test command succeeded |
| Build succeeds | Build command succeeded |
| App runs | Health checks and smoke tests passed |
| Migration succeeds | Migration and schema verification passed |
| Task complete | All acceptance criteria have verified evidence |

Completion rule:

```text
required_evidence ⊆ verified_evidence
```

No evidence means no completion.

---

# 26. Verification Pipeline

Typical verification order:

```text
Formatting
→ Linting
→ Type checking
→ Unit tests
→ Integration tests
→ Build
→ Docker startup
→ Health checks
→ Smoke tests
→ Git diff review
→ Acceptance-criteria review
```

Not every task requires every check.

Required checks are defined in the plan before execution.

---

# 27. Failure Handling

Failure types:

- Syntax error
- Type error
- Test failure
- Dependency conflict
- Build failure
- Runtime failure
- Tool failure
- Permission failure
- Missing context
- Plan invalidation
- Resource limit
- Network failure
- Database failure
- Service health failure

Recovery flow:

```text
Classify failure
→ generate failure capsule
→ identify affected step
→ select repair or rollback
→ limit repair attempts
→ verify again
→ stop when progress is not improving
```

Same-error detection should stop repeated repairs.

---

# 28. Run Control

The production run controller must support:

- Run registration
- Run status
- Cancellation
- Feedback injection
- Current model-call cancellation
- Current tool-call cancellation when supported
- Wall-clock limits
- Event logging
- Pause
- Resume
- Approval waiting

Every live run must be observable and cancellable.

---

# 29. Development Milestones

## Milestone 1 — Repository reset

Deliverables:

- Branch `shamsu-v2.0.0`
- Legacy baseline tag
- Existing code under `legacy-code/`
- New root README
- New architecture document
- CI separation

Exit condition:

- V2 tests run without importing legacy agent code

## Milestone 2 — Runtime foundation

Deliverables:

- Typed state
- SQLite persistence
- State transitions
- Cancellation
- Checkpoints
- Event ledger
- Execution limits

Exit condition:

- Simulated runs can pause, resume, cancel, and reject invalid transitions

## Milestone 3 — Artifact foundation

Deliverables:

- Repository manifest
- Repository map
- Module cards
- Symbol cards
- Artifact registry
- Source hashes
- Staleness and invalidation

Exit condition:

- Artifacts regenerate correctly after source changes

## Milestone 4 — Read-only agent

Deliverables:

- Project inspection
- Code search
- File reading
- Context compiler
- Structured model decisions
- Tool policy

Exit condition:

- Agent produces grounded implementation plans without modifying files

## Milestone 5 — Controlled editing

Deliverables:

- File patching
- Git inspection
- Targeted tests
- Evidence records
- Rollback
- Checkpoint commits

Exit condition:

- Agent completes simple changes with verified evidence

## Milestone 6 — Structured planning

Deliverables:

- Plan contracts
- Step persistence
- Acceptance criteria
- Evidence requirements
- Re-planning
- Step completion gates

Exit condition:

- Agent completes bounded multi-file tasks step-by-step

## Milestone 7 — Repair

Deliverables:

- Failure capsules
- Error signatures
- Bounded repair
- Same-failure stop
- Related-file restrictions

Exit condition:

- Agent fixes simple failures without uncontrolled edits

## Milestone 8 — Code intelligence

Deliverables:

- Tree-sitter indexing
- Symbol lookup
- Reference graph
- Call graph
- Related tests
- Impact analysis
- Semantic fallback

Exit condition:

- Retrieval evaluations show useful code is selected accurately

## Milestone 9 — Project memory

Deliverables:

- Project facts
- Architecture decisions
- Failure lessons
- Confidence
- Staleness
- Context invalidation

Exit condition:

- Memory improves task success without increasing stale-context errors

## Milestone 10 — Packages and documentation

Deliverables:

- Official documentation retrieval
- Version-aware integration
- Controlled package installation
- Lockfile verification
- Contract tests

## Milestone 11 — Docker

Deliverables:

- Compose validation
- Image build
- Service startup
- Logs
- Health checks
- Local smoke tests

## Milestone 12 — Databases

Deliverables:

- Schema inspection
- Read-only queries
- Migration generation
- Disposable database tests
- Approval-controlled migration
- Rollback

## Milestone 13 — PRD workflows

Deliverables:

- PRD parsing
- Requirements extraction
- Architecture proposal
- Backlog
- Vertical slices
- Acceptance testing

## Milestone 14 — Advanced projects

Deliverables:

- Multi-service systems
- Background workers
- Queues
- Caches
- Infrastructure-as-code
- Performance profiling
- Security testing

## Milestone 15 — Tiny operating system support

Deliverables:

- Cross-compilation
- Emulator control
- Serial-log artifacts
- Boot tests
- Kernel build workflow
- Filesystem images

---

# 30. Initial Pull Request Sequence

## PR 1 — Archive legacy code

- Create branch
- Create baseline tag
- Move old implementation
- Add legacy README
- Add migration status file

## PR 2 — V2 package skeleton

- Add package layout
- Add interfaces
- Add initial CI
- Add import-boundary checks

## PR 3 — State and persistence

- Add typed records
- Add SQLite store
- Add state migrations
- Add transition validation

## PR 4 — Run control

- Add registration
- Add cancellation
- Add events
- Add pause/resume
- Add wall-clock limits

## PR 5 — Artifact registry

- Add artifact metadata
- Add source hashing
- Add freshness status
- Add invalidation rules

## PR 6 — Repository artifacts

- Generate repository manifest
- Generate repository map
- Generate module cards
- Generate symbol cards

## PR 7 — Tool contracts and policy

- Add typed tools
- Add phase allowlists
- Add risk classification
- Add approval handling

## PR 8 — Read-only agent

- Add context compiler
- Add project inspection
- Add code search
- Add file reads
- Add structured model output

## PR 9 — Controlled authoring

- Add file patching
- Add Git diff
- Add targeted tests
- Add evidence records
- Add rollback

## PR 10 — Planning contracts

- Add plans
- Add plan steps
- Add acceptance criteria
- Add required evidence
- Add re-planning

## PR 11 — Completion gate

- Add claim validation
- Add step completion gate
- Add final completion gate
- Add evidence reports

## PR 12 — Repair

- Migrate error digest
- Add failure capsules
- Add repair limits
- Add same-error stopping

## PR 13 — Structural code intelligence

- Add Tree-sitter
- Add references
- Add callers/callees
- Add related tests
- Add impact analysis

## PR 14 — Lightweight project memory

- Add project facts
- Add architecture decisions
- Add failure lessons
- Add confidence and staleness

## PR 15 — Legacy utility migration

- Migrate selected parser
- Migrate sandbox utility
- Migrate command safety
- Migrate reliability metrics

---

# 31. Evaluation Strategy

Track:

```text
verified_task_success_rate
false_success_rate
success_without_verification_rate
first_pass_verified_rate
repair_success_rate
repeated_action_rate
wrong_tool_rate
rollback_rate
context_retrieval_precision
artifact_freshness_error_rate
stale_context_usage_rate
tokens_per_verified_task
```

## 31.1 Initial task suite

- Documentation edit
- Single-file bug fix
- Add one unit test
- Fix one failing test
- Small multi-file feature
- Refactor one function
- Add one API validation rule

## 31.2 Adversarial suite

- Malicious instruction inside repository documentation
- Destructive shell request
- Contradictory architecture decision
- Stale artifact
- Huge terminal output
- Repeated failing repair
- Missing environment variable
- Unrelated pre-existing test failure
- Tool schema mistake
- Attempted path escape

---

# 32. Definition of the First Usable Release

The first usable v2 release should reliably perform:

```text
Open repository
→ inspect task
→ generate or refresh artifacts
→ retrieve relevant code
→ create a short plan
→ patch files
→ run targeted tests
→ inspect Git diff
→ record evidence
→ checkpoint
→ report verified result
```

Required properties:

- Runs are cancellable
- Runs are resumable
- State is persistent
- Tool access is phase-restricted
- Completion requires evidence
- Repair is bounded
- Context uses fresh artifacts
- Stale artifacts are detected
- No production imports come from legacy code
- Graphiti is not required
- The agent can complete the initial evaluation suite consistently

---

# 33. Future Graphiti Reconsideration Gate

Graphiti should be reconsidered only if all are true:

1. The v2 runtime is already reliable.
2. The SQLite and artifact memory system is stable.
3. Benchmarks identify a real long-term memory limitation.
4. Graphiti improves that benchmark.
5. Resource use remains acceptable.
6. Graphiti can run asynchronously.
7. Graphiti can fail without blocking tasks.
8. Graphiti records include project isolation.
9. Graphiti records include confidence and freshness.
10. Stale Graphiti results can be invalidated.

Even then, Graphiti remains optional.

---

# 34. Final Architectural Rules

1. The runtime controls the loop.
2. The model performs one narrow decision at a time.
3. SQLite is authoritative.
4. Code artifacts are the primary long-codebase compression mechanism.
5. Structural code retrieval comes before semantic retrieval.
6. Graphiti is optional and deferred.
7. The complete repository never enters the model context.
8. Every artifact is versioned and traceable.
9. Fresh tool results override stale artifacts.
10. Completion requires verified evidence.
11. Legacy code is a donor, not a dependency.
12. New development occurs only under `src/shamsu/`.
13. Long-running autonomy remains disabled until evaluations justify it.
14. Safety, cancellation, checkpointing, and recovery are runtime features.
15. Reliability is more important than autonomy.

---

# 35. Final Recommendation

Proceed with:

```text
New branch: shamsu-v2.0.0

Archive:
legacy-code/

New production implementation:
src/shamsu/

Authoritative memory:
SQLite

Long-codebase representation:
Versioned code artifacts
+ structural code index
+ compact task packets

Optional future memory:
Graphiti adapter, disabled by default
```

This creates a lower-resource, easier-to-debug, small-model-friendly foundation while preserving the useful parts of the previous SHAMSU implementation for selective migration.
