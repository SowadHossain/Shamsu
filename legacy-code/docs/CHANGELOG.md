# Changelog

All notable SHAMSU release changes are documented here.

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
