# SHAMSU — 10-Day Development Plan
## Maximum-Parallelism Build: PRD → Django Web App, with the Full Engineering Harness
> **3 developers · 10 days · Tested, working Day-1 scaffold included**
> Stack: Django + DRF + DaisyUI + HTMX · Router (Phi-3 Mini) + specialists (Qwen2.5-Coder, DeepSeek-Coder, Mistral) · BM25/FTS5 retrieval · 8GB RAM ceiling

---

## What's Different About This Plan

Every previous plan had a hidden serialization problem: Dev B needed Dev A's search engine, Dev C needed Dev A's sandbox, and so on. That's fine over 12 days but is fatal over 10 — there's no slack left to absorb a blocked day.

This plan removes that problem at the source. The Day 1 scaffold (attached as a zip, **already written and passing 24 tests**) ships the entire shared contract — `types.py`, `interfaces.py`, a working `SearchAgentStub`, a working SQLite+FTS5 schema, a working context builder, a working safety sandbox, and a working LLM manager with router-JSON parsing. **All three devs start building real features on Day 1 afternoon, against real working code, never against an empty file.**

The ownership split is also stricter than before: each dev owns a *vertical slice* (one full path from input to output), not a *layer*. This means a dev can demo a working, if narrow, feature every single day without needing anyone else's code to merge first.

---

## The Scaffold (already built — see attached zip)

```
shamsu/
  types.py              ✅ written, tested — the shared contract, frozen
  interfaces.py          ✅ written, tested — ISearchAgent, IContextBuilder, etc.
  storage/schema.py      ✅ written, tested — SQLite + FTS5, with sync triggers
  retriever/search.py    ✅ written, tested — real FTS5 SearchAgent + SearchAgentStub
  context/budget.py      ✅ written, tested — token budget constants
  context/builder.py     ✅ written, tested — truncate-middle, dedup, packing
  safety/sandbox.py      ✅ written, tested — path traversal blocking
  safety/commands.py     ✅ written, tested — command risk + secret redaction
  llm/manager.py         ✅ written, tested — router JSON parsing + json_repair fallback
  cli/repl.py            ✅ written — minimal REPL stub
  core/                  empty — Dev B builds Coordinator here, Day 2+
  indexer/               empty — Dev A builds file walker + AST parser here, Day 1-2
  agents/                empty — Dev B builds workflows here, Day 3+
  prd/                   empty — Dev C builds PRD parser here, Day 1-2
  patch/                 empty — Dev A builds patch engine here, Day 4+
  tools/                 empty — Dev C builds command runner + git here, Day 3+
  skills/                empty — not needed until Day 9+
  templates/django/      empty — Dev C fills with fixed templates, Day 2-3
  templates/frontend/    empty — Dev B fills with DaisyUI/HTMX snippets, Day 5+
tests/test_day1_scaffold.py   ✅ 24 passing tests — run this before any PR
.github/workflows/ci.yml      ✅ ruff + pytest on every push
.github/PULL_REQUEST_TEMPLATE.md  ✅ ready
pyproject.toml          ✅ all dependencies pinned, install with `pip install -e ".[dev]"`
```

**Before Day 1 starts, every dev should run:**
```bash
unzip SHAMSU_day1_scaffold.zip -d shamsu-project && cd shamsu-project
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/ -v       # must show 24 passed
ruff check shamsu/      # must show "All checks passed!"
```
If either command fails on your machine before you've changed a single line, stop and fix your environment first — don't build on top of a broken baseline.

---

## Why the Scaffold Already Answers Several Open Questions

A few things that would normally eat a half-day of Day 1 are already decided and tested in the scaffold, so the team doesn't re-litigate them:

- **FTS5 multi-word queries use OR, not implicit AND.** This was caught and fixed during scaffold construction — a bare `snippets_fts MATCH 'login authentication'` only matches rows containing *both* words, which silently kills recall on any natural-language query from the router or PRD. `SearchAgent._build_fts_query()` joins terms with `OR`. This is exactly the kind of bug that's invisible until Day 6 when results start looking suspiciously sparse — it's fixed now.
- **The context builder already implements the "Lost in the Middle" recency trick** (see `llm/manager.py::_format_pack` — the task is restated as the literal last line of every specialist prompt, snippets sit in the middle). This is locked in by a test (`test_task_statement_placed_last_in_prompt`) so nobody can accidentally regress it while refactoring.
- **The router already has a tested 3-tier fallback:** clean JSON → `json_repair` repair attempt → safe `"qa"` default. SHAMSU will never crash on a malformed routing response.
- **The sandbox already blocks the three classic escapes** (relative traversal, absolute path, nested traversal) — verified, not just asserted.

---

## Ownership Split (Vertical Slices, Not Layers)

| Dev | Owns the full path for | Folders |
|---|---|---|
| **Dev A** | Indexing → retrieval → patch application. "How does SHAMSU understand and safely modify code." | `indexer/`, `retriever/`, `patch/`, `storage/` |
| **Dev B** | Routing → context → LLM → workflows. "How does SHAMSU decide what to do and generate output." | `core/`, `llm/`, `context/`, `agents/` |
| **Dev C** | PRD → Django generation → safety → CLI. "How does SHAMSU turn a PRD into a running project, safely." | `prd/`, `tools/`, `safety/`, `cli/`, `templates/` |

This is deliberately different from a "frontend/backend/infra" split — it's a split by *what breaks if it's missing*, so each dev can build and demo a vertical without waiting on the other two past Day 1.

---

## Daily Rhythm

```
Morning standup (15 min): done yesterday / doing today / blocked by
Midday sync (optional, 10 min): anyone about to touch types.py or interfaces.py speaks now
End of day: push WIP branch even if incomplete, open draft PR, run pytest + ruff locally first
```

**Branch naming:** `feature/dev-a/...`, `feature/dev-b/...`, `feature/dev-c/...`
**Merge rule:** 1 approval from another dev, CI green, squash merge.
**The one hard rule:** nobody edits `types.py` or `interfaces.py` alone. Propose the change in chat, get a thumbs-up from the other two, then edit. Every other file is yours to move fast in.

---

## Day-by-Day Plan

### Day 1 — Unpack scaffold, claim territory, first real feature each

**All 3 devs (first 30 minutes, together)**
- Unzip scaffold, run `pytest` + `ruff`, confirm 24/24 green on every machine
- Read `types.py` and `interfaces.py` out loud as a team — this is the only "everyone in one room" moment of the whole 10 days
- Agree: nobody touches these two files solo for the rest of the project without a 2-minute heads-up in chat

**Dev A** — `indexer/walker.py`
- Recursive file walker: `pathlib.Path.rglob`, `DEFAULT_IGNORE` set (node_modules, .git, venv, __pycache__, dist, build, *.pyc, *.lock, image/font extensions), language detection by extension
- sha256 hash computed during the read pass (64KB chunks — don't `read_text()` whole files, see harness §8 RAM notes)
- Writes results into the `files` table from `storage/schema.py` (already exists — just call `init_db()` and insert)
- **End of day demo:** point the walker at this very scaffold repo, print every discovered file + language + hash

**Dev B** — `core/coordinator.py` (thin) + `agents/qa_workflow.py`
- `Coordinator.handle(user_input: str)`: calls `LLMManager.route()` (already built), prints the routing decision — that's the whole Day 1 scope, deliberately small
- `agents/qa_workflow.py`: wires `SearchAgentStub` (already built) → `ContextBuilder.pack()` (already built) → prints the assembled prompt (don't call Ollama yet — that needs Ollama running, which not everyone will have set up by hour 3)
- **End of day demo:** `shamsu> how does login work?` → prints routing JSON → prints assembled context pack with the task at the end

**Dev C** — `prd/parser.py` (Markdown only today) + `safety/approval.py`
- `prd/parser.py`: `mistletoe` AST walk, extract H1-H3 headings into a `sections: dict[str, list[str]]`, output `ParsedPRD` (type already exists in `types.py`)
- `safety/approval.py`: one function, `ask_approval(request: ApprovalRequest) -> bool`, prints a Rich panel with action/risk/preview, reads y/n from input
- **End of day demo:** feed a sample PRD markdown file in, print the extracted sections dict

**End of Day 1 — everyone has shipped something that runs.** Push WIP branches even if rough.

---

### Day 2 — Deepen each slice, first cross-dev handoff prepared (not yet wired)

**Dev A** — `indexer/parser.py` (AST) + `storage` writes
- Python `ast` stdlib walk: extract functions, classes, imports, docstrings, signatures → write into `symbols` table
- Wire walker (Day 1) → parser (today) → `symbols` table, end to end, on a real multi-file project (use this very scaffold as the test subject)
- **Demo:** `symbol_lookup('Sandbox')` returns the real class from `safety/sandbox.py` with correct line numbers

**Dev B** — `llm/manager.py` live Ollama test + `agents/qa_workflow.py` real call
- Get Ollama running locally, pull `phi3:mini-4k-instruct` (small, fast to verify plumbing — swap to Qwen2.5-Coder once this works, per the harness)
- Replace the Day 1 "print the prompt" stub with a real `await llm.run_specialist("qa", pack)` call, stream the response
- **Demo:** ask a real question against `SearchAgentStub`'s fake data, get a real model-generated answer back, end to end

**Dev C** — `templates/django/` fixed templates + PRD extractor v1
- Write the fixed-template constants from the architecture doc: `SETTINGS_TEMPLATE`, `MANAGE_TEMPLATE`, `WSGI_TEMPLATE`, `BASE_HTML_TEMPLATE` (DaisyUI + HTMX CDN tags + navbar), `LOGIN_HTML_TEMPLATE`, `REGISTER_HTML_TEMPLATE`, `REQUIREMENTS_TEMPLATE` — these are Python string constants with `{{ var }}` substitution points, zero LLM involved
- `prd/extractor.py`: regex-based entity extraction from the `## Entities` section (`- **Name**: field (type), field (type)` pattern) → `list[EntitySpec]`
- **Demo:** run extractor on a sample PRD, print the `EntitySpec` list; render `base.html` with a fake project name and show it's valid HTML

**Midday sync today:** Dev A shares the exact shape of `SearchResult` and symbol data Dev B will eventually consume (no code change needed — just confirms the contract works as designed before Day 3's real wiring).

---

### Day 3 — First real cross-dev wiring

**Dev A** — `retriever/ranker.py` + swap stub → real `SearchAgent` for Dev B
- Multi-signal ranking on top of FTS5 results: combine BM25/FTS5 score (0.5) + exact symbol match (0.3) + file path match (0.2) — simpler weighting than earlier drafts since FTS5 already gives a reasonable base score
- Open the PR that lets Dev B replace `SearchAgentStub()` with the real `SearchAgent(db_path)` — **this is the Day 3 handoff**, flagged in standup

**Dev B** — swap in real `SearchAgent`, build `agents/code_edit_workflow.py`
- Pull Dev A's PR, change one import line in `qa_workflow.py` (this is the entire point of having built against `ISearchAgent` from Day 1 — confirm it really is a one-line change)
- `code_edit_workflow.py`: search → pack → call `coder` specialist with "output ONLY a unified diff" system prompt → print the raw diff (validation comes Day 4 from Dev A's patch engine)
- **Demo:** real FTS5 search results flowing into a real LLM call producing a real (unvalidated) diff

**Dev C** — `prd/extractor.py` v2 (endpoints + pages) + DaisyUI system prompt constant
- Extend extractor: `## API Endpoints` → `list[EndpointSpec]`, `## Pages` → `list[PageSpec]`
- Write `FRONTEND_SYSTEM_PROMPT` as a constant: full DaisyUI class reference (btn, card, table, badge, modal, stats names) + HTMX attribute reference + "always use `{{ form|crispy }}`, never write raw `<input>`" — this gets reused by Dev B's frontend generator on Day 5+
- **Demo:** full `ProjectSpec` assembled from a real PRD — entities, endpoints, and pages all populated

**End of Day 3 — the retrieval pipeline is real and shared. This was the single riskiest dependency in every earlier version of this plan, and it's now done on schedule.**

---

### Day 4 — Patch engine + bug fix + Django model generation begins

**Dev A** — `patch/engine.py`
- `validate_diff()`: check `@@` headers, hunk format, `+++/---` lines present
- `.bak` backup before any write; `apply()` via `subprocess` or Python `patch` lib; auto-restore from `.bak` on failure
- Wire into Dev B's `code_edit_workflow.py` from yesterday: diff → validate → preview (Rich syntax highlight) → `ask_approval()` (Dev C's function from Day 1) → apply
- **Demo:** a real, full code-edit loop — search, generate, validate, preview, approve, apply, with rollback on a deliberately broken diff

**Dev B** — `agents/bugfix_workflow.py` + start `agents/django_models_generator.py`
- Bug fix: parse `file:line` from a pasted traceback (regex), targeted search at that location, error message in context pack, `bugfixer` specialist (DeepSeek-Coder)
- Start the first Django generator: `models.py` generation — entity list in, Qwen2.5-Coder out, `ast.parse()` validation gate (per the harness — this is non-negotiable, never skip it)
- **Demo:** intentionally broken Python file → bug fix workflow finds and proposes a correct patch

**Dev C** — `tools/executor.py` (command runner) + `safety/commands.py` wiring
- `CommandRunner.run()`: classify via `classify_command()` (already built, Day 1), block on `BLOCKED`, gate `MEDIUM` through `ask_approval()`, run `SAFE` directly, capture stdout/stderr
- Wire `Sandbox.validate()` into every file path across the whole codebase so far (grep audit — this is tedious but important, do it now before more code exists)
- **Demo:** attempt `rm -rf /` through the runner (blocked), attempt `pip install` (approval gate), attempt `pytest` (runs immediately)

---

### Day 5 — Django generators: serializers, views, forms + frontend system prompt live

**Dev A** — `indexer/hasher.py` (incremental re-index) + integration test
- Compare sha256 against stored hash, only re-parse changed files
- Write `tests/test_integration_search.py`: walk → parse → chunk → search → verify results, on a real multi-file fixture project
- **Demo:** edit one file in the fixture project, re-index, confirm only that file was re-touched (add a print/log line to prove it, remove before merge)

**Dev B** — `agents/django_serializer_generator.py` + `agents/django_view_generator.py`
- Serializer generator: feeds the *already-generated* `models.py` content into context so field names structurally can't mismatch (per the architecture doc's core trick)
- View generator: `ModelViewSet` for API + `@login_required` template views, using Dev A's patch engine validation pattern (`ast.parse()` gate) here too
- **Demo:** PRD with 2 entities → models.py → serializers.py → views.py, all three files generated in sequence, all valid Python, names matching across files

**Dev C** — `agents/django_form_generator.py` + `agents/django_settings_generator.py` (template-only, no LLM)
- Form generator: `ModelForm` per entity, Phi-3 Mini, trivial output
- Settings generator: pure template substitution (`SETTINGS_TEMPLATE` from Day 2 + project name + generated `SECRET_KEY` + installed apps list) — confirms the "9 files need zero LLM calls" claim from the architecture doc actually holds
- **Demo:** full `settings.py` generated with zero Ollama calls, `python -m py_compile` confirms it's valid

---

### Day 6 — URLs + admin + frontend pages begin

**Dev A** — `patch/preview.py` (Rich diff display polish) + `core` error handling
- Colored diff preview (green add / red remove) for every patch shown to the user from here on
- Global try/except wrapper at the coordinator level: catch, log, show friendly message + retry/skip/abort choice — this single change makes every workflow built so far noticeably more robust
- **Demo:** force an exception in a workflow, show the friendly recovery instead of a raw traceback

**Dev B** — `agents/django_url_generator.py` + `agents/django_admin_generator.py`
- URL generator: DRF router registration + `path()` entries, validated against the views.py generated yesterday (every referenced view name must exist — simple `ast`-based check, no LLM needed for this validation)
- Admin generator: `admin.site.register()` per model — near-template, Phi-3 Mini
- **Demo:** `python manage.py check` on a freshly generated project — 0 errors

**Dev C** — `agents/django_dashboard_generator.py` (first frontend page)
- Uses the `FRONTEND_SYSTEM_PROMPT` from Day 3, DaisyUI stats row + table, HTMX `hx-get` for loading
- **Demo:** open the generated `dashboard.html` in a browser (via `python manage.py runserver` if Dev B's backend is far enough along, otherwise just visually inspect the DaisyUI markup is well-formed)

**End of Day 6 — full backend pipeline (models → serializers → views → urls → admin → settings) runs end to end.** This is the point where the project stops being "pieces" and starts being "a system."

---

### Day 7 — Full pipeline integration test (first real end-to-end run)

**All 3 devs — coordinated integration day, less solo building, more wiring + fixing**

- **Morning:** assemble the full pipeline — `prd/parser.py` → `prd/extractor.py` → all Django generators in dependency order → `patch`/file write → `manage.py check`
- Write a real Todo App PRD (Task, Category entities; CRUD endpoints; dashboard + list pages)
- Run the whole thing. **It will break somewhere — that's the point of doing this on Day 7, not Day 10.**

**Dev A** focuses on: indexing/search bugs surfaced by the integration run, patch application failures
**Dev B** focuses on: LLM output quality issues — malformed diffs, JSON parse failures, field-name mismatches between generated files
**Dev C** focuses on: template substitution bugs, PRD extraction misses, frontend rendering issues

- **End of day target:** `python manage.py runserver` starts without errors, `/admin/` loads, at least the dashboard page renders. File every remaining issue, assign an owner, fix tomorrow.

---

### Day 8 — Fix Day 7 failures + HTMX partials + migrations runner

**Dev A** — fix indexing/patch bugs from Day 7 + `tools/git.py`
- Whatever broke yesterday in search/patch gets fixed first
- `git status`/`git diff` read-only wrapper, warn before editing uncommitted files

**Dev B** — fix LLM/generator bugs from Day 7 + `agents/django_resource_list_generator.py`
- Whatever broke yesterday in generated code gets fixed first
- Resource list page generator: DaisyUI table + HTMX add-modal + HTMX delete, using model field names + URL names from already-generated files

**Dev C** — fix template/extraction bugs from Day 7 + `tools/migrations_runner.py`
- Whatever broke yesterday in templates/extraction gets fixed first
- `python manage.py makemigrations && migrate` wrapper, parse output for success/error, feed errors to the bug-fix workflow on failure

**End of Day 8 — re-run the Day 7 Todo App PRD. It should now complete cleanly.**

---

### Day 9 — Error feedback loop + test generation + RAM check

**Dev A** — RAM profiling
- `tracemalloc` through the full pipeline on the Todo App PRD, peak RAM recorded
- Target: under 7GB. If over, the first suspects are: BM25 corpus built eagerly instead of lazily, full file contents kept in Python strings post-indexing — check both per the harness

**Dev B** — `agents/error_feedback_loop.py` + `agents/django_test_generator.py`
- Run `manage.py test` → parse Django test output → extract failing test + file:line → targeted bug-fix call → patch → re-run, max 3 iterations
- Test generator: `TestCase` + `APIClient`, per ViewSet, Qwen2.5-Coder
- **Demo:** intentionally break a serializer field name, run the loop, watch it self-correct within 3 iterations

**Dev C** — safety audit + `shamsu status` / `shamsu log` CLI commands
- Path traversal, blocked commands, secret redaction — re-verify all of Day 1's tests still pass against the *generated Django project*, not just the scaffold itself
- `shamsu status`: files indexed, symbols found, current task, model in use
- `shamsu log`: tail the structured jsonl log

---

### Day 10 — Ship

**Dev A**
- Final merge across all branches, resolve conflicts
- Full clean-env test: fresh venv, `pip install -e ".[dev]"`, `pytest`, confirm green
- Tag `v0.3.0`

**Dev B**
- Record the demo: Todo App PRD → full generation → `runserver` → browser walkthrough (dashboard, add a task via HTMX, delete it)
- Fix any rough edges visible in the recording

**Dev C**
- `README.md`: install steps, PRD format guide, how to run a generated project, troubleshooting
- `CHANGELOG.md` + short retro doc: what worked, what was harder than expected, what's explicitly out of scope for this milestone (PostgreSQL, Docker, React option — same backlog as before)

---

## Success Criteria for Day 10

- [ ] `pytest tests/` passes (the original 24 scaffold tests + everything added since)
- [ ] `ruff check shamsu/` is clean
- [ ] Todo App PRD → generated Django project → `python manage.py check` → 0 errors
- [ ] `python manage.py runserver` starts, `/admin/` loads, dashboard page renders with DaisyUI styling
- [ ] At least one full code-edit loop works end to end on the generated project (search → diff → validate → approve → apply)
- [ ] Error feedback loop demonstrably self-corrects at least one class of generated-test failure
- [ ] Peak RAM during full pipeline run is measured and under 7GB
- [ ] Path traversal, blocked commands, and secret redaction all still pass against the generated project

---

## Why This Schedule Survives Losing a Day

If any single dev loses a day (sick, blocked, environment issues), the vertical-slice ownership means the other two can keep moving — Dev A losing a day delays patch validation, not Dev B's routing work or Dev C's PRD parsing. The Day 7 integration checkpoint exists specifically to surface cross-slice problems with three days of runway left to fix them, instead of finding out on Day 10 that the pieces don't actually fit together.

---

*SHAMSU — Killer of API Bills from the Big Giants*
*10-day plan · scaffold pre-built and tested · v0.3.0 target*
