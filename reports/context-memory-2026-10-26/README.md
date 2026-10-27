# Context and memory hardening - 2026-10-26

Implemented the next batch from the harness audit: context correctness and shared durable memory. Existing working-tree changes were preserved. This report describes this batch, not a full-repository clean bill of health.

## Corrected behavior

| Audit finding | Result |
|---|---|
| F05: orphaned tool results under a small budget | History selection retains complete user-led turns. Oversized current turns are elided with call/result structure intact, or refused before sending. |
| F06: truncation exceeds its token cap | The final serialized result, marker and continuation guidance are measured. CJK, escaped emoji, dense code and oversized JSON metadata are covered. JSON remains valid. |
| F07: project/plan overhead omitted | One grounding builder supplies both accounting and transmission. Summary growth triggers another selection pass. Calibration covers the full usable prompt budget. |
| F09: stale code context | File-content fingerprints invalidate outlines and briefs after edits, deletes and external changes, including same-size/same-mtime edits. |
| F10: overlapping writers lose notes | Notes share transactional SQLite records. Deduplication and writes are serialized; stale tier/usage snapshots cannot overwrite newer facts. |
| F11: failed save reports success | Database write failures return failure. Committed data is distinguished from optional export/maintenance failures. |
| F12: default memory and /memory are disconnected | Both interfaces read/write/forget the same records. Explicit preferences reach default prompts without keyword overlap, including non-English task text. |
| F13: recall destroys edited Markdown | Old files are retained. New Markdown is labelled as a generated snapshot; supported edits use the memory API. Recall does not rewrite Markdown. |

The original context probe now reports a changed outline containing new_name, SQLite preferences injected into default memory, and exactly 2,048 measured tokens against the 2,048-token tool-result cap. Its separate skills-off probe still reproduces the outstanding skills issue; see [original probe output](original-context-probe.log).

## Migration and compatibility

See [MEMORY_CONTEXT.md](../../MEMORY_CONTEXT.md). The next project-memory open imports legacy JSON notes once into `.shamsu/memory/memory.db`, preserving IDs and metadata. Forget markers also migrate into SQLite. Original JSON and Markdown remain available for recovery/review; the imports cannot resurrect forgotten facts on later startups. Existing manually edited Markdown is not automatically merged into the imported JSON facts.

Generated snapshots live under `notes/exports`. Repeating `memory_remember` with the same type/title edits the stored note while preserving its ID and creation time. Both CLI and model tools can forget that record. Retrieval remains bounded to five records and a 600-token complete block.

An integration run exposed Windows extended drive paths being rejected as outside their own workspace. Sandbox comparison now normalizes ordinary drive/UNC aliases after resolving symlinks. Tests retain rejection of sibling escapes and do not reinterpret device namespaces.

## Validation

- **817 passed** in the broad context, simple-loop, memory, settings, session and token-budget regression run: [results](regression-tests.log).
- **43 passed** in the final audit-specific, Windows-path and separate-process browser acceptance run: [results](acceptance-tests.log). These include later edge cases added after the broad run; counts overlap with the regression run.
- **27 passed** in standalone service, policy, browser and conversation integration checks: [results](service-tests.log).
- **98 passed, 1 skipped** in path/sandbox, patch, CLI-workspace and safety compatibility checks: [results](safety-tests.log).
- Ruff checks for undefined/unused names and fatal syntax issues passed for changed implementation and new tests; `git diff --check` passed.

The browser tests start a separate service process with scripted Ollama responses. They verify file creation after closing a tab and restored conversation history. Concurrent-memory tests include both threads and separate processes. These are harness checks, not real-model coding-quality measurements.

## Remaining work

F08 rollback-journal consistency, F14 default-loop MCP tools, F15 skills settings, F16 requirement-linked verification and F17 real compiler validation remain separate work. This batch did not rebuild/restart the Docker stack or complete the pending real-model Docker smoke test. Token limits are enforced using the active local tokenizer/estimator and calibration; exact agreement with every model is not claimed.
