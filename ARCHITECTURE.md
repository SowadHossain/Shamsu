# SHAMSU v2 — Architecture

Companion to [`docs/migration/v2-full-rebuild-plan.md`](docs/migration/v2-full-rebuild-plan.md),
which remains the authoritative specification. This document explains how the
pieces fit together and, where it matters, why they are shaped that way.

---

## 1. The inversion

SHAMSU v1 gave the model the loop. It decided what to do next, when it was
done, and what "done" meant. The runtime supplied tools and reacted.

That design has a specific failure mode, and v1 hit it: every observed problem
gets patched with another guard. v1 accumulated at least fifteen independent
per-run recovery counters inside a single loop file — identical-call repeats,
read-failure recoveries, prose-only corrections, stall answers, empty
responses, missing-mutation recoveries, truncation recoveries. Each was
reasonable alone. Together they formed a control system nobody had modelled,
inside a loop with no cancellation path.

v2 inverts the relationship:

```text
        v1                              v2
  ┌───────────┐                   ┌───────────┐
  │   Model   │ drives            │  Runtime  │ drives
  └─────┬─────┘                   └─────┬─────┘
        │ asks for tools                │ asks one narrow question
  ┌─────▼─────┐                   ┌─────▼─────┐
  │  Runtime  │ reacts            │   Model   │ answers
  └───────────┘                   └───────────┘
```

The model still does the reasoning. It no longer owns the control flow, the
completion decision, or the definition of success.

---

## 2. Component map

```text
┌────────────────────────────────────────────────────┐
│ CLI / API                                          │
└────────────────────────┬───────────────────────────┘
                         │
┌────────────────────────▼───────────────────────────┐
│ Run Controller                    runtime/         │
│ registration · cancellation · feedback · pause     │
│ wall-clock limits · status · events                │
└────────────────────────┬───────────────────────────┘
                         │
┌────────────────────────▼───────────────────────────┐
│ Typed Agent Runtime               runtime/ agent/  │
│ state transitions · classifier · planner           │
│ step executor · repair · approval · completion     │
└──────────┬────────────────────────────┬────────────┘
           │                            │
┌──────────▼──────────┐      ┌──────────▼────────────┐
│ Context Compiler    │      │ Tool Gateway          │
│ context/            │      │ tools/                │
│ artifacts · facts   │      │ validate · phase gate │
│ code index · state  │      │ approve · execute     │
│ fresh results       │      │ cap output · evidence │
└──────────┬──────────┘      └──────────┬────────────┘
           │                            │
┌──────────▼──────────────────┐  ┌──────▼────────────┐
│ SQLite State Store  state/  │  │ Workspace         │
│ Artifact Store  artifacts/  │  │ security/         │
│ Code Index code_intelligence│  └───────────────────┘
└─────────────────────────────┘
```

Everything crosses a Protocol defined in `src/shamsu/interfaces/`. Concrete
implementations are wired once, at the composition root.

---

## 3. The state machine

The runtime owns transitions. The model proposes; the runtime decides.

```text
RECEIVE_TASK → LOAD_PROJECT_STATE → INSPECT_PROJECT → CLASSIFY_TASK
                                                          │
                                     ┌────────────────────┴─────┐
                                  DIRECT                     PLANNED
                                     │                          │
                                     │             CREATE_PLAN → VALIDATE_PLAN
                                     │                          → APPROVAL_CHECK
                                     └────────────┬─────────────┘
                                                  ▼
                                        EXECUTE_CURRENT_STEP
                                                  ▼
                                        VERIFY_CURRENT_STEP
                    ┌──────────────┬──────────────┼───────────┬─────────────┐
                  PASS        REPAIRABLE    PLAN_INVALID  APPROVAL      CANCELLED
                    │              │              │       REQUIRED       BLOCKED
             CREATE_CHECKPOINT  REPAIR         REPLAN       WAIT        STOP/REPORT
                    ▼
            CHECK_REMAINING_STEPS ──more──► EXECUTE_CURRENT_STEP
                    │ none
                    ▼
            FINAL_VERIFICATION → COMPLETION_GATE → FINAL_REPORT
```

Nodes are `interfaces.AgentState`; outcomes are `interfaces.StepOutcome`.

### Bounded inner loop

Each step runs a bounded ReAct-style loop:

```text
compile step context → model selects one action → validate → policy check
→ execute one tool → compress observation → register evidence
→ verify progress → continue | repair | replan | stop
```

Initial limits (plan §11), all enforced by the runtime:

| Limit | Value |
|---|---:|
| Actions per step | 4 |
| Repair attempts per step | 2 |
| Re-plans per task | 2 |
| Consecutive failed actions | 3 |
| Mutating tool calls per model decision | 1 |
| Logical actions per model turn | 1 |
| Long-running mode | disabled |
| Automatic production actions | disabled |

Unlike v1, these live in one place. "How much work can this task cause?" has a
single answer.

---

## 4. Cancellation

This is the defect that most directly motivated the rebuild.

v1 implemented a full control plane — `register_run`, `cancel_run`,
`add_feedback`, in-flight model-task cancellation — in `runtime/run_control.py`,
and the live loop never observed any of it. `cancel_run` could be called and
nothing would happen. The only occurrence of "cancel" in the live loop cancelled
a heartbeat task.

The v2 fix is structural rather than a better implementation:

- `CancellationToken` is a **parameter** on every blocking call — tool
  execution, model generation, artifact generation.
- A component that can block is therefore *statically obliged* to accept one.
  A component that does not take a token is one that is not allowed to block.
- `Cancelled` and `FeedbackInterrupt` are distinct exception types, so
  "the user cancelled" and "feedback interrupted this call" never have to be
  disambiguated by inspecting whether some event happened to be set.
- `wait_cancelled()` lets a caller *race* a long call against cancellation
  rather than only polling between calls.

See `src/shamsu/interfaces/cancellation.py`.

---

## 5. Memory, in four layers

Graphiti is off the critical path (plan §2). Not because graph memory is a bad
idea, but because it was another service to keep healthy, extra model calls per
task, significant CPU and RAM, harder debugging — and the integration was not
trusted to be correctly wired into the live runtime anyway.

| Layer | Holds | Storage |
|---|---|---|
| 1 — Authoritative runtime state | task, plan, step, approvals, tool events, evidence, checkpoints | SQLite |
| 2 — Project knowledge | architecture decisions, conventions, stack, constraints | SQLite + files |
| 3 — Code artifacts | repo map, module and symbol cards, dependency/route/schema/test maps | `.shamsu/artifacts/` |
| 4 — Semantic retrieval *(optional, later)* | docs and old-task fallback search | local embedding index |

Layer 4 never replaces structural retrieval. It is stage 9 of 9.

---

## 6. Artifacts: how a large repository fits in a small model

An artifact converts repository structure into a unit small enough to prompt
with. The discipline that makes them safe:

- **Structural facts come from deterministic analysis.** Parsers, git, test
  discovery, manifest readers. A model may add a prose summary; it may never be
  the source of a symbol name, a path, or a dependency edge.
- **Every artifact carries its sources and their content hashes**, so freshness
  is computable without asking anyone.
- **Confidence is recorded**, so parsed facts outrank model-written summaries
  when the compiler is choosing what to include.
- **Generator version is tracked separately from artifact version**, because a
  generator change invalidates artifacts whose sources never changed.

Statuses: `fresh · stale · invalidated · missing · generation_failed`.

Only `fresh` and `stale` may reach the model, and `stale` must be labelled —
`ContextSection.stale_warning` exists precisely so it cannot be forgotten.

### Contradiction handling

When a fresh tool result disagrees with an artifact, the resolution is fixed and
not negotiable:

```text
fresh result wins → artifact invalidated → contradiction recorded
                 → regeneration queued
```

The recorded rate is the `artifact_freshness_error_rate` evaluation metric.

---

## 7. Retrieval order

```text
1 exact path        2 exact text        3 symbol index
4 reference graph   5 call graph        6 related tests
7 dependency graph  8 git history       9 semantic fallback
```

Structural questions have correct answers. An embedding similarity score is not
one. Semantic search runs only after structural retrieval returns nothing, and
degrades to "no hits" on failure rather than raising.

---

## 8. The context compiler

The model never sees a transcript. Each call gets a frame assembled from
authoritative state under a token budget:

```text
[PHASE] [CURRENT TASK] [CURRENT STEP] [ACCEPTANCE CRITERIA] [PROJECT FACTS]
[RELEVANT ARTIFACTS] [RELEVANT SOURCE CODE] [LATEST OBSERVATION]
[PREVIOUS STEP SUMMARY] [ALLOWED TOOLS] [OUTPUT CONTRACT]
```

Default 8K budget:

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

Tiers: **hot** context is never dropped (current task, step, criteria, latest
result, relevant code, allowed tools); **warm** is included when it fits; **cold**
is retrieved only on demand. Anything dropped for budget is recorded in
`ContextFrame.dropped_sections` — a decision made without the source code
section is a different kind of decision, and the telemetry should say so.

Compilation is deterministic: the same state produces the same frame. That is
what makes a bad decision reproducible.

---

## 9. Tools

One registry, not two. v1 had `ToolRegistry` (3 tools) and `AgentToolRegistry`
(42 tools, 3,731 lines) with different validation and gating, so "is this
allowed right now?" had two answers depending on which surface you asked.

Every tool declares a `ToolContract`: allowed phases, risk, approval
requirement, reversibility, timeout, output cap, evidence produced, artifacts
invalidated, and whether it mutates. The gateway enforces all of it. None is
advisory.

Two properties worth calling out:

- **The model is only shown tools reachable in the current phase.** A
  wrong-phase call is therefore a runtime bug, not an expected model mistake.
- **Output is capped before entering context**, never after. v1's comment on
  this was precise and correct: the budget-aware trimmer always keeps the most
  recent message, so one oversized read survives trimming and crowds out
  everything else. Kept, because the failure mode is real.

Initial surface: `project.inspect`, `code.search`, `file.read`, `file.patch`,
`test.run`, `git.inspect`, `git.checkpoint`.

Logical tools may fan out internally. `git.inspect` collects branch, status,
changed files, relevant diff, untracked files, and recent commits in one call —
the model should not be picking among low-level git commands for ordinary work.

---

## 10. Evidence and completion

The model may *propose* that a step is done. The runtime accepts only when the
required evidence exists.

| Claim | Required evidence |
|---|---|
| File modified | successful patch + git diff |
| Tests pass | required test command succeeded |
| Build succeeds | build command succeeded |
| App runs | health checks + smoke tests passed |
| Migration succeeds | migration + schema verification passed |
| Task complete | every acceptance criterion has verified evidence |

```text
required_evidence ⊆ verified_evidence
```

Evidence is registered by the runtime after a tool actually produced it. It is
never registered because a model asserted it. This replaces v1's implicit
success classification, and it is what the `false_success_rate` metric measures.

---

## 11. Failure and repair

Failures are classified into a fixed taxonomy (`interfaces.FailureKind`), then:

```text
classify → generate failure capsule → identify affected step
→ repair or roll back → bound the attempts → verify again
→ stop when progress is not improving
```

Repair may only touch failure-related files — "related" decided by the code
index's impact analysis, not by the model's judgement. Same-error detection
stops repeated repairs: two identical error signatures in a row ends the
attempt rather than spending the remaining budget.

---

## 12. Safety

`security/` is a **policy layer, not an OS sandbox** — the same honest caveat
v1 carried. It provides path sandboxing, command risk classification, and
secret redaction. Real workspace isolation (CPU, memory, disk, process limits,
restricted filesystem and network, no host Docker socket) is a deployment
concern described in plan §24.1.

Commands are structured, not shell strings:

```json
{"program": "pytest", "args": ["tests/auth", "-q"],
 "cwd": "/workspace/project", "timeout_seconds": 120}
```

---

## 13. What success looks like

Reliability is measured, not asserted. The metrics that matter most are the
ones that would have caught v1's problems:

- `false_success_rate` — claimed done, was not
- `success_without_verification_rate` — completion without evidence
- `stale_context_usage_rate` — decided on out-of-date structure
- `artifact_freshness_error_rate` — artifact contradicted reality
- `verified_task_success_rate`, `repair_success_rate`, `rollback_rate`
- `tokens_per_verified_task`

Plus an adversarial suite: prompt injection in repository docs, destructive
shell requests, contradictory decisions, stale artifacts, huge output, repeated
failing repair, path escape attempts.

---

## 14. Standing rules

1. The runtime controls the loop.
2. The model performs one narrow decision at a time.
3. SQLite is authoritative.
4. Artifacts are the primary long-codebase compression mechanism.
5. Structural retrieval precedes semantic retrieval.
6. Graphiti is optional and deferred.
7. The complete repository never enters model context.
8. Every artifact is versioned and traceable.
9. Fresh tool results override stale artifacts.
10. Completion requires verified evidence.
11. Legacy code is a donor, not a dependency.
12. New development happens only under `src/shamsu/`.
13. Long-running autonomy stays disabled until evaluations justify it.
14. Safety, cancellation, checkpointing, and recovery are runtime features.
15. Reliability matters more than autonomy.
