# Agent Context: SHAMSU

Quick-start context for any agent working in this repository. Read this first,
then `CURRENT_STATE.md` for what actually works today.

Last verified: **2026-07-20** (tests, layout, model tiers, and route table all
checked against the code on this date).

---

## 1. Where Things Are

- **Repo root (the real one):** `F:\Work\PROJECTS\shamsu\Shamsu`
  The parent folder `F:\Work\PROJECTS\shamsu` is *not* the package. It holds the
  repo plus scratch dogfood workspaces (`test-shamsu/`, `shamsu-launcher-test/`).
  Run `pytest`, `ruff`, and `python -m shamsu...` from the nested `Shamsu\` dir.
- **Remote:** `https://github.com/SowadHossain/Shamsu.git`
- **Branching:** feature branches target `develop`. Do not push directly to `main`.
- **Version:** `0.4.0b1` (see `pyproject.toml`)
- **Python:** 3.11+. Always use the repo venv: `.\.venv\Scripts\python.exe`.

### Docs that exist in `agent context/`

| File | What it is |
|---|---|
| `AGENTS.md` | This file. Orientation + rules for agents. |
| `CURRENT_STATE.md` | **Ground truth**: what is built, what works, what is broken. |
| `REQUIREMENTS.md` | The original product spec, now with a conformance table. Aspirational in places — check the status markers before trusting a line. |
| `PROGRESS.md` | Append-only implementation ledger. Long. Historical detail lives here. |
| `SHAMSU_RELIABILITY_PRODUCT_PLAN.md` | The reliability work-package plan (WP1–WP12). |
| `prompts/` | Currently empty. |

Docs referenced by older notes that **no longer exist**: `SHAMSU_week2_milestone_v2.md`,
`SHAMSU_10day_dev_plan.md`, `DEV-TASK-DIVI.MD`, `MILESTONE-2-FINISH-PLAN.md`,
`claude-hand-off-plan.md`, `SHAMSU_agent_gap_analysis.md`, `v2.3-techstack-recomendation.md`.
If a doc cites one of those, treat that citation as history, not as a live pointer.

Root-level docs: `README.md` (for humans), `CHANGELOG.md`, `BENCHMARK.md` /
`BENCHMARK-light.md` (model-quality evals), `RELEASE_VALIDATION.md`
(deterministic runtime gates), `DEMO_SCRIPT.md`.

---

## 2. Product Identity

SHAMSU is a local-first autonomous coding agent for low-resource machines.

Core promise:

- Run on sub-8GB devices.
- No cloud API bills. Inference is local-only via Ollama on `localhost:11434`.
- Source code stays local and private.
- Deterministic tools do the scanning, indexing, parsing, searching, and validation.
- Small local LLMs are used only for reasoning, planning, summarization, and generation.

The central engineering principle:

> Do not use the LLM as a brute-force scanner. Use tools to find the right
> context, then use the LLM to reason and generate.

---

## 3. Package Map

`shamsu/` — 201 modules. The ones that matter most:

| Package | Role |
|---|---|
| `cli/` | Entry point. `repl.py` (~8.9k lines) is the interactive + headless driver; `command_router.py`, `noninteractive.py`, `arguments.py`, `request_lifecycle.py`, `approval_ui.py`, `session_commands.py` split out of it. |
| `agents/` | Workflows: `chat_loop.py` (the ReAct loop), `orchestrator.py` (pre-model deterministic answers), `qa_workflow.py`, `code_edit_workflow.py`, `bugfix_workflow.py`, `audit_workflow.py`, `doc_workflow.py`, `test_generation_workflow.py`, `plan_mode.py`, `planner.py`, `full_pipeline.py`, `scaffold_*`, `*_fallback.py`. |
| `routing/` | `operations.py` — deterministic parsing of composite multi-action prompts into an ordered `OperationPlan`. |
| `llm/` | `manager.py` (specialist dispatch, streaming, lazy model pull), `output.py` (`parse_model_turn` — salvages tool calls from messy small-model output), `council.py` (draft→critique→reconcile, gated). |
| `runtime/` | `models.py` (tier cookbook), `ollama.py`, `doctor.py`, `session_registry.py`. |
| `action_ledger/` | Canonical per-run artifacts: events, decisions, tool-calls, model-calls, contexts, mutations, final output, manifest. |
| `indexer/` | `walker.py` (incremental SQLite/FTS5 index), `parser.py` (tree-sitter/AST symbols). |
| `retriever/`, `context/` | Search + ranking, context packing and budgeting. |
| `abstract/`, `memory/` | Code-memory / abstract index; optional Graphiti+FalkorDB adapter with SQLite store. |
| `safety/` | Sandbox, command classification, approvals, permission memory, autonomy toggle, clarification. |
| `verify/` | `gate.py` (verification gate), `checks.py`, `dod.py`, `prd_checklist.py`. |
| `tools/` | Model-facing tools: `agent_tools.py`, `workspace.py`, `executor.py`, `git.py`, `web.py`, `browser.py`, `dev_server.py`, `django.py`. |
| `prd/`, `templates/`, `registry/` | PRD parse/extract/plan, Django + frontend generators, category registry. |
| `tasks/`, `plans/`, `taskmaster/` | `MilestoneTask` state, plan-mode plans, task decomposition. |
| `diagnostics/`, `repair/`, `audit/`, `session/`, `ui/` | Error parsing/adapters, repair loops, audit trail, session store, Rich rendering. |

`tests/` — 125 files. `evals/` — task-success harness (`python -m evals`).
`scripts/` — install/uninstall/doctor/run for PowerShell and Bash, plus
`benchmark_mvp.py` and `validate_release.py`.

---

## 4. Models

Three tiers, one role contract (`shamsu/runtime/models.py`). Thinking roles
(router/qa/planner/classifier/review/docs/summarizer/chat) get one anchor;
coding roles (coder/frontend/backend/tests/bugfix) get the other.

| Tier | Thinking anchor | Coding anchor |
|---|---|---|
| `light` (8GB, CPU-only) | `qwen2.5:3b-instruct` | `qwen2.5-coder:3b-instruct` |
| `default` (8GB cookbook) | `deepseek-r1:7b` | `qwen2.5-coder:7b-instruct` |
| `heavy` (16GB+) | `mistral-nemo:12b` | `qwen2.5-coder:14b` |

- `qwen3:8b` and `gemma3:4b` are **former** anchors — still allowed and known,
  never auto-pulled. Older docs naming either as "the default" are stale.
- `ModelSpec` carries capability flags: `supports_native_tools` and
  `is_reasoning`. Do not assume a model does native tool-calling — the default
  thinking anchor (`deepseek-r1:7b`) does not, and gets a prompt-level tool
  protocol with `llm/output.py` as the primary parser.
- Active tier is process-global (`initialize_model_tier` / `set_model_tier`),
  persisted at `.shamsu/model_tier.json`, overridable with `SHAMSU_MODEL_TIER`.
- `SHAMSU_SINGLE_MODEL_MODE=1` routes every role to the thinking anchor.

---

## 5. How Requests Are Routed

`_ROUTE_RULES` in `shamsu/cli/repl.py` (~line 3554) is a single ordered table.
**Order is the logic: top-down, first match wins.** Moving a rule changes
behavior; `tests/test_routing_matrix.py` pins it.

Current order: `prd_summary` → `git` → `workspace.location` → `workspace.files`
→ `prd.build` → `file.read` → `file.write` → `direct_code` → `workspace.prds`
→ `continue_game` → `run_game` → `dev_server.recovery` → `dev_server` →
`prd.context_question` → `browser` → `web` → `agent-chat` → `django` →
`plan_prd`. No match falls through to `qa` (`ROUTE_FALLTHROUGH`), the tool-less
QA brain.

Before routing, `AgentOrchestrator` answers deterministic workspace questions
(location, file listing, PRD discovery) without a model call at all.

Composite prompts ("read X and then fix Y") are split by
`routing/operations.py` into an ordered `OperationPlan` before dispatch.

---

## 6. Running SHAMSU

Interactive:

```powershell
.\scripts\run-shamsu.ps1
```

Headless (this is what the dogfood harness uses):

```powershell
python -m shamsu.cli.repl run `
  --workspace F:\path\to\workspace `
  --prompt "<prompt>" `
  --output json `
  --approval allow   # or: deny
```

Other `run` flags: `--session`, `--new-session`, `--dry-run`, `--timeout`.

Every prompt writes a run bundle under `<workspace>\.shamsu\runs\<run-id>\`:
`manifest.json`, `events.jsonl`, `decisions.jsonl`, `tool-calls.jsonl`,
`model-calls.jsonl`, `mutations/mutations.jsonl`, `context-preview.json`,
`contexts/`, `final-output.md`, `summary.json`. Inspect with `/runs` and
`/run show|timeline|decisions|tools|commands|context|diff|validate` **inside the
REPL** — see the known bug in §8 about slash commands in headless mode.

---

## 7. Safety Rules To Keep Front And Center

- The active project folder is the workspace boundary. Block path traversal and
  sensitive system paths.
- Ask before writing, editing, deleting, or moving files; installing
  dependencies; running commands; accessing the internet; calling external tools.
- Prefer patch-based edits with preview; fall back to full-file rewrite only on
  a *malformed* diff, never on a user denial.
- Block dangerous commands by default. `run_command`, `file_delete`,
  `web_search`, and `mcp_tool` are never auto-approvable, regardless of
  remembered permissions.
- Redact secrets in logs and summaries. Never send private source to the web.
- Answer local workspace questions with deterministic tools before an LLM call.
- Resolve `@file` / `@folder` mentions through the sandbox only.
- Preserve recent conversation turns so follow-ups inherit context.

---

## 8. Known-Broken Behavior (read before you "fix" something)

From the fresh dogfood pass on 2026-07-20
(`../../test-shamsu/SHAMSU_FRESH_DOGFOOD_2026-07-20.md`). The artifact/logging
layer is healthy; the **behavior layer is not**. Open bugs, highest value first:

1. Stale PRD/TaskFlow context leaks into unrelated prompts.
2. Simple file-creation prompts misroute to `prd.build`.
3. "Do not change files" is not honored when broad approvals are allowed.
4. Running a command can turn its stdout into a file write.
5. A read-only web answer is mislabeled `failed` / route `file.write`.
6. Dry-run produces no planned mutation for new-file creation.
7. Headless slash commands (`/run show`) are not handled before model dispatch.
8. Artifact validation checks completeness, not task semantics — a wrong result
   still validates `ok`.

Do not treat "run validation ok" as "the agent did the right thing."

---

## 9. Working Rules For Agents

- Keep `shamsu/types.py` and `shamsu/interfaces.py` stable unless the team
  explicitly agrees to change the contract.
- Update `CURRENT_STATE.md` when behavior changes; append to `PROGRESS.md` for
  the historical record. Do not rewrite `PROGRESS.md` history.
- Keep generated-app templates deterministic wherever possible.
- Use local files and indexes as the handoff mechanism between models.
- Keep memory usage low; avoid always-on heavy services.
- Keep implementation small and testable. SHAMSU exists to help small models —
  the codebase itself should be boring in the best way.
- Some older markdown contains mojibake. Preserve meaning when editing, but
  avoid broad formatting churn unless asked.

---

## 10. Verification Commands

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
.\.venv\Scripts\python.exe -m ruff check shamsu tests scripts
.\.venv\Scripts\python.exe -m evals            # model-quality task success
.\.venv\Scripts\python.exe scripts\validate_release.py
.\scripts\doctor.ps1
```

Baseline as of 2026-07-20: **1418 passed, 1 skipped**; evals 11/12 on the
default tier (2 cases flagged flaky); release gate PASS.
