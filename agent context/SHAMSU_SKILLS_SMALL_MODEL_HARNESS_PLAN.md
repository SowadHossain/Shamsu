# SHAMSU Skills and Small-Model Harness Plan

**Status:** In progress; Phases 0-6 are implemented as the reliability, skills, PRD-artifact, and durable milestone-execution foundation on `phase0-telemetry-slice`. Phase 6 includes per-milestone verifier checkpoints, opt-in schema-validated model preflight, bounded repair transitions, and rollback-policy enforcement. Phase 7 has a provenance bridge. Deeper skill hooks and A/B rollout remain pending.
**Date:** 2026-07-23
**Primary target:** Reliable local operation with 7B/8B planner and coding models

## 1. Goal

Add a Claude/Codex-style skill system and strengthen the execution harness so
SHAMSU can complete larger coding tasks and realistic PRDs more reliably than
the current version, without regressing ordinary questions, edits, bug fixes,
web search, MCP use, permissions, logging, or rollback behavior.

The central design decision is this:

> The model should make one small, grounded decision at a time. The harness
> should own task state, requirements, dependencies, permissions, context
> selection, verification, recovery, and the definition of success.

A 7B/8B model should not be asked to remember an entire PRD, architecture,
file graph, progress history, tool policy, and build failure while also writing
correct code. SHAMSU should compile those concerns into small artifacts and
give the model only the slice required for the current step.

### 1.1 Ordering decision

Per-step reliability comes before new skill architecture. If a build requires
ten independent single-attempt steps, a 60% step success rate produces only
`0.60^10`, or about 0.6% one-pass completion. An 85% step success rate produces
about 20%. The assumptions are simplified because real failures are correlated
and SHAMSU supports retries, rollback, and resume, but the priority is still
valid: improve the reliability of each verified transaction first.

The implementation order is therefore:

1. Measure the current execution path using existing run artifacts.
2. Make the existing tool-result, diagnostic, verification, and outcome
   mechanisms consistent across every mutation route.
3. Re-measure and fix the worst observed per-step failure.
4. Introduce skill discovery and selection in shadow mode.
5. Add PRD requirement and milestone structure after the base execution loop is
   trustworthy.

Skills, requirement ledgers, and checkpoints still matter. They reduce
wrong-target work, cross-step drift, repeated work, and missing requirements.
Bundled skill assets and deterministic checks can also improve per-step success.
They are sequenced later because they should build on measured execution truth,
not because they are merely cosmetic.

## 2. Current Baseline We Must Preserve

SHAMSU already has important reliability infrastructure:

- A deterministic and model-assisted routing layer.
- A structured task handoff in `shamsu/agents/task_harness.py`.
- Separate planner and coder role contracts.
- Context budgeting and compaction.
- Workspace retrieval and Codebase-Memory MCP integration.
- Real external MCP support with stdio, SSE, streamable HTTP, OAuth, and
  permission handling.
- Approval-backed file and command tools.
- Transactional mutations, backups, patches, and undo support.
- Verification and bounded repair loops.
- A deterministic `DiagnosticDigest` that selects root diagnostics and compact
  source windows instead of feeding full build logs to the repair model.
- A strict one-root-error repair loop with rollback, progress comparison,
  repeated-action blocking, and verifier-owned final messages.
- Per-tool-result token limiting before results enter model chat history.
- Resumable PRD generation state.
- A canonical action ledger containing prompts, decisions, model calls,
  contexts, commands, tools, mutations, and final output.
- A deterministic test suite, a live-model eval harness, and a headless runner.

These should be extended, not replaced.

## 3. Current Limitations

### 3.1 The task harness is a handoff, not a durable executor

The current `TaskPlan` is useful but shallow. It contains steps, tools, target
files, and generic verification text, then renders them into a prompt. It does
not own a dependency graph, requirement coverage, per-step context, checkpoints,
attempt budgets, or acceptance evidence.

### 3.2 Complex generation is too concentrated

The freeform generator asks for a project-wide file plan, then generates files
one by one while repeating the broad contract and full file plan. This can work
for small projects, but it puts too much architectural consistency pressure on
the model.

### 3.3 Framework knowledge is mixed into the generator

The React/Vite hardening logic currently lives in the generic freeform
generator. It improves the result, but it is difficult to extend cleanly to
other stacks and makes it hard to distinguish model work from deterministic
scaffolding. That behavior belongs in a visible, versioned stack skill.

### 3.4 Long PRDs are reduced, but not executed as a traceable graph

`PRDContract` is a good source of truth, but generation still needs stable
requirement IDs, milestone ownership, dependency tracking, and acceptance
evidence. Without those, a build can pass while major PRD requirements remain
unimplemented.

### 3.5 Strong reliability mechanisms are not universal or measured

SHAMSU already has deterministic verification, compact diagnostic packets,
one-root-error repair, rollback, and tool-result token caps. The gap is
coverage and measurement:

- Not every mutation or generation route emits the same verification evidence.
- Repair-attempt outcomes are not aggregated into a product-level success rate.
- Tool-result truncation does not consistently record original tokens, returned
  tokens, the truncation decision, and a full-output artifact reference.
- A successful command is not sufficient evidence unless it is the required
  verifier or acceptance check for the current task.
- There is no single report for patch application, first-pass verification,
  repair success, false success, and context/tool-output pressure.

A small model performs better when these existing mechanisms are applied after
every narrow transaction rather than only at the end of a broad build.

### 3.6 The live eval suite is not yet a product-building benchmark

Component tests and short prompt evals protect many behaviors, but a skills and
milestone system needs repeated end-to-end PRD builds scored for requirement
coverage, runtime behavior, safety, and truthful logs.

## 4. Target Architecture

```text
User prompt
    |
    v
Intent + safety + requested outcome
    |
    v
Skill catalog metadata -> skill selection -> conflict validation
    |
    v
Task contract
    |
    +--> Simple request -> existing focused workflow
    |
    +--> Complex request / PRD
             |
             v
        PRD compiler
             |
             +--> requirement ledger
             +--> architecture decisions
             +--> milestone graph
             +--> acceptance matrix
             |
             v
        Milestone executor
             |
             +--> load only relevant skills/context
             +--> preflight plan
             +--> inspect/read
             +--> mutate transactionally
             +--> verify narrow checks
             +--> repair one failure at a time
             +--> checkpoint evidence/state
             |
             v
        Full build + acceptance + PRD conformance
             |
             v
        Truthful final report and complete run ledger
```

## 5. Skill System Design

### 5.1 Discovery locations

Use three layers, with higher layers overriding skills with the same name:

1. Bundled: `shamsu/skills/bundled/<skill-name>/`
2. User: `~/.shamsu/skills/<skill-name>/`
3. Workspace: `<workspace>/.shamsu/skills/<skill-name>/`

Workspace skills must not be allowed to silently elevate permissions or bypass
the sandbox. They provide instructions and resources; SHAMSU remains the
authority for tools, approvals, paths, and commands.

### 5.2 Skill package format

Each skill should use progressive disclosure:

```text
react-vite/
  SKILL.md                  required instructions
  skill.json                optional machine-readable policy and hooks
  references/               loaded only when relevant
  assets/                   templates and boilerplate copied as output inputs
  scripts/                  optional approval-backed utilities
  checks/                   deterministic verification definitions
```

`SKILL.md` frontmatter should initially contain only:

```yaml
---
name: react-vite
description: Build and repair React and Vite applications. Use for React,
  TypeScript, Vite, frontend dashboard, SPA, and component tasks.
---
```

Optional `skill.json` should contain machine policy that does not belong in
model context:

- Skill version and compatibility range.
- Stack and task tags.
- Priority and conflicts.
- Allowed tool names, as a restriction only.
- Required MCP capability names.
- Resource and template paths.
- Deterministic preflight, postprocess, and verification hook IDs.
- Context and action budgets.

External skills must not load arbitrary Python entry points in the first
version. Scripts may run only through the existing command permission system.
Bundled deterministic hooks should be registered internal code, selected by a
known hook ID.

### 5.3 Initial bundled skills

1. `developer`

   - Always active for coding tasks.
   - Defines inspect, plan, change, verify, report behavior.
   - Requires reading relevant files before edits.
   - Requires evidence before claiming completion.
2. `prd-planner`

   - Compiles a PRD into requirement and milestone artifacts.
   - Tracks source references, assumptions, and unresolved decisions.
   - Never writes application code.
3. `react-vite`

   - Owns the React/Vite scaffold, package constraints, file conventions,
     TypeScript checks, build commands, and repair hints.
   - Receives the deterministic React hardener currently embedded in the
     freeform generator.
4. `ui-designer`

   - Converts journeys and screens into a compact UI specification.
   - Defines hierarchy, responsive states, accessibility, empty/loading/error
     states, interaction coverage, and screenshot checks.
   - Does not replace the framework skill.
5. `sqlite-persistence`

   - Owns schema, migrations, foreign keys, seed behavior, persistence tests,
     and backup/error rules.
6. `django`

   - Wraps the existing dependable Django generation pipeline instead of
     duplicating it.
7. `testing`

   - Selects test layers and turns acceptance criteria into executable checks.
8. `web-research`

   - Provides source selection and citation rules while reusing the current
     permission-gated web tools.
9. `mcp-client`

   - Guides capability discovery and selection among configured MCP tools.
   - Does not handle transport or OAuth; the existing MCP manager keeps that
     responsibility.

### 5.4 Skill selection

Skill selection should be hybrid and bounded:

1. Deterministic rules produce a candidate set from the route, PRD contract,
   repository files, requested stack, and available MCP capabilities.
2. A small schema-constrained model call may rank ambiguous candidates.
3. A validator applies defaults, dependencies, conflicts, and a maximum active
   skill count.
4. Full skill instructions are loaded only after selection.

For a typical React PRD, the active set should be:

```text
developer + prd-planner + react-vite + ui-designer + testing
```

Only the skills relevant to the current milestone should be placed in that
model call. The database milestone may swap `ui-designer` for
`sqlite-persistence`.

### 5.5 Skill context budget

For an 8K context model, target this input allocation:

| Context component          | Target tokens |
| -------------------------- | ------------: |
| System and tool contract   |     900-1,200 |
| Current task and milestone |       500-800 |
| Active skill instructions  |     800-1,400 |
| Relevant PRD requirements  |     500-1,000 |
| Code/file/error context    |   2,000-3,000 |
| Previous-step evidence     |       300-600 |
| Output reserve             |   1,500-2,000 |

This is a budget, not a quota. The context manager should remove low-value
snippets before compressing the task contract or current error.

## 6. PRD Compiler for Long Documents

The raw PRD should be read and normalized once, then compiled into durable,
small artifacts. Later model calls should receive only referenced slices.

### 6.1 Required artifacts

Store these under the generation task directory:

- `prd-contract.json`: normalized entities, stack, roles, journeys, and rules.
- `requirements.jsonl`: one stable requirement per line.
- `architecture.json`: selected architecture and explicit assumptions.
- `milestones.json`: dependency graph and execution status.
- `acceptance-matrix.json`: requirement to check and evidence mapping.
- `decisions.jsonl`: unresolved and resolved product decisions.
- `progress.json`: current milestone, attempts, blockers, and checkpoints.

### 6.2 Stable requirement records

Each requirement needs:

- Stable ID such as `AUTH-003` or `INCIDENT-012`.
- Short normalized statement.
- Source section/page/line reference.
- Priority and scope.
- Owning milestone.
- Implementing files.
- Verification method.
- Status: pending, implemented, verified, blocked, or excluded.
- Evidence references.

The compiler must reject or warn on model-inferred requirements that have no
source reference. Inferences can be retained only as labeled assumptions.

### 6.3 Ambiguity gate

Ask the user only when an ambiguity changes architecture, data ownership,
security, destructive behavior, or a public product decision. Ordinary coding
details remain SHAMSU's responsibility.

## 7. Milestone Planning and Execution

### 7.1 Milestone schema

Each milestone should include:

- ID, title, goal, and requirement IDs.
- Dependencies and preconditions.
- Active skills.
- Expected files and interfaces.
- Maximum files changed in one execution step.
- Deterministic verification commands and checks.
- Acceptance conditions.
- Attempt budget and rollback policy.
- Status and evidence references.

### 7.2 Recommended build order

For a full-stack application:

1. Repository and toolchain foundation.
2. Shared types and domain contracts.
3. Persistence schema and seed path.
4. Authentication and authorization.
5. One vertical product slice at a time.
6. Navigation and cross-feature integration.
7. Empty, loading, error, and responsive UI states.
8. Unit and integration tests.
9. Browser-level acceptance checks.
10. Full PRD conformance audit.

This produces working checkpoints early. It is safer than generating every
model, every screen, and every test as independent files before integration.

### 7.3 Step size for 7B/8B models

Default limits:

- One goal per model call.
- One to three closely related files per mutation step.
- At most six visible tools in a tool-calling turn.
- At most six tool rounds before replanning.
- One primary diagnostic per repair attempt.
- Two repair attempts per step, then replan or block honestly.
- Full build at milestone boundaries; syntax/type/import checks after each step.

These values should be configurable and measured, not permanently hard-coded.

### 7.4 Preflight call

Before mutation, use a small structured call that returns:

- The requirement IDs being handled.
- Files that must be inspected.
- Files expected to change.
- Interfaces that must remain compatible.
- The narrow verification command/check.
- Any blocker requiring user input.

The harness validates this plan against the workspace and skill policy before
giving it to the executor.

### 7.5 Execution call

The executor receives the approved preflight artifact, exact relevant code,
the current requirements, and a small tool set. It does not receive the full
conversation, full PRD, all skills, or unrelated past errors.

### 7.6 Checkpoint and resume

After each verified milestone, persist:

- Files and patches.
- Commands and results.
- Requirements completed.
- Public interfaces introduced.
- Remaining known failures.
- Active architecture decisions.
- A compact handoff for the next milestone.

Resume must use this state rather than asking the model to reconstruct progress
from chat history.

## 8. Generation Strategy

### 8.1 Deterministic foundation, model-owned product logic

Use assets and deterministic hooks for fragile, repetitive foundation work:

- Package manifests with valid dependency sets.
- Framework configuration.
- TypeScript/Python compiler settings.
- Standard entry points.
- Test runner configuration.
- Migration and seed command wiring.

Use the model for product-specific behavior:

- Domain rules.
- Feature workflows.
- Page composition.
- Validation behavior.
- Integration code.
- Tests derived from acceptance criteria.

Every deterministic output must be visible in the skill and recorded in logs.
Do not hide substantial product behavior inside a generic postprocessor.

### 8.2 Interface-first generation

For each milestone:

1. Define shared types, schemas, routes, or function contracts.
2. Verify those contracts parse/typecheck.
3. Generate implementations against them.
4. Generate tests against the same requirement IDs.
5. Run the narrow verification gate.

This lowers cross-file drift because the model is not inventing both sides of
an interface independently in distant calls.

### 8.3 Existing project edits

Prefer patches for existing files. Permit whole-file generation only for new,
small files or when the current rewrite fallback proves it is safer. Continue
to require transactional writes and retain undo compatibility.

### 8.4 Freeform generator migration

Do not rewrite it in one change.

1. Keep the current freeform path as `legacy` behavior.
2. Extract React/Vite hardening into the bundled `react-vite` skill while
   preserving byte-equivalent output under tests.
3. Introduce the milestone executor behind a feature flag.
4. Run legacy and new planning in shadow mode without applying the new plan.
5. Compare plans and logs in eval runs.
6. Make the new path default only after the quality gates pass.
7. Retain a temporary rollback flag for one release cycle.

## 9. Universal Verification and Repair Harness

This work extends existing code; it does not create a second verifier or repair
system. `verify.gate`, `DiagnosticDigest`, `RepairLoop`, transaction rollback,
and action-ledger evidence remain canonical. The first objective is to route
every relevant execution path through them and expose comparable metrics.

### 9.1 Verification ladders

Run the cheapest useful check first:

| Level | When                     | Examples                                       |
| ----- | ------------------------ | ---------------------------------------------- |
| V0    | After generated output   | JSON/schema/path/content validation            |
| V1    | After each mutation step | parse, lint target, import, typecheck target   |
| V2    | After a vertical slice   | focused unit/integration tests                 |
| V3    | At milestone boundary    | package build or project test command          |
| V4    | At UI/product boundary   | browser flow, screenshot, accessibility checks |
| V5    | At project completion    | PRD acceptance and traceability audit          |

The harness, not the model, decides whether each level passed.

### 9.2 Diagnostic digest

The existing digest and strict repair prompt already provide the core behavior.
Standardize their use so every repair prompt includes only:

- Failing command/check.
- Exit code and one primary error.
- Small relevant traceback/compiler span.
- Files implicated by the error.
- Requirement and milestone IDs when available.
- Last patch summary.

Command logs are already artifact-backed. Extend the same behavior to large
non-command and MCP tool results: retain the full redacted result by reference,
while giving the model only the compact summary. Never copy large logs into the
model context.

### 9.3 Repair state machine

```text
verify fails
  -> classify failure
  -> detect environment/dependency/code/test/requirement mismatch
  -> select relevant skill and files
  -> one narrow repair
  -> rerun the same check
  -> pass: checkpoint
  -> same failure twice: replan
  -> budget exhausted: mark blocked/failed with evidence
```

Never weaken or delete a required test merely to make the build green. Any
acceptance change must be an explicit, logged decision.

## 10. Tool and MCP Harness Improvements

### 10.1 Smaller tool surfaces

Expose only tools relevant to the current step. A UI implementation step may
need file read/search/write, command, and browser tools. It does not need every
Git, Django, web, and MCP tool at once.

### 10.2 Tool preconditions

Encode deterministic rules such as:

- Read an existing file before modifying it.
- Search before claiming a symbol or file is absent.
- Validate paths before mutation.
- Run mutations through transaction-backed tools.
- Use a configured MCP tool only when its capability matches the task.
- Never substitute an MCP mutation with a local tool when the user explicitly
  required that MCP.

### 10.3 Tool result normalization

SHAMSU already caps individual model-facing tool results. Complete that design
by returning a small structured envelope to the model:

- `ok`, `category`, `summary`, `changed_paths`, `evidence_ref`, `next_hint`.
- Store full stdout/stderr and large MCP content as artifacts.
- Include stable error categories rather than asking the model to infer them
  from thousands of log characters.
- Record `original_tokens`, `returned_tokens`, `truncated`, and `artifact_path`
  for every result so context pressure can be measured instead of guessed.

### 10.4 Permissions remain centralized

Skills can narrow tools but cannot grant access. The existing permission,
read-only, scoped-write, dry-run, sandbox, and approval systems remain the only
authorization layer.

## 11. Context Harness for Small Models

### 11.1 Task-local context assembly

Build every model context from named blocks:

1. Role contract.
2. Current milestone and success condition.
3. Active skill instructions.
4. Current requirement records.
5. Relevant code and interfaces.
6. Current diagnostic, if any.
7. Previous verified handoff.
8. Allowed tools and output schema.

Record why each context item was included or omitted.

### 11.2 No whole-history dependence

Conversation history is useful for user intent, but project execution state must
come from artifacts. Summaries should contain decisions and verified facts, not
unverified model claims.

### 11.3 Model call profiles

Create explicit profiles instead of using one general setting:

| Profile      | Temperature | Output style          | Primary model need         |
| ------------ | ----------: | --------------------- | -------------------------- |
| route/select |         0.0 | tiny JSON             | fast instruction following |
| extract/plan |     0.0-0.1 | bounded JSON          | structure and coverage     |
| implement    |         0.1 | code/tool calls       | coding strength            |
| repair       |         0.0 | patch/tool calls      | exact diagnostics          |
| summarize    |         0.0 | short structured text | faithful compression       |

Benchmark whether the thinking 7B or coding 7B is better for each profile.
Do not assume a reasoning model is automatically a better router or planner.

### 11.4 Output validation

Every structured model output must pass schema and semantic validation. Repair
minor JSON formatting mechanically. Reject plans with unknown files,
requirements, skills, commands, or circular dependencies and ask for a narrow
retry.

## 12. Logging and Explainability

Keep the existing action ledger as canonical. Add these records:

- Attempted and cleanly applied patch/mutation counts.
- First-pass verifier result and required verifier identity.
- Repair success by attempt number, including unchanged/worse/rolled-back.
- Tool-result original tokens, returned tokens, truncation, and artifact path.
- Final-claim versus required-evidence comparison for false-success scoring.
- Skill catalog version and discovered sources.
- Candidate, selected, rejected, and conflicting skills.
- Why each skill was selected, as a concise decision summary.
- Skill resources, templates, scripts, and hooks used.
- Task contract and milestone graph versions.
- Requirement IDs supplied to every model call.
- Context inclusion and omission reasons.
- Preflight plan and validation result.
- Milestone state transitions and checkpoints.
- Verification level, command/check, result, and evidence.
- Repair classification, attempt, and outcome.
- Requirement coverage and final acceptance matrix.

For model reasoning, record a safe structured decision summary:

```json
{
  "goal": "Implement incident status filtering",
  "evidence": ["REQ-INC-014", "src/data.ts exports Incident"],
  "decision": "Add a typed filter in the incident list state",
  "next_action": "Patch IncidentList.tsx and run its focused test",
  "uncertainty": []
}
```

Do not depend on or promise raw hidden chain-of-thought. It is unnecessary for
debugging and can be noisy or misleading. The useful log is the prompt,
selected context, decisions, tool activity, mutations, verification evidence,
and final output.

Continue redacting secrets in prompts, MCP headers, command output, environment
values, and artifacts.

## 13. Compatibility and Rollout Strategy

### 13.1 Feature flags

Introduce independently switchable flags:

- `SHAMSU_SKILLS_ENABLED`
- `SHAMSU_SKILLS_SHADOW_MODE`
- `SHAMSU_MILESTONE_EXECUTOR`
- `SHAMSU_LEGACY_FREEFORM`

Exact names can change during implementation, but the capabilities must be
independently controllable for testing and rollback.

### 13.2 Compatibility rules

- Existing prompts must keep their current route unless the new behavior is
  explicitly enabled.
- Existing CLI commands and public Python interfaces remain valid.
- Existing generation-state files are versioned and migrated non-destructively.
- Existing action-ledger readers tolerate new record fields.
- Existing permissions and approval defaults remain unchanged.
- Existing Django generation stays on its proven pipeline at first.
- Existing MCP configuration remains Claude-compatible.
- Skills never auto-install dependencies or authorize MCPs without the normal
  command and approval path.

### 13.3 Shadow mode

In shadow mode SHAMSU should:

1. Run the current behavior normally.
2. Discover/select skills and create the proposed task/milestone plan.
3. Log the proposed artifacts.
4. Never apply shadow mutations or run shadow commands.
5. Compare route, file expectations, requirement coverage, and verification
   plans with the actual run.

This finds integration mistakes before user-visible behavior changes.

## 14. Implementation Phases

Current implementation snapshot, 2026-07-23:

- Phase 0 telemetry report command and reproducible aggregate report are in
  place.
- Phase 1 verifier-owned outcomes, tool-result telemetry, repair attempt
  ledger events, and cross-route verifier evidence are in place.
- Phase 2 failure taxonomy and medium/long PRD benchmark fixtures are in place.
  The remaining Phase 2 gate is to run the stochastic benchmark scenarios at
  the required sample count and freeze the observed thresholds.
- Phase 3 skill package discovery is in place for bundled, user, and workspace
  skills with precedence, validation, safe metadata rejection, and `/skills`
  inspection commands.
- Phase 4 deterministic skill selection is wired into the task handoff and the
  action ledger. `SHAMSU_SKILLS=off|shadow|on` controls whether selection is
  disabled, logged-only, or injected into coding prompts.
- Phase 5 now emits the PRD execution artifact bundle:
  `prd-requirements.json`, `requirements.jsonl`, `architecture.json`,
  `milestones.json`, `acceptance-matrix.json`, `decisions.jsonl`, and
  `progress.json`.
- Phase 6 has a guarded durable-state slice: `SHAMSU_MILESTONE_EXECUTOR=1`
  lets the generic PRD build path fall back to compiled requirement-ledger
  milestones, persist `.shamsu/prd-executions/<contract-hash>/`, write
  per-milestone preflight files, checkpoint milestone outcomes, and resume by
  skipping already checkpointed milestones. Schema-validated model preflight
  bounded verifier-driven repair transitions, and rollback policy enforcement
  are in place.
- Phase 7 has a first bridge: bundled `react-vite`, `ui-designer`, `testing`,
  `sqlite-persistence`, and `mcp-tools` skills exist, and the current
  deterministic React/Vite foundation logs `react-vite` skill provenance.
  The hardener has not yet been fully extracted out of the generic freeform
  generator.
- Phase 8 and Phase 9 are not complete yet. They should be treated
  as the next implementation waves, not as finished capability.

### Phase 0 - Rapid execution telemetry

This phase should take days, not weeks. First derive what is already available
from action-ledger and session artifacts; add counters only where the existing
records cannot answer the question.

Define one execution step as a **verified transaction**: one bounded goal, its
inspections, one mutation transaction, and the required narrow verifier. A
model call or generated file alone is not a step.

Deliverables:

- Record exact model names/tags, temperatures, context limits, environment, and
  feature flags.
- Measure patch/mutation attempts versus clean applications.
- Measure first-pass verification success.
- Measure repair success on attempt one, attempt two, or never.
- Measure false success against required verifier and acceptance evidence.
- Measure original and returned tool-result tokens and truncation rate.
- Measure plan validity: referenced files exist or are explicitly new, commands
  are allowed, and the proposed verifier is available.
- Run the existing live evals plus two representative builds and the current
  AtlasOps build. Preserve all raw artifacts.

Exit gate:

- The six primary rates above can be reproduced from a run set.
- The largest measured failure class is identified without guessing.
- No skill or milestone architecture is required to obtain this first report.

### Phase 1 - Universalize existing execution reliability

Deliverables:

- Route every mutation path through transaction evidence and a required
  verifier when one exists.
- Ensure successful unrelated commands cannot satisfy a task's verifier.
- Use the existing `DiagnosticDigest` and strict one-root-error repair path for
  all supported build/test failures.
- Add the structured tool-result envelope and artifact references for large
  command, file, browser, web, and MCP results.
- Put repair attempts and outcomes in the canonical action ledger.
- Apply V0-V3 verification consistently: output validation, narrow parse/type
  checks, focused tests, and milestone/project build checks.

Exit gate:

- Every mutating route ends as verified, explicitly unverified, failed,
  blocked, denied, cancelled, or timed out from harness evidence.
- No route can become success only because the model claimed completion.
- Repeated unchanged repair actions stop or replan within the configured
  budget.
- Existing safety, permission, rollback, MCP, and deterministic tests pass.

### Phase 2 - Re-measure and repair the worst bottleneck

Deliverables:

- Repeat Phase 0 with identical models, settings, prompts, and sample counts.
- Classify remaining failures as routing, planning, context, tool call, patch
  application, verification, repair, requirement coverage, or environment.
- Improve the single worst class before introducing new architecture.
- Add medium and long PRD fixtures with machine-checkable acceptance criteria.
- Run each stochastic scenario at least three times.

Decision gate:

- If first-pass verified-transaction success remains below the frozen target,
  continue improving that execution path.
- If bounded repair does not materially improve eventual step success, improve
  repair proposals/context before adding more orchestration.
- Proceed to skills when execution truth is reliable enough that missing domain
  knowledge and cross-step structure are the dominant remaining failures.

### Phase 3 - Skill package loader only

Deliverables:

- New skill types, discovery, precedence, validation, and catalog.
- Bundled `developer` skill with no runtime injection yet.
- User and workspace skill discovery.
- Skill inspection CLI commands.
- Ledger records for discovery and validation.

Exit gate:

- Feature disabled is behaviorally identical to the measured Phase 2 version.
- Invalid or malicious skill metadata fails safely.
- Workspace overrides do not bypass permissions.

### Phase 4 - Skill selection in shadow mode

Deliverables:

- Deterministic candidate selection.
- Optional bounded model ranking.
- Dependency/conflict validator.
- Context budget accounting for selected skill instructions.
- Shadow-mode logs on existing prompts.

Exit gate:

- `developer` is selected for coding tasks.
- Stack skills match repository/PRD evidence.
- Irrelevant skills do not enter context.
- Ordinary Q&A, file reads, web, and MCP prompts do not regress.

### Phase 5 - PRD compiler and requirement ledger

Deliverables:

- Stable requirement IDs and source references.
- Architecture, assumption, milestone, and acceptance artifacts.
- Semantic validators for ownership, dependency, and source evidence.
- Long-PRD chunk extraction using section-aware retrieval.

Exit gate:

- Every acceptance criterion maps to a milestone and verification method.
- Unsupported or ambiguous requirements are visible, not silently dropped.
- Re-running compilation is stable unless the PRD changes.

### Phase 6 - Durable milestone executor

Deliverables:

- Versioned task/milestone state machine.
- Preflight, execution, verification, repair, checkpoint, and resume states.
- Per-step tool and context budgets.
- Transaction boundary and rollback per milestone.
- Integration with the existing Taskmaster/MilestoneTask code rather than a
  second unrelated task system.

Implementation status:

- Implemented behind `SHAMSU_MILESTONE_EXECUTOR=1`:
  `.shamsu/prd-executions/<contract-hash>/state.json`,
  `preflight/<milestone>.json`, `checkpoints.jsonl`, `blockers.jsonl`,
  `verification.jsonl`, resume from the first incomplete compiled milestone,
  reuse of the existing `MilestoneTask` store, and harness-owned milestone
  verifier results that checkpoint as `verified`, `implemented`
  when honestly unverifiable, or `failed`.
- Implemented behind `SHAMSU_PRD_MODEL_PREFLIGHT=1`: schema-validated model
  preflight calls that can narrow focus within compiled allowlists, record
  accepted/rejected decisions in `preflight-decisions.jsonl`, and always fall
  back to the deterministic preflight when validation fails.
- Implemented in the guarded milestone executor: failed verifier results can
  enter bounded repair attempts, each attempt is durable in `repairs.jsonl`,
  milestone state moves through `repairing`, and the final checkpoint is owned
  by the post-repair verifier verdict.
- Implemented in the guarded milestone executor: failed milestone transactions
  are grouped from the milestone boundary, restored through the existing patch
  rollback engine when the rollback policy requires it, recorded in
  `rollbacks.jsonl`, and excluded from preserved milestone `changed_files`
  after successful rollback.

Exit gate:

- A stopped build resumes from the first unverified milestone.
- Completed milestones are not regenerated unnecessarily.
- Failure and blocked states cannot become success through final-response text.
- Eventual completion and cost are measured separately from one-pass success.

### Phase 7 - Move React/Vite behavior into skills

Deliverables:

- `react-vite`, `ui-designer`, and `testing` bundled skills.
- Current deterministic React/Vite foundation moved out of the generic
  freeform generator.
- Visible asset/template provenance in logs.
- Requirement-aware tests and V4-V5 UI/PRD acceptance checks.
- Vertical-slice generation for a medium PRD.

Exit gate:

- Existing React/Vite build tests remain green.
- Generated dependency manifests contain only valid packages.
- Model-generated files and deterministic skill-generated files are clearly
  distinguished in the ledger.
- Every mutated milestone has verification and requirement evidence.

### Phase 8 - Persistence, UI, browser, and MCP skill depth

Deliverables:

- `sqlite-persistence` skill with real SQLite integration and migrations.
- UI acceptance checks across desktop and mobile.
- MCP capability-aware selection using the existing MCP manager.
- Skill-specific acceptance checks and failure guidance.

Exit gate:

- Seeded data survives process restart where the PRD requires persistence.
- Login and key journeys are browser-tested.
- MCP-required tasks prove the named MCP tool was actually used.

### Phase 9 - A/B dogfood and default rollout

Deliverables:

- Legacy versus skills/milestone runs in isolated workspaces.
- Repeated 7B/8B model measurements.
- Published failure taxonomy and remaining limitations.
- Default enablement only after gates below pass.

Exit gate:

- New harness is measurably better and has no safety regression.
- Legacy fallback remains available for one release cycle.

## 15. Test and Evaluation Matrix

### 15.1 Core execution metrics

Report these for each route, model tier, task size, and project build:

- **Plan validity:** valid preflight plans / attempted preflight plans.
- **Apply success:** cleanly applied transactions / attempted transactions.
- **First-pass step success:** verified transactions passing before repair /
  attempted verified transactions.
- **Bounded eventual step success:** verified transactions passing within the
  allowed repair budget / attempted verified transactions.
- **Repair success by attempt:** solved on attempt one, two, later, or never.
- **False success:** success outcomes missing their required verifier or
  acceptance evidence / all success outcomes.
- **Tool pressure:** results above 1K and 2K tokens, truncation rate, and tokens
  removed before model context.
- **Plan and context grounding:** unknown file/symbol references and omitted
  required evidence.
- **PRD coverage:** verified requirements / in-scope requirements.
- **Cost to completion:** model calls, tool calls, repair calls, elapsed time,
  and tokens for a successful task.

Keep one-pass and eventual completion separate. Checkpoint/resume can improve
eventual completion and reduce repeated work without changing first-pass step
success, and both effects matter.

### 15.2 Deterministic tests

- Skill discovery, precedence, validation, dependencies, and conflicts.
- Skill context token budgeting and progressive loading.
- Task and milestone state transitions.
- Requirement source and acceptance mappings.
- Context inclusion/omission records.
- Tool restriction and permission non-escalation.
- Transaction rollback and resume.
- Verification outcome truthfulness.
- Action-ledger schema and backward compatibility.
- Legacy behavior when all new flags are disabled.

### 15.3 Live 7B/8B scenarios

Run each at least three times in a fresh workspace:

1. Answer a repository question with exact file evidence.
2. Make a one-file targeted edit and run a focused test.
3. Fix a multi-file bug with a failing command.
4. Perform a read-only web search with citations and no mutations.
5. Use a real external MCP tool and report its exact name.
6. Build a small application from a short PRD.
7. Build a medium full-stack application from a structured PRD.
8. Build the AtlasOps application from the long PRD.
9. Stop and resume an in-progress PRD build.
10. Inject build, type, test, dependency, and browser failures.

### 15.4 Product scoring

Score more than whether `npm run build` passed:

- Requested requirements implemented.
- Acceptance checks passed.
- Runtime journeys work.
- Persistence is real when required.
- Security/permission rules hold.
- No collateral file changes.
- No false-success outcome.
- Logs are complete and redacted.
- Model calls, elapsed time, repairs, and tokens stay within budget.
- Human-visible quality is acceptable for UI tasks.

## 16. Quality Gates for "Outperforms Current"

Use the same model, machine, PRD, approval policy, and sample count for both
versions. The new harness becomes default only if all of these hold:

- No deterministic test regression.
- No safety, permission, sandbox, undo, MCP, or logging regression.
- Zero false-success results in the benchmark suite.
- 100% of mutation tasks identify their required verifier or explicitly state
  that no deterministic verifier exists.
- 100% of model calls have context and skill references.
- 100% of mutations have transaction and provenance records.
- First-pass and bounded eventual verified-transaction success improve over the
  frozen Phase 0 baseline; set exact thresholds after the rapid telemetry pass
  and freeze them before implementation evaluation.
- Large model-facing tool results include original/returned token counts,
  truncation status, and an artifact reference when truncated.
- At least 95% correct skill selection on the benchmark set.
- At least 20 percentage points better end-to-end completion on medium/long PRDs,
  or at least 30% fewer failed milestones if baseline completion is already high.
- At least 20% better acceptance-requirement coverage.
- At least 25% fewer repair calls per successful project.
- No more than 15% median latency increase for ordinary short coding tasks.
- Ordinary Q&A and simple edits remain within 2 percentage points of baseline.

The exact thresholds may be tightened after Phase 0, but they must be fixed
before evaluating the implementation to avoid tuning the score after the fact.

## 17. Main Risks and Controls

| Risk                                           | Control                                                                 |
| ---------------------------------------------- | ----------------------------------------------------------------------- |
| Skill instructions bloat context               | Metadata-first discovery, active-skill cap, token budgets               |
| Conflicting skills                             | Dependency/conflict validator and deterministic precedence              |
| Workspace skill executes unsafe code           | No arbitrary in-process hooks; scripts use approvals                    |
| Model drops PRD requirements                   | Stable requirement ledger and final traceability audit                  |
| Templates produce generic apps                 | Keep templates to foundation; model owns product slices                 |
| Hidden postprocessing overstates model ability | Log provenance and move stack behavior into visible skills              |
| Repair loop damages working code               | Milestone transactions, focused checks, rollback, attempt limits        |
| Full logs leak secrets                         | Existing redaction plus artifact-level secret tests                     |
| Eval overfits one PRD                          | Multiple stacks, sizes, failure injections, and unseen holdout PRDs     |
| Parallel steps create interface drift          | Serialize mutations sharing interfaces; parallelize read-only work only |

## 18. Recommended Pull Request Sequence

1. Rapid telemetry counters and an aggregate execution-reliability report.
2. Universal required-verifier identity and evidence-owned outcomes.
3. Structured large tool-result artifacts and canonical repair-attempt records.
4. Re-measurement plus a focused fix for the worst observed failure class.
5. Skill types, loader, catalog, validation, and inspection commands.
6. Shadow skill router, context accounting, and ledger records.
7. PRD compiler, requirement ledger, and acceptance matrix.
8. Versioned milestone state machine and resume behavior.
9. Developer and React/Vite skill integration with legacy parity.
10. UI, SQLite, testing, web, and MCP skills.
11. Repeated 7B/8B dogfood, rollback test, and default rollout.

Each PR should leave the full suite green and should be independently reversible.

## 19. First Implementation Slice

The safest first coding slice is deliberately small:

1. Define a verified transaction and required-verifier identity.
2. Derive patch/apply, verification, repair, false-success, and tool-pressure
   metrics from existing run artifacts where possible.
3. Add only the missing counters to the action ledger.
4. Record original/returned tool-result tokens and truncation decisions.
5. Produce one aggregate report for the current evals and three existing PRD
   build runs.
6. Identify the worst measured failure class before changing behavior.

The second slice then universalizes the existing diagnostic, repair, and
verification path for that failure class. Skill package discovery begins only
after the current executor can be measured and trusted. This first slice makes
no skill, milestone, routing, permission, or generation behavior change.

## 20. Definition of Done

This program is complete when:

- Users can install bundled, personal, and workspace skills.
- SHAMSU activates a small relevant skill set and explains the selection.
- The developer skill is the default for coding work.
- Long PRDs compile into traceable requirements and resumable milestones.
- A 7B/8B model works on one bounded, grounded step at a time.
- Framework scaffolding, tools, permissions, verification, and rollback remain
  deterministic and auditable.
- Every final claim maps to tool, mutation, command, test, browser, or acceptance
  evidence.
- The new path beats the frozen baseline under repeated end-to-end tests.
- Disabling the new path restores the previous behavior for rollback.
