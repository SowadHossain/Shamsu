# SHAMSU — agent orientation

Local-first autonomous coding agent. Inference is local; no cloud AI APIs.

**You are on branch `shamsu-v2.0.0`, a greenfield rebuild.** The working v1
implementation is archived under `legacy-code/` and is *not* the production
code. If a file you are reading lives under `legacy-code/`, it is reference
material, not something to extend.

- **New code goes in `src/shamsu/` only.** Nothing else.
- **Never import from `legacy-code/`.** Enforced by
  `scripts/check_import_boundary.py` in CI.
- Authoritative spec: `docs/migration/v2-full-rebuild-plan.md`.
  Progress: `MIGRATION_STATUS.md`. Design: `ARCHITECTURE.md`.

## Prime directive

> The runtime controls the loop. The model performs one narrow decision at a
> time. Complete information lives outside the model; the context compiler
> selects only what the next decision needs.

Use deterministic tools to find the right context, then use a small local model
to reason over it. Never dump a codebase into a prompt.

## Do not run local models here

The current environment is a VPS with no GPU. Do not invoke Ollama, embeddings,
or any inference path. Build against the Protocols in `src/shamsu/interfaces/`
and the deterministic fake in `tests/fixtures/fake_model.py`. Live inference is
tested separately on a GPU machine — say so rather than reporting untested
inference work as verified.

## Use the knowledge graph before grep/read

This repo is indexed into a `codebase-memory-mcp` knowledge graph. For
structural questions — "who calls X", "what's the architecture", "what breaks
if I change Y" — query the graph. It answers in ~1–2 KB where the equivalent
file reads cost tens of KB.

MCP tools (8, via `.mcp.json`): `search_graph`, `query_graph`, `trace_path`,
`get_code_snippet`, `get_graph_schema`, `get_architecture`, `search_code`,
`index_repository`.

**The graph project name is `home-shamsu-Shamsu`** — required by every tool
except `index_repository`; it is not the directory name.

⚠️ **The graph currently indexes the v1 tree at its old paths.** It was built
before the archival move, so structural answers point at `shamsu/...` rather
than `legacy-code/shamsu/...`, and it knows nothing about `src/shamsu/`.
Re-index before trusting it for v2 work.

Six more tools are **CLI-only** in 0.9.0 (`manage_adr`, `index_status`,
`list_projects`, `detect_changes`, `delete_project`, `ingest_traces`):

```
/root/.shamsu/tools/codebase-memory-mcp/codebase-memory-mcp cli <tool> \
    --project home-shamsu-Shamsu --flag value      # 2>/dev/null — logs to stderr
```

Raw positional JSON is deprecated; use flags, `--args-file <path>`, or stdin.

## v2 layer map (`src/shamsu/`)

- **`interfaces/`** — Protocols for every seam. Depend on these, not on
  concrete classes. Landed: `enums`, `ids`, `cancellation`, `tools`,
  `artifacts`, `models`, `code_intelligence`, `context`. `state` and `runtime`
  protocols land with their record types in PR 3 / PR 4.
- **`state/`** — typed records + SQLite. **Authoritative.** If a fact is not
  here, it is derived or advisory.
- **`runtime/`** — run controller, state machine, execution limits. Owns the loop.
- **`agent/`** — classifier, planner, step executor, repair, completion. Each is
  a bounded controller, not a loop.
- **`context/`** — the context compiler. Builds one frame per decision.
- **`artifacts/`** — versioned, hash-traceable repository artifacts.
- **`code_intelligence/`** — structural retrieval; semantic is stage 9 of 9.
- **`tools/`** — the single typed tool gateway.
- **`verification/`** — evidence and the verification pipeline.
- **`memory/`**, **`models/`**, **`security/`**, **`telemetry/`**.

## Invariants

1. Deterministic retrieval before inference; compact packets, never raw dumps.
2. SQLite is authoritative. Artifacts are derived and invalidatable.
3. Fresh tool results override stale artifacts; contradictions are recorded.
4. Stale artifacts may reach the model only with an explicit label.
5. Completion requires verified evidence: `required ⊆ verified`. Evidence is
   registered after a tool produced it, never because a model asserted it.
6. Every blocking call takes a `CancellationToken`. A component that cannot
   accept one is not allowed to block.
7. **Honest failure over fabrication** — `ok=False` beats an invented fact.
8. Structural facts come from parsers, not from models. A model may summarise;
   it may not be the source of a symbol name, path, or dependency edge.
9. Local-first: no cloud inference.

## Conventions

- Verify with: `ruff check src/ tests/ scripts/`, `ruff format --check ...`,
  `mypy`, `pytest tests/`, `python scripts/check_import_boundary.py --root .`
- No `[project.scripts]` on purpose — a managed launcher lives at `~/.shamsu/bin`;
  a pip console-script would shadow it.
- Feature branches target `develop`; do not push directly to `main`.
- Reusing v1 logic means *migrating* it: ten-step process in
  `LEGACY_COMPONENTS.md`, recorded there. Copy or rewrite behind a clean v2
  interface — never import.

## Legacy context

`legacy-code/LEGACY_README.md` is the honest account of what v1 was and what
went wrong — read it before redesigning anything it already tried. Key facts:

- The live v1 loop was `AgentChatLoop`; `ToolCallingAgentLoop` and
  `runtime/run_control.py` were test-only dead code.
- v1 had **no mid-run cancellation path at all**. This is the single defect
  that most motivates v2's run controller.
- Deeper v1 material: `legacy-code/docs/agent-context/` (AGENTS.md,
  CURRENT_STATE.md, AGENT_LOOP_AND_TOOLING_REPORT.md). Note AGENTS.md documents
  a Windows path (`F:\...`); this checkout is Linux at `/home/shamsu/Shamsu`.
- **v1 baseline: 2349 passed / 7 failed / 6 skipped** of 2362, fully triaged:
  5 environmental (3 need a local model, 1 needs `python3-venv`, 1 needs
  playwright), 1 stale test, and **1 genuine bug** (`_FILE_TOKEN_RE` cannot
  match a POSIX absolute path — see `LEGACY_COMPONENTS.md`).
- Two traps when running the legacy suite: this box has **no `python`, only
  `python3`** (symlink one onto PATH or you get 10 spurious failures), and
  pre-archival `__pycache__` under `legacy-code/` makes every traceback show
  `???` (delete it). Both are documented in `legacy-code/LEGACY_README.md`.

## Installing Python packages here

The system Python is PEP-668 externally managed and `python3-venv` is not
installed, so `pip install` and `python -m venv` both fail. Use:

```
pip install --target <dir> <packages>
PYTHONPATH=<dir> python3 -m <tool>
```

This is how mypy and the legacy test dependencies were installed without
touching the system environment. Do not reach for `--break-system-packages`.
