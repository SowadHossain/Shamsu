# Context and project memory

Shamsu now uses the same SQLite project memory for `/memory`, model memory
tools, and the default loop's prompt retrieval. The rolling conversation summary
remains separate: it describes a session, while project memory survives sessions.

## Where memory lives

- `.shamsu/memory/memory.db` is authoritative. Records, forget markers, and
  migration markers are committed using SQLite transactions.
- Existing `notes/index.json` records are imported once, preserving their IDs
  and type/tag/timestamp metadata. Existing files are retained. Damaged indexes
  are moved to unique `.corrupt` recovery files instead of overwritten.
  Manually edited legacy Markdown is retained for review; migration imports the
  JSON index and does not automatically merge those Markdown edits.
- Existing `tombstones.json` forget markers are imported once. Later startups
  cannot resurrect forgotten records by importing those files again.
- `notes/exports/*.md` contains generated snapshots. Editing these files does
  not edit memory. Recall does not rewrite them or original Markdown files.

Use the memory tools to inspect and change records. `memory_remember` with the
same type and title updates the existing note, retaining its ID and creation
time. `memory_forget` accepts that ID. CLI-created records are visible to these
tools, and forgetting from either interface affects both.

Saves only report success after the database commits. A failed Markdown export
is reported separately from a successful database save. Concurrent writers
serialize deduplication and writes; stale usage/tier snapshots cannot overwrite
a newer fact. SQLite connections are explicitly closed.

Prompt retrieval remains bounded: at most five records and a 600-token rendered
block, including its heading. User preferences and safety rules can be recalled
without literal keyword overlap. Ordinary notes are selected for the task, and
older model notes retain the existing archive/expiry behavior. Explicit CLI
preferences are not subject to model-note expiry.

## Context assembly

Conversation selection keeps complete user-led turns. An assistant tool call
and its result are never separated by history selection. If the current turn
is oversized, the loop shortens tool payloads while keeping the interaction
structure and the user's request. It reports an insufficient-context error
before sending a request if required context still cannot fit.

One grounding builder supplies the exact text used for both budgeting and
transmission: project facts, standing plans, recalled memory, skills, file
listing, graph brief, and local outlines. A newly expanded conversation summary
triggers another budget pass. Calibration applies to the complete prompt budget.

Tool-result truncation measures the final serialized output, including its
marker and continuation guidance. Oversized JSON remains valid JSON; Unicode
and escape-heavy payloads no longer rely on a four-characters-per-token slice.
Truncated file reads name the next range and preserve complete lines when possible.

Code-context caches include content fingerprints. Edits, deletions, and external
changes invalidate outlines, including edits that preserve file size and mtime.
An older graph brief is withheld after a detected source change until refreshed;
fresh local outlines remain available.

Token counts use the configured local tokenizer/estimator and runtime calibration.
These changes enforce the measured budget; they do not establish exact token
agreement with every model or prove real-model coding quality.

## Validation and remaining work

Audit-specific tests cover Unicode/JSON caps, interaction grouping, prompt
overhead, summary growth, stale code context, concurrency, old-schema migration,
cross-interface recall/forget, failed writes, and preservation of existing files.
See `reports/context-memory-2026-10-26/README.md` for results.

Verification integrity, default-loop MCP integration, consistent skill settings,
real-model task evaluations, and the final Docker smoke test remain separate
work from this context/memory batch.
