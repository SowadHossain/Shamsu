# Changelog

All notable SHAMSU release changes are documented here.

## Unreleased

### Added

- A per-call content cap for every tool that carries a payload
  (`write_file`, `append_file`, `patch_file`, `read_and_patch`,
  `create_and_run`): `clamp(2,000 | 0.85 x reply_cap | 8,000)` characters. This
  restores smallcode's 4x headroom ratio, where the model is never permitted to
  attempt a write large enough to exhaust its own output budget. The ceiling is
  llama.cpp's ~13KB tool-argument wall, which does NOT report
  `done_reason: "length"` and so was invisible to the existing truncation
  guard; the floor says the window is the wrong shape for the task rather than
  degrading to useless chunk sizes. `MAX_REPLY_TOKENS` is deliberately
  unchanged - the unit of work is bounded, not the budget.
- The number 60 lines is now stated in the system prompt AND in the schema
  description of every payload argument, not only enforced in the tool. A
  refusal names the strategy ("write the first 60 lines, then append_file each
  section") rather than only the limit, which turns an unrecoverable failure
  into a recoverable one: the content was fully generated and is merely
  rejected at the door.
- A pre-write gate that tests for TRUNCATION SIGNATURES rather than validity -
  an unterminated string or comment, a dangling operator, a bracket opened on
  the last line - for new files and for every language, closing the hole where
  both write-time gates bailed out when the target did not already exist and
  only understood Python. A first section with unclosed blocks passes, because
  a gate testing for validity would refuse every legitimate chunk.
- Continue-from-the-tail recovery in simple mode, language-agnostic: a file
  found stopping mid-construct is asked for ONLY the missing remainder, quoted
  against its own last twelve lines, instead of being resent whole.

### Fixed

- A file still being built now reports its open blocks as PROGRESS ("3 block(s)
  still open - continue with append_file") instead of as a fault. Verifying
  after every chunk was correct and had to stay; reporting an unfinished
  section as broken would have sent the model repairing a file that was simply
  not finished yet. A file left open when the turn ENDS is still failed, so the
  run outcome cannot read a half-written file as a success.
- A write that GROWS a file no longer counts toward the repeated-edit ceiling.
  Live 2026-08-20, told to build a 1,500-line file, qwen2.5:3b chunked as asked
  but carried each section with `write_file`; five verified-clean sections in,
  the turn was stopped for "5 blind edits I cannot confirm". The exemption
  already existed for `append_file` and now follows the shape rather than the
  tool.
- Unknown models no longer silently drop to an 8,192-token window. The table
  was exact-match only, so `qwen2.5:3b-instruct-q4_K_M` - one quantisation
  suffix from a listed model - asked for 8k, which collapses the reply cap and
  shrinks every write cap derived from it. Family patterns are consulted before
  the fallback.
- `test_count_tokens_falls_back_when_asset_missing` left the tokenizer cache
  memoised on a missing asset, so every later `count_tokens` in the session
  silently used chars/4.

## 0.4.0b1 - 2026-07-20

### Added

- Full-request noninteractive harness with deterministic approval policy,
  dry-run, timeout, JSON result contract, and persisted-evidence validation.
- Canonical ActionLedger run folders for every prompt, including structured
  decisions, tool/model calls, context records, command output, diagnostics,
  mutations, verification, final output, and concise `/run show` inspection.
- Composite routing, mentioned-document normalization, shared workspace file
  policy, asynchronous memory finalization, TaskFlow PRD acceptance, and
  complete PRD-to-Django generation/browser checks.
- Web provider fallback and browser capability status, one-time first-run
  readiness report, expanded `/doctor`, and idempotent workspace schema
  upgrades that preserve historical evidence.
- Three-OS CI lifecycle validation and a deterministic Python/Django/Node/
  React/mixed release dogfood benchmark.

### Fixed

- False-success outcomes, swallowed patch/fallback errors, ungrounded compound
  routing, malformed approval semantics, and non-actionable Git probe errors.
- Missing optional run artifacts on read-only requests and incomplete run
  summaries.
- Unix lifecycle scripts failing under Bash because of CRLF line endings.
- SQLite web-cache connections remaining open until garbage collection on
  Windows; cache creation is now lazy for non-web prompts.

### Measured

- 1.266s import startup, 1.071s cold first answer, 0.307s slowest warm answer,
  90.9 MB peak RSS, and 52,128 bytes of run-log growth across five deterministic
  dogfood sessions on the release workstation.
- Default model tier: 11/12 cases at three samples; light tier: 9/12. Stochastic
  cases and tier limitations are documented rather than hidden.

## MVP Release

This MVP focuses on a local-first path from workspace understanding to
approval-backed project work on low-resource machines.

### Added

- CLI REPL with workspace-scoped `index`, `status`, `log`, `search`,
  `symbols`, `parse-prd`, `models`, and `django setup` commands.
- SQLite-backed workspace index with Python symbol extraction, snippets, and
  stale-file cleanup.
- Markdown PRD parsing, rule-based entity extraction, and `ProjectSpec`
  assembly.
- Deterministic Django fixed templates for generated projects using Django,
  DRF, Simple JWT, crispy forms, DaisyUI, HTMX, and SQLite.
- Natural-language routing for QA, code edit, bug fix, audit, test generation,
  documentation, and project-generation intents.
- Approval-backed command runner with workspace checks, command risk
  classification, blocked-command rejection, timeouts, captured output, and
  secret redaction.
- Django generated-project setup runner for `pip install -r requirements.txt`,
  `makemigrations`, and `migrate`.
- Approval-backed patch validation, Rich preview, apply, rollback, backups,
  failure restore, and post-patch re-indexing.
- Read-only git dirty-worktree helpers for safer edits.
- Local JSONL audit trail under `.shamsu/` for safety-sensitive events.
- Local Ollama runtime status and repair helpers.

### Changed

- README now includes the PRD format guide, generated Django project run guide,
  demo script, and explicit known limitations.
- CLI help now presents workflow examples and generated-project setup commands.
- Local logs and command outputs are redacted before display.

### Known Limitations

- SHAMSU is a workspace sandbox, not an OS sandbox or Docker isolation layer.
- Full PRD-to-Django project generation is branch-dependent until the complete
  pipeline lands on the default branch.
- Generated Django projects target local development with SQLite.
- PostgreSQL, Docker deployment, React SPA generation, file uploads, email
  delivery, and background workers are outside the MVP scope.
- Local model quality and speed depend on the installed Ollama models and host
  hardware.
