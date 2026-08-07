# SHAMSU v2 — Handoff

**Branch:** `shamsu-v2.0.0` · **Head:** `565566c` · **Date:** 2026-08-07

Everything in the rebuild plan's PR sequence is done, plus two things the plan
never scheduled (an orchestrator and a terminal interface). This document is
what you need to pick the work up somewhere else.

| | |
|---|---|
| Tests | **858 passing** |
| Source | 68 files, ~15,700 lines under `src/shamsu/` |
| `mypy --strict` | clean, **zero `type: ignore` in `src/`** |
| `ruff` + format | clean |
| Import boundary | clean — no production import from `legacy-code/` |

---

## 1. The one-line summary

SHAMSU v2 can run a task end to end — inspect, plan, patch, test, verify,
checkpoint, report — with a completion gate that opens only on evidence
produced by observed tool executions. **It cannot yet talk to a real model.**

---

## 2. What is blocking everything

**There is no inference backend.** `src/shamsu/models/` contains contracts,
output normalisation, and a scripted client. Nothing implements `ModelClient`
against a real model, and `shamsu --model ollama` raises `NotImplementedError`
with a message saying so.

So the runtime, the gate, the planner, the repair controller, the code index,
the memory layer, and the interface all work — and the agent cannot do a single
real thing until this exists.

**Writing it is doable anywhere. Validating it needs your GPU.**

What the client has to satisfy is `shamsu.interfaces.models.ModelClient`:

```python
name, context_tokens                # properties
count_tokens(text) -> int           # must work without a live model
generate(request, cancel)           # honours the token mid-call
generate_typed(request, contract, cancel)   # parses, never repairs
```

Three things to get right, in order of how badly they bite:

1. **`generate_typed` must route through `shamsu.models.normalization.
   parse_json_response`.** That function strips `<think>` spans and code
   fences and then parses *once*. There is deliberately no salvage layer —
   v1's was 1,159 lines and PR 15 removed it on purpose. A response that does
   not satisfy its contract raises `ModelContractError`, which the runtime
   already handles as a bounded retry.
2. **Cancellation must reach an in-flight HTTP request.** `cancel` is a
   `CancellationToken`; race `cancel.wait_cancelled()` against the request the
   way `tools/testing.py:_spawn` races a subprocess. v1 had no mid-run
   cancellation at all and that is the single defect that most motivates v2.
3. **`count_tokens` is called by the context compiler while building a frame**,
   long before any request is sent. It must not need a live model. An
   approximation is fine as long as it is stable across runs.

`src/shamsu/models/scripted.py` is a working reference implementation of the
whole protocol.

---

## 3. What to do next, in order

### 1. Ollama client — `src/shamsu/models/ollama.py`

The blocker. See §2. Everything else is judged through it.

### 2. Approval path

`AgentState.WAIT_APPROVAL` currently returns `STOPPED` with *"a step requires
approval and no approver is configured"*. Honest, but it means a `HIGH`-risk
step **cannot proceed at all** — and there is now a terminal interface that
could ask.

The pieces exist: `ApprovalRecord`, `store.request_approval`,
`store.decide_approval`, `store.pending_approvals`, and
`ToolGateway(approval=...)` whose default is `deny_all`. What is missing is a
prompt in `ui/` and a handler in `runtime/session.py:_dispatch`.

Note `ApprovalDecision.TIMED_OUT` is a distinct decision from `APPROVED`.
Silence is never consent.

### 3. `check.run` — closes the §26 verification pipeline

Seven of eleven `EvidenceKind` members have **no tool that can produce them**:

```
build_succeeded  lint_passed  typecheck_passed
health_check_passed  smoke_test_passed  migration_applied  schema_verified
```

The consequence is concrete: `CLAIM_REQUIREMENTS` maps `build_succeeds`,
`app_runs`, and `migration_succeeds` onto evidence nothing can generate, so a
plan step requiring any of them **can never complete**. The gate refuses
forever, correctly and uselessly.

The first three are cheap: one `check.run` tool shaped exactly like
`test.run` — an allowlist of command *keys*, never a shell string (plan §24.3).
The last four belong to milestones 11–12.

### 4. §31.1 evaluation suite — 7 tasks

Documentation edit · single-file bug fix · add one unit test · fix one failing
test · small multi-file feature · refactor one function · add one API
validation rule.

This is §32's **last unmet property**: *"the agent can complete the initial
evaluation suite consistently."* `tests/evals/` currently holds only the
retrieval eval.

Build the harness anywhere; **the numbers only mean something on the GPU.** A
scripted model always emits valid JSON, so running the suite against
`FakeModelClient` measures the runtime and tells you nothing about the model.

### 5. §31.2 adversarial suite — 10 scenarios

Malicious instruction in repository docs · destructive shell request ·
contradictory architecture decision · stale artifact · huge terminal output ·
repeated failing repair · missing environment variable · unrelated pre-existing
test failure · tool schema mistake · attempted path escape.

Several have component-level tests already (`tests/adversarial/`). None run
through `AgentSession`.

### Held deliberately

- **Interactive session** (REPL, follow-up turns, `/` commands). A nice-to-have
  on a tool that cannot do anything yet.
- **Milestones 10–15** (packages, Docker, databases, PRD workflows,
  multi-service, tiny-OS). Adding capability before there is a measured success
  rate means you cannot tell whether the next change helped.

---

## 4. Things that are not obvious from the code

### The standing constraint

**Do not run local models on the VPS.** No Ollama, no embeddings, no inference.
Build against the Protocols in `src/shamsu/interfaces/` and the deterministic
fakes. Live inference is tested separately on a GPU machine — say so rather
than reporting untested inference work as verified.

### Environment traps on this box

- **`python3` only, no `python`.** Many legacy tests shell out to `python -c`;
  without a symlink you get ~10 spurious failures.
- **PEP-668 externally managed, no `python3-venv`.** `pip install` and
  `python -m venv` both fail. Use `pip install --target <dir>` and
  `PYTHONPATH=<dir>`. `mypy` lives at `.tools/pylibs` (gitignored):

  ```bash
  PYTHONPATH=/home/shamsu/Shamsu/.tools/pylibs python3 -m mypy
  ```

- **Stale `__pycache__` under `legacy-code/`** makes every traceback show
  `???`. `git mv` preserved mtime and size, so Python loaded pre-move bytecode.
  Delete it.

### Verification commands

```bash
ruff check src/ tests/ scripts/
ruff format --check src/ tests/ scripts/
PYTHONPATH=.tools/pylibs python3 -m mypy
python3 -m pytest tests/
python3 scripts/check_import_boundary.py --root .
```

### Running it

```bash
PYTHONPATH=src python3 -m shamsu --model fake "fix the adder"          # TUI
PYTHONPATH=src python3 -m shamsu --model fake --no-tui "fix it"        # plain
```

---

## 5. Design decisions worth not re-litigating

Each of these was made deliberately and is documented where it lives.

| Decision | Why |
|---|---|
| **Evidence is non-forgeable** | `EvidenceRecord.source_event_id` is a NOT NULL FK to `tool_events`. There is no path from a model assertion to a row. |
| **The model may raise its own bar, never lower it** | `required_evidence` = what the plan asked ∪ the runtime's floor. The only discount is declaring a step `investigate`, which also removes every mutating tool. |
| **No output repair, ever** | Normalisation removes wrapping and never edits content. A wrong repair produces a *parseable wrong answer*, which is worse than a parse failure. |
| **Test files are protected during repair** | Editing the failing test is indistinguishable from deleting the evidence. `allow_test_edits` is the caller's decision, never the model's. |
| **stdlib `ast`, not tree-sitter** | For Python, `ast` is the language's own parser — exact where tree-sitter is approximate, and no dependency. `CodeIndex` is the seam a tree-sitter backend arrives through. |
| **Metrics are queries, not counters** | v1 incremented at the site that believed it had succeeded, so `false_success_rate` read zero exactly when things were worst. |
| **Facts go stale; decisions do not** | A decision that was made stays made, even when the code it produced has been rewritten. Separate tables, not one with a `kind` column. |
| **The state machine is a table** | `advance_task` validates every move. An illegal transition raises rather than being coerced. |
| **The UI observes; it never drives** | `RunController.subscribe` points one way. Nothing in `runtime/` knows a UI exists. |

---

## 6. Known gaps and honest risks

### Untested, and the biggest one

**Live inference has never run.** Every model interaction in 858 tests is
scripted. Whether a small local model reliably emits valid `InvestigationStep`
and `ImplementationPlan` JSON is *completely unknown* — and PR 15 deliberately
removed v1's salvage layer, so v2 is **by design less tolerant** of malformed
output. That was the right call architecturally. Whether the bar is set at the
right height is exactly what item 1 + item 4 will tell you.

### Known weaknesses

- **An all-`investigate` plan completes having verified nothing.** Correct —
  those steps cannot write — but a model that wants the run over could propose
  one. The report now says `COMPLETE (NOTHING VERIFIED)` and explains, which
  makes it visible rather than fixing it.
- **Repair scope is grounded in traceback frames + the step's declared files**,
  not the call graph. `code_intelligence.related_files_for` exists and would
  widen it, and is deliberately **not wired in** — widening a write scope is a
  safety change that should land on the strength of evaluations.
- **References and callers are matched by name.** Python binding is not
  statically decidable, so the index over-approximates. Safe direction for
  scoping, never proof. `provenance` and `truncated` exist so callers can tell.
- **No resume.** `CheckpointRecord` and `store.resume_task` exist; nothing
  calls them. Plan §12.3 only promises resume at verified step boundaries.
- **`WAIT_APPROVAL` is a dead end** — see §3 item 2.

### Bugs found by building, worth remembering

These are all fixed, but each is a class of mistake that will recur:

1. **Stale bytecode masked a fixed bug.** CPython validates `.pyc` against
   *(mtime in whole seconds, size)*, and `return a - b` → `return a + b`
   changes neither. `test.run` now uses a fresh `PYTHONPYCACHEPREFIX`.
2. **Traceback frames alone cannot scope a repair.** An assertion failure names
   the *test*, not the function that returned the wrong value — that function
   returned normally, so no frame points at it.
3. **`ast.walk` yields a `Call` and the `Name` inside it**, so every call was
   recorded twice.
4. **The transition table had no `BLOCKED` edge from `CREATE_PLAN`.** It
   described a machine that could not fail at planning time. Composing the
   machine for the first time found it.
5. **A second CLI run in the same directory crashed.** `upsert_project`
   conflicts on `project_id`, but `root` is UNIQUE.

The pattern: **four of these five were found by running the thing, not by
reading it.** Prefer that.

---

## 7. Where the documents are

| File | What it holds |
|---|---|
| `docs/migration/v2-full-rebuild-plan.md` | The authoritative spec, 35 sections |
| `MIGRATION_STATUS.md` | Per-PR record with design points and every bug found |
| `LEGACY_COMPONENTS.md` | Migration ledger — what crossed from v1, what was written fresh, what is rejected |
| `legacy-code/LEGACY_README.md` | The honest account of what v1 was and what went wrong |
| `CLAUDE.md` | Agent orientation, invariants, conventions |
| `ARCHITECTURE.md` | Layer map |

---

## 8. If you read only one thing

The prime directive, and every invariant serves it:

> The runtime controls the loop. The model performs one narrow decision at a
> time. Complete information lives outside the model; the context compiler
> selects only what the next decision needs.

And the rule that the whole evidence architecture exists to enforce:

```
required_evidence ⊆ verified_evidence
```

No evidence means no completion. Evidence is registered after a tool produced
it, never because a model asserted it.
