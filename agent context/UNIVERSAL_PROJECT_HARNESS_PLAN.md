# Universal Project Harness Plan

Status: Phase 9 reopened after mixed Django/React Canvas Lite dogfood exposed
remaining architecture and repair-loop failures
Branch: `fix/universal-project-harness`

## Objective

Make SHAMSU reliably build and modify many project types with local 7B/8B
models. The harness owns document grounding, task decomposition, bounded
context, mutations, verification, recovery, and completion claims. Models work
on small evidence-backed units rather than carrying the whole project in one
conversation.

Canvas Lite is a regression fixture, not a special implementation target.

## Phase 0 - Baseline And Regression Contracts

Status: reopened

- Backtick-quoted and spaced document paths resolve exactly.
- Image-only documents use a local OCR adapter after native extraction fails.
- Long structured project builds bypass generic sentence splitting.
- One-target creation requests remain one atomic operation.
- Context budgets include system, summary, history, and output reserves.
- Required verification failure overrides successful mutation evidence.

Observed failed-build baseline:

- Explicit `canvas lite.pdf` was parsed as `lite.pdf`.
- Native PDF extraction returned five blank pages.
- A project build became 12 sentence-fragment operations.
- Executor prompts grew from 16,319 to 116,435 characters.
- Model work consumed 595.6 seconds before the 600-second request timeout.
- Four isolated files were written; no runnable project existed.
- Syntax-only checks allowed invalid framework configuration to survive.

## Phase 1 - Universal Input Resolution

Status: complete

Resolve exact workspace artifacts from backticks, quotes, `@mentions`, and
relative paths. A document does not need `prd` in its filename. Produce a
normalized artifact with type, content, citations, tables, images, confidence,
and warnings.

## Phase 2 - Document Extraction

Status: core complete; visual evidence interpretation pending

Use native text and table extraction first. Render and OCR only pages without
usable text. Use optional vision interpretation only for diagrams, wireframes,
or screenshots whose meaning is not textual. Cache normalized evidence by file
hash and stop on low-confidence grounding.

Implemented: native/OCR page selection, local PDF rendering, hash-based OCR
cache, page provenance, confidence/warnings, and a low-confidence build stop.
Pending: a capability-gated vision adapter for non-textual diagrams and
wireframes; OCR remains the default for text-bearing scanned pages.

## Phase 3 - Stable Request Routing

Status: complete

Classify project creation and extension before generic composite parsing. A
resolved specification plus build intent selects `project.build`. Restrict
clause splitting to short, genuinely independent commands.

## Phase 4 - Project Contract And Capability Adapter

Status: complete

Normalize requirements, workflows, security, data, interfaces, deliverables,
constraints, and acceptance criteria into a technology-neutral project
contract. Select an adapter for existing repositories, web apps, APIs, CLIs,
libraries, data projects, or installed platform capabilities.

## Phase 5 - Requirement-Led Milestones

Status: core complete; freeform execution integration pending in Phase 7

Enable the existing requirement ledger, milestone graph, checkpoint, repair,
rollback, and resume machinery by default for complex builds. Each milestone
owns requirement IDs, expected files, mutation scope, dependencies, and a
deterministic verifier.

Implemented: automatic complex-build detection, complete cross-cutting
requirement capture, dependency-safe milestone graphs, checkpoints, bounded
repair, rollback, resume, and 12-requirement capsule limits. Pending: route the
structured freeform project generator through these checkpoints in Phase 7.

## Phase 6 - Small-Model Context Capsules

Status: complete

Give each internal call only the current milestone, relevant requirements,
implicated files, preserved interfaces, latest primary diagnostic, and
completion criteria. Do not hydrate the full user conversation for internal
milestones. Include every prompt component in the budget and prevent context
growth between retries.

## Phase 7 - Harness-Owned Mutations And Recovery

Status: complete

Accept validated structured file bundles for multi-file generation and apply
them through the transaction and approval layer. Keep native tools for
exploration and focused edits. Permit at most two evidence-changing repairs,
then rollback and checkpoint the blocker.

Existing foundation: the freeform generator already uses structured JSON,
sandboxed paths, per-file transactions, deterministic verification, and bounded
repair.

Implemented: complex freeform builds now consume compiled milestone capsules,
generate exact-path bundles of at most three files, obtain approval before each
bundle, apply the whole bundle in one transaction, run deterministic structural
checks, rollback the complete bundle on failure, and stop after two attempts.
Durable checkpoints record implementation, repair, rollback, final verification,
and resume completed bundles without regenerating them. Simple projects retain
the existing lightweight per-file path.

## Phase 8 - Authoritative Verification

Status: complete

Choose stack-aware setup, compile, migration, seed, test, integration, and
browser checks. A write is never completion evidence by itself. Final success
requires every mandatory requirement and verifier to pass, with a responding
preview URL when applicable.

Implemented: verification failures now override mutation evidence for every
operation kind. The deterministic gate discovers declared Node typecheck,
build, migration, seed, test, integration, browser/e2e, and lint scripts; Django
system, migration, seed, and test checks; and native Cargo and Go checks.
Required-but-unverified Definition-of-Done items block completion distinctly
from failed checks. Project-declared browser/e2e scripts provide the applicable
live-preview evidence without guessing or launching an unverified server command.

## Phase 9 - Evaluation And Release

Status: reopened after Canvas Lite mixed-stack validation

Run fixtures for full-stack apps, APIs, frontends, CLIs, libraries, existing
project edits, text PRDs, scanned PDFs, and visual specifications. Release only
when routing is deterministic, prompts remain bounded, false success is zero,
and interrupted builds resume from verified checkpoints.

Implemented and validated:

- The deterministic release harness passed Python, Django, Node, React, and
  mixed-stack dogfood, with 1.17-second startup, 1.33-second cold first answer,
  89.8 MB peak RSS, and bounded log growth.
- A real local-model medium Python CLI PRD build passed project-local setup,
  syntax, pytest, and five acceptance commands in 57.9 seconds.
- A real local-model long React/full-stack PRD build passed generation and
  external acceptance in 109.0 seconds.
- The aggregate release telemetry recorded 26/26 successful mutation applies,
  11/11 passing verification events, zero false-success candidates, zero
  success-without-verification runs, and no tool pressure or missing token
  telemetry across the two live builds.
- The repository release gate passed Ruff and 1,837 tests, with one intentional
  skip. Routing, document normalization, OCR fallback, milestone resume,
  bounded prompts, rollback, and verification-failure precedence all have
  deterministic regression coverage.

Release note: native extraction and OCR cover textual and scanned PRDs. Purely
visual diagrams and wireframes still require a configured vision-capable
provider; the harness does not pretend OCR can infer their semantics.

Post-validation finding (2026-08-01): the medium CLI and long React fixtures
remain green, but the scanned Canvas Lite Django/React PRD does not meet release
quality. Bundle generation now completes after adaptive split fallback and
focused one-file repair recovers correctly, but the generated mixed-stack
architecture is inconsistent: the root hardener creates an unrelated generic
React/Atlas dashboard, the nested frontend cannot install, and the Django
backend cannot pass its system check. Phase 9 remains open until a fresh
mixed-stack build passes backend setup/tests, frontend tests/build, database
seed, browser workflows, and PRD acceptance without manual code authorship.
