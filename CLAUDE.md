# SHAMSU — agent orientation

Local-first autonomous coding agent (Python ≥3.11, package `shamsu`, v0.4.0b1).
Inspects, indexes, searches, explains, edits, fixes, tests, documents, and
generates projects **without cloud AI APIs for inference**. Inference is local.

**Prime directive:** use deterministic tools to find the right context, then use a
small local model to reason over that context. Never dump a codebase into a
prompt. Retrieve first, then build a compact context pack.

## Use the knowledge graph before grep/read

This repo is indexed into a `codebase-memory-mcp` knowledge graph
(161k nodes / 239k edges). For structural questions — "who calls X", "what's the
architecture", "what breaks if I change Y" — query the graph. It answers in
~1–2 KB where the equivalent file reads cost tens of KB.

MCP tools (8, via `.mcp.json`): `search_graph`, `query_graph`, `trace_path`,
`get_code_snippet`, `get_graph_schema`, `get_architecture`, `search_code`,
`index_repository`.

**The graph project name is `home-shamsu-Shamsu`** — required by every tool
except `index_repository`; it is not the directory name.

Six more tools are **CLI-only** in 0.9.0 (`manage_adr`, `index_status`,
`list_projects`, `detect_changes`, `delete_project`, `ingest_traces`):

```
/root/.shamsu/tools/codebase-memory-mcp/codebase-memory-mcp cli <tool> \
    --project home-shamsu-Shamsu --flag value      # 2>/dev/null — logs to stderr
```

Raw positional JSON is deprecated; use flags, `--args-file <path>`, or stdin.

**After edits, refresh — or SHAMSU silently degrades:**

```
python3 -m shamsu.abstract.cli refresh     # then `status` → expect
                                           # degraded:false, retrieval_mode:"external"
```

Driving the raw binary directly does **not** update `.shamsu/abstract/last-index.json`,
and `AbstractService.ensure_ready()` gates off that file, not the graph.

## Layer map

- **entry** — `cli/`. REPL at `shamsu/cli/repl.py`. Only outbound calls.
- **core** (high fan-in): `action_ledger/` (per-prompt run record — decisions,
  tool/model calls, contexts, mutations, verification), `tools/`, `session/`,
  `agents/` (QA, code-edit, bug-fix, audit, test-gen, docs), `memory/`,
  `safety/` (path sandbox, command risk classifier, secret redaction — **not** an
  OS sandbox).
- **Memory** = Graphiti (external, `~/.shamsu/tools/graphiti/`) with a **SQLite
  floor** (`memory/sqlite_store.py`) that is always available; Graphiti is
  preferred when healthy, mirrored to asynchronously via a bounded queue.
- **Live loop** is `AgentChatLoop` (`agents/chat_loop.py`), the only one
  `repl.py` constructs. `ToolCallingAgentLoop` and `runtime/run_control.py` are
  **test-only / dead code** — see `agent context/AGENT_LOOP_AND_TOOLING_REPORT.md`.
- **leaf** — `abstract/`, `runtime/`, `telemetry/`, `templates/`, `diagnostics/`,
  `repair/`, `patch/`, `verify/`, `retriever/`, `llm/`, `prd/`, `indexer/`,
  `plans/`, `routing/`, `skills/`, `taskmaster/`, `tasks/`, `audit/`, `context/`,
  `core/`, `registry/`, `ui/`.

## Frozen contract

`shamsu/types.py` and `shamsu/interfaces.py` are the shared team contract — do not
change casually. `interfaces.py` splits ownership: Dev A `indexer/ retriever/
patch/ storage/`, Dev B `llm/ agents/ context/ core/`, Dev C `cli/ safety/ prd/
tools/`. Consumers of unbuilt deps import the interface and write a `Stub*` class
(example: `shamsu/retriever/search.py`) rather than blocking on a PR.

## Retrieval stack

**codebase-memory-mcp is the only search/symbol backend.** There is no
SHAMSU-owned index, parser, or code graph — every result traces to a real tool
call (`search_code`, `search_graph`, `get_code_snippet`). See the explicit
docstring at `shamsu/retriever/search.py:1-9`. The `search_index` tool gates on
`AbstractService.ensure_ready()` before querying (`agent_tools.py:3188-3194`).

**Last-resort semantic rescue** (`shamsu/retriever/semantic.py`): local Ollama
embeddings (`nomic-embed-text`, ~274 MB), file-level granularity, JSON vector
index under `.shamsu/` refreshed lazily per query. Runs only after the primary
search returns nothing, degrades to "no hits" on any failure, and remembers the
failure per-process. Disable with `SHAMSU_SEMANTIC_SEARCH=0`.

Stale claims to ignore — **README and `pyproject.toml` are out of date here**:
- README's `.shamsu/index.db` SQLite FTS5 index no longer exists in `shamsu/`;
  the only FTS5 reference is a comment describing the *external* tool's index.
- `rank_bm25` and `yake` are declared dependencies but imported nowhere.

## Invariants

1. Deterministic retrieval before inference; compact packs, never raw dumps.
2. Mutations are approval-gated and ledger-logged; patches validated,
   diff-previewed, applied, then re-indexed.
3. File input passes the sandbox; commands pass the risk classifier; output
   passes redaction.
4. **Honest failure over fabrication** — `ok=False` beats an invented fact. The
   code-memory adapter never fabricates a structural claim.
5. Local-first: no cloud inference, no remote code-memory URI.

## Conventions

- No `[project.scripts]` on purpose — a managed launcher lives at `~/.shamsu/bin`;
  a pip console-script would shadow it.
- Feature branches target `develop`; do not push directly to `main`.
- Deeper context: `agent context/AGENTS.md`, then `agent context/CURRENT_STATE.md`
  for ground truth. Note AGENTS.md documents a Windows path (`F:\...`); this
  checkout is Linux at `/home/shamsu/Shamsu`.
