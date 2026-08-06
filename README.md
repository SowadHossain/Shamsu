# SHAMSU v2

A local-first autonomous coding agent built around a typed runtime and small
local language models. No cloud inference.

> **Status: pre-alpha (`2.0.0a0`).** The runtime foundation is under
> construction. v2 is not yet usable as an agent. The previous working
> implementation is archived under [`legacy-code/`](legacy-code/) — see
> [`legacy-code/LEGACY_README.md`](legacy-code/LEGACY_README.md).

---

## The idea

> The runtime controls the loop. The model performs one narrow decision at a
> time. Complete information lives outside the model; the context compiler
> selects only what the next decision needs.

SHAMSU v1 inverted this: the model drove the loop and the runtime reacted. That
worked, but every reliability improvement meant another inline guard counter,
and the design ran out of room. v2 moves orchestration into the runtime, makes
state authoritative in SQLite, and gates completion on evidence rather than on
the model's say-so.

The model never receives the repository, the conversation, or the memory
system. It receives a compiled frame built for one decision under an explicit
token budget.

## Design commitments

| | |
|---|---|
| **Retrieval before inference** | Deterministic tools find the context; the model reasons over it. Never dump a codebase into a prompt. |
| **SQLite is authoritative** | Artifacts, memory, and model output are derived or advisory. If it is not in SQLite, it is not a fact. |
| **Completion requires evidence** | `required_evidence ⊆ verified_evidence`. No evidence, no completion. |
| **Fresh results beat stale artifacts** | A contradiction invalidates the artifact and is recorded, not reconciled. |
| **Structural before semantic** | Embeddings are a last-resort fallback, never a replacement for a symbol lookup. |
| **Every run is cancellable** | Cancellation is a parameter threaded through every blocking call, not a flag someone remembers to check. |
| **Honest failure over fabrication** | `ok=False` beats an invented fact. |
| **No Graphiti on the critical path** | SQLite plus repository artifacts. Reconsidered only under the gate in the plan's §33. |

## Repository layout

```text
src/shamsu/          v2 production code — the only place new work happens
  interfaces/        Protocols for every seam. Depend on these.
  state/             Typed records and the SQLite store
  runtime/           Run controller, state machine, execution limits
  agent/             Classifier, planner, step executor, repair, completion
  context/           The context compiler
  artifacts/         Versioned, hash-traceable repository artifacts
  code_intelligence/ Structural retrieval: symbols, references, call graph
  tools/             The typed tool gateway
  verification/      Evidence collection and the verification pipeline
  memory/            Project facts, decisions, failure lessons
  models/            Local model clients and output contracts
  security/          Path sandbox, command policy, secret redaction
  telemetry/         Events, metrics, reliability tracking

tests/               unit · integration · evals · adversarial · fixtures
docs/                architecture · decisions · migration · development · protocols
scripts/             Development and enforcement tooling
legacy-code/         Archived v1. Reference and donor only — never imported.
```

## Development

Requires Python ≥ 3.11.

```bash
pip install -e ".[dev]"

ruff check src/ tests/ scripts/       # lint
ruff format src/ tests/ scripts/      # format
mypy                                  # type check (strict)
pytest tests/                         # tests
python scripts/check_import_boundary.py --root .   # legacy boundary
```

### No models in the test suite

Nothing in `tests/` contacts a model. The suite runs against
`tests/fixtures/fake_model.py`, a deterministic `ModelClient` with scripted
responses. This keeps CI hermetic and reproducible, and it is possible only
because the runtime — not the model — holds the system together.

Live local inference is exercised separately, on a GPU-equipped machine.

### The legacy boundary

v2 must never import from `legacy-code/`. This is enforced, not merely
documented:

- `scripts/check_import_boundary.py` fails CI on any import, `sys.path`
  injection, or path literal referencing the archive.
- `ruff` bans the module names.
- `pytest` excludes the directory from collection.
- The wheel does not ship it.

To reuse v1 logic, migrate it through the ten-step process in
[`LEGACY_COMPONENTS.md`](LEGACY_COMPONENTS.md). Copying or rewriting behind a
clean v2 interface is the supported path; importing is not.

## Documentation

| Document | Contents |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | How the runtime fits together |
| [`docs/migration/v2-full-rebuild-plan.md`](docs/migration/v2-full-rebuild-plan.md) | The full rebuild plan — the authoritative spec |
| [`MIGRATION_STATUS.md`](MIGRATION_STATUS.md) | Milestone and PR progress |
| [`LEGACY_COMPONENTS.md`](LEGACY_COMPONENTS.md) | Per-component migration ledger |
| [`legacy-code/LEGACY_README.md`](legacy-code/LEGACY_README.md) | What v1 was and what went wrong |
| [`CHANGELOG.md`](CHANGELOG.md) | Release history |

## Branching

Feature branches target `develop`. Do not push directly to `main`.
The rebuild happens on `shamsu-v2.0.0`.
