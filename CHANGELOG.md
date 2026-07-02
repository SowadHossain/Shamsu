# Changelog

All notable SHAMSU release changes are documented here.

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
