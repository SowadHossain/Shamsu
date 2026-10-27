# SHAMSU harness audit — 25 October 2026

**Verdict: SHAMSU contains a substantial, functioning harness, but its default execution path is not yet reliable enough for unattended intensive coding.** The largest problems are permission enforcement, context correctness, and evidence consistency. Some features work in the older path and are advertised generally even though the default loop bypasses them.

Reviewed the working tree at commit **5db4de1**, branch **small-model-hardening**, including pre-existing uncommitted changes. Those changes were preserved. This audit added reports and reproduction scripts; it did not change production code, install packages, or download models. Mutation probes used temporary workspaces.

The review combined refreshed code-graph indexing, source and call-path inspection, 880 focused tests, attempted full-suite validation, scripted-model probes through the real headless runner, and Chromium checks at desktop/mobile sizes. This is a subsystem audit, not a claim that every line or workflow was exercised.

**Fresh live-model quality could not be measured:** Ollama's /api/tags and /api/ps both returned empty model lists. Scripted responses isolate harness behavior; they do not measure model intelligence.

Evidence: [machine-readable results](evidence.json), [focused tests](focused.log), [full-suite timeout](pytest.log), [lint findings](ruff.log). The probe scripts in this directory run from the repository root using the repository's Python environment.

## What works, and where the claims break

| Capability | Assessment |
|---|---|
| Local coding loop | Real. The default dispatcher reaches SimpleChatLoop, calls the model, executes tools, and carries results forward. |
| File mutations | Real backups, transactions, hashes and diffs. A later guard can restore bytes without correcting the transaction record. |
| Context management | Real token estimates, output reserves, elision, summaries and hydration. Accounting, grouping and freshness defects remain. |
| Memory | Real persistence and recall. Default notes and the /memory SQLite store are disconnected; notes can lose data. |
| Skills | Real discovery, overrides, matching and loading. Default-mode behavior differs from the older selector and ignores the off setting. |
| External MCP tools | Implemented in the manager/older agent, but not offered or dispatched by the default loop. |
| Verification | Real checks and test execution. Structural scans overclaim syntax validity, and evidence is not tied to requirements. |
| Django generation | Substantial specialist workflows/templates exist. Their presence does not prove ordinary default-loop requests use them. |
| Multiple interfaces | Shared turn events are a sound design. Web-client state and rendering still have correctness defects. |
| Intensive small-model coding | Plausible as supervised incremental work. Current evidence does not establish dependable long-running, multi-file autonomy. |

The default flow is **_handle_request → _run_simple_chat → SimpleChatLoop → fixed simple-tool catalog**. Some calls reach AgentToolRegistry; others use special handlers for memory, search, contracts and symbols. SHAMSU_LEGACY_ROUTING selects the older orchestrator/workflows. Improving the older branch does not automatically improve the default branch.

## Confirmed findings

P0 means fix before relying on the affected safety mode. P1 means high-impact correctness, scope or evidence failure. P2 means important reliability/usability work. Ratings concern the reproduced conditions, not every request.

### F01 — P0: deny and dry-run still allow file writes

**Reproduced through the real run_prompt dispatcher:** a scripted model requested write_file. With approval=deny, forbidden.py was created and status was success. With dry_run=True, the file was also created, status was dry_run, and the final response claimed “the agent planned no file changes.”

**Cause:** [make_approval_func](../../shamsu/agents/simple_chat.py), around line 2449, approves file actions before consulting the headless override. [_run_simple_chat](../../shamsu/cli/repl.py), line 5646, constructs the registry without attaching the dry-run recorder; the older path attaches it explicitly.

**Fix:** compute one effective request policy before choosing the loop. Explicit deny/dry-run must precede convenience auto-approval. Intercept mutations before side effects. Test disk state through the default dispatcher for ordinary, composite, symbol and MCP mutations.

Evidence: policy.deny and policy.dryrun in [evidence.json](evidence.json); [probe_policy.py](probe_policy.py).

### F02 — P1: explicit read-only intent is not enforced

**Reproduced:** the real headless path created a source file under “Review the project only. Do not create, edit, or delete any files.” It reported success.

**Cause:** [_configure_agent_request_safety](../../shamsu/cli/repl.py), line 18167, wires read-only/scoped permissions for the older agent. The default branch around line 6071 returns through the simple loop first. Its new registry starts writable.

**Fix:** pass a typed policy to every execution path. Model tool selection and catalog narrowing cannot substitute for enforcement of read-only and allowed-path constraints.

Evidence: policy.read_only; [probe_policy.py](probe_policy.py).

### F03 — P1: search reads outside the workspace

**Reproduced:** search_files with an absolute sibling-directory path returned source containing OUTSIDE_AUDIT_SENTINEL from outside the selected workspace.

**Cause:** [_hybrid_search](../../shamsu/agents/simple_chat.py), line 7583, replaces its root with a resolved requested directory without sandbox containment validation. This special handler runs before normal registry dispatch.

**Fix:** validate the root and each result against the sandbox after symlink resolution. Explicitly refuse out-of-scope paths.

Evidence: scope; [probe_scope.py](probe_scope.py).

### F04 — P1: local web Host validation is missing

**Reproduced:** /api/workspaces accepted Host: attacker.invalid:<port>, with no Origin and no token, returning HTTP 200.

**Cause:** [_origin_is_local](../../shamsu/webui/server.py), around line 231, accepts missing Origin and never validates Host. Same-origin browser GETs can omit Origin; Origin checking alone is insufficient for the documented DNS-rebinding defense.

**Fix:** validate Host on every request and Origin when present. The official MCP Python SDK describes the same defense for local HTTP services; the portal itself is not an MCP server. [Official guidance](https://py.sdk.modelcontextprotocol.io/run/deploy/).

The probe proves the missing defense/data-access condition, not a complete browser DNS attack or cross-origin command execution.

Evidence: core.portal_foreign_host; [probe_core.py](probe_core.py).

### F05 — P1: context selection can orphan tool results

**Reproduced:** under a small budget, history containing a user request, assistant call and result was reduced to the tool-result message alone.

**Cause:** [ChatState.select_for_budget](../../shamsu/agents/chat_state.py), lines 229–276, retains the newest message even when no complete user/call boundary fits.

**Fix:** select complete interaction groups. Keep assistant calls and corresponding results together; summarize an oversized group explicitly. Validate the assembled conversation. Ollama's tool-calling examples preserve call/result order. [Official documentation](https://docs.ollama.com/capabilities/tool-calling).

Evidence: core.budget_can_orphan_tool_result.

### F06 — P1: token caps are not enforced after truncation

**Reproduced:** _budgeted emitted **8,202 tokens** against a **2,048-token cap** at an 8,192-token context, using a long CJK string.

**Cause:** [_budgeted](../../shamsu/agents/simple_chat.py), around line 10326, detects excess with a tokenizer but cuts to cap×4 characters. That ratio fails for non-English text, dense symbols and escaped data. Its raw fallback can also produce truncated invalid JSON.

**Fix:** truncate by tokenizer offsets or binary-search until the final serialized result, including markers, fits. Return valid structured results and continuation/blob references.

Evidence: context.tool_cap_exceeded; [probe_context.py](probe_context.py).

### F07 — P2: prompt overhead omits project and plan text

**Reproduced:** adding a large standing plan left _fixed_overhead unchanged at 4,721 tokens. A project block of roughly 1,201 tokens was also omitted from that accounting.

**Cause:** [_messages](../../shamsu/agents/simple_chat.py), line 4578, inserts project/plan text; _fixed_overhead, line 5112, builds a different grounding block.

**Fix:** construct and measure one prompt bundle. Later reply-cap estimates can partly compensate by reducing output space, so this proves inconsistent allocation, not universal overflow.

Evidence: core.context_grounding_undercount.

### F08 — P1: rollback leaves the mutation journal incorrect

**Reproduced:** a patch removed helper(). The guard restored the original file and returned rolled_back=True, but the transaction still said applied and retained the rejected file's after-hash.

**Cause:** [_erased_a_definition](../../shamsu/agents/simple_chat.py), line 6033, writes the old bytes directly after the registry has finalized the transaction.

**Fix:** validate before commit or perform a recorded compensating rollback. One transaction owner must update disk, hashes, status, index freshness and evidence together.

Evidence: journal; [probe_journal.py](probe_journal.py).

### F09 — P1: injected symbol context becomes stale after edits

**Reproduced:** cache a skeleton for old_name(), edit the existing file to new_name(), then request the skeleton again. It still contains old_name and omits new_name.

**Cause:** [_skeletons](../../shamsu/agents/simple_chat.py), line 8402, keys its cache on file names and budget, not content. The initial code brief is also calculated once per turn despite later edits.

**Fix:** use content hashes/index generation, invalidate after edits/moves/deletes/rollbacks, and detect external changes before reusing code facts.

Evidence: context.skeleton_cache_stale.

### F10 — P1: overlapping memory writers lose notes

**Reproduced:** construct two MemoryStore instances; save one note through each. Reopening finds only the second note although both saves reported success. This deterministic interleaving models overlapping sessions/processes.

**Cause:** [MemoryStore._save](../../shamsu/agents/simple_memory.py), lines 171–207, overwrites the JSON index from an instance snapshot without locking or merging. Cleanup can delete markdown absent from that stale snapshot. Writes are not atomic.

**Fix:** use SQLite transactions, or lock the whole reload–modify–save operation and atomically replace the file. Recall should not rewrite every note.

Evidence: core.memory_lost_update.

### F11 — P2: a failed memory save can report success

**Reproduced:** put a directory at the index-file path. remember() reports success; reload yields zero notes.

**Cause:** _save swallows OSError and returns no outcome; remember unconditionally reports success.

**Fix:** return a durable-write result and distinguish save failure/session-only availability. Preserve a recoverable previous index and expose failures in diagnostics.

Evidence: core.memory_false_success.

### F12 — P1: /memory and the default loop use different stores

**Reproduced:** MemoryService.remember_local successfully saved a PostgreSQL preference, but default render_memory returned no memory for PostgreSQL.

**Cause:** [/memory](../../shamsu/cli/repl.py), line 1512, uses MemoryService/SQLite. The default loop reads [simple_memory](../../shamsu/agents/simple_memory.py)'s JSON notes. Their recall/forget operations address different records.

**Fix:** one authoritative memory API for CLI and model tools. Migrate notes with source/type metadata. Explicit preferences should not depend on accidental keyword overlap.

Evidence: context.sqlite_memory_not_in_notes.

### F13 — P2: editing markdown notes does not edit memory

**Reproduced:** change a markdown note from SQLite to PostgreSQL. Recall still returns SQLite and rewrites the markdown, destroying the edit.

**Cause:** only index.json is loaded. Markdown is an output mirror despite comments describing human editing/deletion. Recall updates usage and saves the collection.

**Fix:** label generated mirrors clearly and offer supported editing, or import changes with stable IDs/version checks.

Evidence: core.manual_memory_edits_ignored.

### F14 — P1: configured MCP tools are unreachable by default

**Reproduced:** the simple catalog contains zero mcp__ schemas; _execute rejects a namespaced MCP tool as nonexistent.

**Cause:** [active_tool_schemas](../../shamsu/agents/simple_chat.py), line 1883, uses a fixed catalog. _execute, line 5904, rejects names outside SIMPLE_TOOLS before registry MCP dispatch. Owning an MCP manager does not make its tools accessible.

**Fix:** derive descriptors and dispatch from one registry. Discover/select a small relevant external-tool subset while preserving schemas, namespace and permission checks. Test a fixture server through default run_prompt. Existing [MCP tests](../../tests/test_mcp_integration.py) instantiate the older agent; [README](../../README.md) around line 545 advertises general availability.

Evidence: core.default_mcp_unreachable.

### F15 — P2: automatic skills ignore SHAMSU_SKILLS=off

**Reproduced:** with SHAMSU_SKILLS=off, a Python-debugging request still injects the debugger skill.

**Cause:** the default matcher discovers/renders directly without applying skills_mode_from_env. The older selector honors that setting. Dependencies, conflicts and explanations also have separate implementations.

**Fix:** share selection policy, honor off/shadow/on consistently, and ensure /skills explain describes the selector actually used.

Evidence: context.skills_off_ignored.

### F16 — P1: an unrelated command can verify a requirement

**Reproduced:** create “Payments complete correctly”; record “run_command(echo hello) exited 0”; mark it passed. The contract accepts it and is_verified becomes true.

**Cause:** [_contract_tool](../../shamsu/agents/simple_chat.py), line 6792, uses the latest successful observed run without relating it to the assertion. Observations are strings rather than requirement-linked evidence.

**Fix:** bind assertion IDs to checker/tool-call IDs, cwd, expected result and source revision. Invalidate passes when relevant files change. Separate command success from requirement verification.

Evidence: core.unrelated_command_verifies_assertion.

### F17 — P2: bracket scans overclaim syntax validation

**Reproduced:** TypeScript “const x: = 1;” receives an ok/brackets result. The loop can summarize successful checks as “no syntax errors.”

**Cause:** [simple_verify._check](../../shamsu/agents/simple_verify.py), line 119, uses bracket checks for TS/TSX, JSX and other languages. Balanced brackets do not prove syntax/types.

**Fix:** label structural-only checks honestly and use installed project compiler/build tools. TypeScript's compiler API supplies project-aware diagnostics and symbol relationships. [Official guide](https://github.com/microsoft/TypeScript/wiki/Using-the-Compiler-API).

Evidence: policy.typescript_false_syntax_pass.

### F18 — P2: the web client duplicates submitted prompts

**Reproduced in Chromium:** send's optimistic bubble plus the corresponding turn.start produces two user bubbles.

**Cause:** [send/handleEvent](../../shamsu/webui/static/app.js) lack shared message-ID reconciliation.

**Fix:** submit a client message ID, echo it from the server, and reconcile pending messages. Failed sends need an explicit retry state.

Evidence: core.ui_duplicate_prompt; [screenshot](ui-duplicate-prompt.png).

### F19 — P1: responses can appear under the wrong thread

**Reproduced:** start loading A, load B, then let A return. The heading/active session say B while the conversation shows A's answer. The probe forces response ordering through the actual openSession implementation.

**Cause:** openSession changes selection before awaiting a fetch without cancelling/discarding stale responses. The old stream remains connected during the fetch. Running-state reconciliation also needs a reset when selection changes.

**Fix:** abort prior requests, close the prior stream immediately, and check a session/generation token before rendering. Scope conversation, queue and status to session identity.

Evidence: core.ui_session_fetch_race; [mobile screenshot](ui-wrong-thread-mobile.png).

### F20 — P2: approval previews are hidden; IME Enter submits

**Reproduced:** the API-supplied preview marker is absent from an approval card. An Enter event with isComposing=true still submits.

**Fix:** show the exact command/diff, targets and workspace before Allow; handle composition state. Add visible stream connection/reconnection state—the current onerror handler is silent.

Evidence: ui_extra; [approval screenshot](ui-approval-preview-missing.png). The exercised desktop/mobile states had no horizontal overflow or JS page errors. Escape closed Settings successfully and is not a reported defect.

## Architecture improvements

The main structural problem is duplicated policy: the older orchestrator, default loop, registry, special handlers, verification packages, memory APIs and interfaces each implement parts of “allowed” and “completed.”

The live modules contain **20,371 lines in cli/repl.py**, **10,425 in agents/simple_chat.py**, and **4,414 in tools/agent_tools.py**. Size alone is not a defect, but these combine transport, policy, context, mutation, model calls and presentation. More keyword exceptions will be hard to reason about across them.

Use this boundary: **interfaces → run service + request policy → context compiler + tool executor + memory/evidence store → typed turn events → interfaces**.

1. Build a request policy containing scope, approvals, dry-run, network access and timeout. Every handler receives it; skills/renderers cannot grant permission.
2. Define each tool once: descriptor, input schema, executor, effect class and result type. Generate model schemas, help, dispatch and checks from that registry.
3. Build one measured prompt bundle with a manifest of sources, hashes, truncation and exclusions.
4. Store evidence as records with IDs, source revisions and requirement links. A passing check belongs to the revision it checked.
5. Let one transaction owner apply, validate, commit, roll back and invalidate indexes.
6. Replace process-global selection state with explicit workspace/session/run identity where multiple sessions can be read concurrently.

Keep the shared turn stream, deterministic retrieval, transactional edits, exact failure messages and bounded retries. A framework rewrite is unnecessary to address these defects.

## Better context management for small models

Fix F05–F09 before enlarging windows. A larger window can conceal failures and increase latency without correcting them.

- Pin user constraints, current step/check, relevant interfaces, exact changed code and latest diagnostic. Keep old outputs/file bodies as blobs with IDs and range-read tools.
- Retrieve named paths, definitions, relevant callers/types/imports and nearest tests in that order. Use lexical search first, structural expansion second, embeddings when they add measured recall.
- Budget complete tool interactions. Summaries should distinguish a requirement, model proposal, observed fact and unverified assumption, with evidence links.
- Measure system text, schemas, plans, skills, memory, arguments, chat envelopes and output reserve together. Recount the serialized payload after truncation.
- Use model-specific calibration; a Qwen3 estimator is useful but not exact for every model. Invalidate calibration when model/tokenizer configuration changes.
- Avoid disk work in budget calculations. _messages and _fixed_overhead both recall memory, and recall can rewrite all notes. Read once per stable input; batch usage updates.
- Key code caches to content hashes/index generation and refresh after external edits as well as harness edits.

Tune context allocation using task success and latency on target hardware. Do not assume 32k always outperforms 8k/16k. Protect minimum generation space so the model can finish a patch and report its checks.

## Make memory trustworthy

Use the existing SQLite infrastructure as the authoritative store. Separate concepts with record types, rather than disconnected backends:

| Record | Necessary behavior |
|---|---|
| User constraint/preference | Source message, explicit priority and scope; persists until changed/revoked. |
| Project convention/decision | Affected paths, provenance, confidence and superseding decision. |
| Working task state | Current step, pending checks, blockers and resume pointer. |
| Observed evidence | Tool-call ID, revision/hash, command/result and timestamp. |
| Derived summary | Source-message range and observed/inferred distinction. |

Use transactions, schema validation, deduplication and supersession. Revalidate code-derived facts when files change. Make edit/forget operate through the API the model uses.

Current note recall is substring scoring, capped at five notes and approximately 600 tokens. That is cheap and bounded, but it is not semantic understanding. Ties do not explicitly favor newer decisions. Usage-based decay can preserve frequently repeated stale statements. Explicit constraints should not depend on ordinary keyword matching or age-based forgetting.

Automatic evidence notes mostly record task, filenames, failures, time and rounds; they do not reliably capture why a solution was chosen. Add a small validated decision/checkpoint record at task boundaries, retaining evidence links. Redact secrets before durable capture. Identify repository/web text as untrusted retrieved content instead of promoting it to authoritative instructions.

Measure disk retention separately from RAM. Report harness RSS, model RAM/VRAM, browser/OCR cost, cold-start latency and log growth independently. Add retention/export rules for regenerable logs while preserving user decisions and required correctness evidence.

## Better output, tools and modern stacks

Give a small model fewer decisions per step: a concrete next action, exact symbol/file anchors, a small relevant catalog and immediate compiler/test feedback. Keep retries bounded and switch strategy from observed failures. Avoid repeated whole-file generation and narration consuming the work budget.

Use native tool calling where supported. For small plans/checkpoints, schema-constrained output plus validation can reduce parser repair; it does not guarantee correct code. [Ollama structured-output documentation](https://docs.ollama.com/capabilities/structured-outputs).

Prioritize these capabilities after shared enforcement works:

| Capability | Direction |
|---|---|
| Type-aware code navigation | Definition, references, diagnostics, rename and import repair through language-service adapters using project configuration. |
| Build/test adapters | Detect manifests, package manager, lockfile and workspace boundaries. Honor npm/pnpm/yarn/bun; support pytest/uv, Go and Cargo explicitly. |
| Safe editing | Exact patches with preimage hashes, syntax-aware replacements where supported, atomic multi-file transactions. |
| Browser verification | Interact with flows, inspect console/network failures, assert visible behavior at relevant viewports. HTTP 200/screenshots alone are insufficient. [Playwright assertions](https://playwright.dev/docs/test-assertions). |
| Runtime ownership | Start/stop run-owned processes, propagate cancellation, capture bounded outputs with blob references, detect port conflicts. |
| MCP discovery | Select a handful of relevant external tools, pass their actual schemas, and enforce policy during execution. |
| Database checks | Inspect schema/migrations in development/test databases, run targeted checks and link outcomes to requirements. |
| Versioned docs | Retrieve official docs for installed dependency versions and retain source/version/date. |

Skills are procedures over tools, not new executable capabilities. Useful packs: TypeScript/React; detected frameworks such as Next.js; Python/FastAPI/Django; SQL migrations; monorepos/package managers; browser accessibility/responsive QA; auth changes; compiler/runtime debugging.

Each pack should declare applicability, version assumptions, required tools, verification commands and a compact core with optional references. Honor off/shadow/on and explain what actually ran. Avoid imposing a Vite layout or harness-owned test entry point on every existing React project; preserve its structure unless the task requires changing it.

Move heavy optional features into extras: browser automation, OCR/PDF extraction, voice and graph integrations should not all be required for a minimal coding CLI. Add reproducible dependency constraints and test minimal/full installs separately.

## UI language

The shell was readable at 1280×800 and 390×844. Thread identity, truthful status, approval previews and send recovery matter more than visual redesign.

| Internal term | User-facing wording |
|---|---|
| contract / assertion | Acceptance checklist / check |
| abstract | Code index or symbol search |
| degraded | Basic search available; symbol search unavailable |
| hydrate | Restore conversation |
| elision | Older file contents removed from prompt; still on disk |
| artifact integrity | Run records are complete |
| lease | Session in use by another window |
| roster | Available tools |
| verified_by | Checked using |
| bind workspace | Choose the bot's project |
| RSS / VRAM | App memory / GPU memory; technical detail expandable |

Separate “run finished,” “checks passed” and “request completed.” A clean event log does not establish correct software. Explain context removal and recovery without repeatedly printing internal categories/correction bookkeeping at normal verbosity.

## Validation and benchmark limits

- Active collection found **4,462 tests**.
- Plain pytest collection failed with **six errors in legacy-code/tests**. Configure testpaths/archive exclusions so the default command works.
- The full active run terminated at the configured **300-second test timeout**, inside runtime/ollama.py::_run_ollama_command during model inventory. HTTP/SDK guards do not fully cover the CLI subprocess route. This was not a completed full-suite pass.
- The focused run finished **879 passed, one failed in 389.50s**. The failure, test_a_prompt_is_refused_when_the_token_is_demanded, was a Windows connection-abort error. Its isolated rerun passed in **1.83s**. Treat it as an observed transient failure pending investigation, not a proven auth bypass.
- CI's ruff command returned **1,072 findings**. Many are import/style/noqa issues, but the configured gate is red. Resolve deliberately rather than mass-fixing during this audit.
- pyproject.toml configures pytest-timeout but omits it from the dev extra. It was installed here; declare it for clean installations.
- tests/conftest.py forces legacy routing for nearly every test file except test_simple_chat.py. That preserves old tests but does not prove default-path parity. Existing MCP integration tests use the older agent.
- This environment was Windows/Python 3.13; CI targets Python 3.11 across operating systems. Cross-platform execution was not performed here.

The checked-in benchmark reports **12/16 default cases** passing by majority over seven samples, and **9/12 light cases** over three samples. These are case-majority scores, not a general probability of coding success. The default table contains **91/112 successful attempts**; row durations sum attempts. Some planning cases use specialist drivers. RELEASE_VALIDATION.md explicitly says it is stale and retains old budget labels.

Rebuild evidence before strengthening claims. Record commit/dirty state, model digest/quantization, Ollama version, prompt/context settings, hardware, tool and dependency versions. Report per-attempt/case-majority success, uncertainty, median/p95 latency, failures, edit churn, truncation and unverified claims separately.

Add real multi-file tasks with hidden acceptance checks: TS API/import/test changes; monorepo packages; migrations/rollback; browser flows; dependency upgrades; continuation after compaction; external edits; truncated-output recovery. Compare baseline and changes on the same tasks/seeds. Include adversarial scripted-model policy tests: enforcement must survive a wrong tool choice even if a model usually behaves.

## Recommended order

1. **Release-blocking correctness:** deny/dry-run, read-only scope, search containment, Host defense and journal rollback. Gate the default route using actual disk state and evidence consistency.
2. **Reliable context/memory:** token-exact caps, complete interactions, one prompt calculation, cache freshness, transactional storage and one /memory API.
3. **Truthful completion/UI:** requirement-linked evidence, honest check labels, thread race/duplicate fixes, approval previews and transport status.
4. **Useful expansion:** MCP in the default registry, shared skill policy, project-aware language/build/browser adapters and versioned docs.
5. **Prove improvement:** clean-install/CI gates and versioned live-model evaluations on the intended hardware/model tiers.

SHAMSU already has many capabilities. Making the default path enforce and connect them consistently should improve output quality more than adding another planner, memory backend or broad tool catalog.
