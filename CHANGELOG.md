# Changelog

## MVP - 2026-07-02

SHAMSU now has a local-first MVP path from PRD to generated Django project.

### Added

- Workspace indexing with SQLite FTS5 search and Python symbol extraction.
- Local-only Ollama runtime checks, model repair commands, and installer bootstrap.
- Claude-like REPL with natural prompt routing, sessions, redacted JSONL logs, and exports.
- Safe code-edit, bug-fix, audit, test-generation, and documentation workflows.
- Approval-backed unified diff preview/apply with rollback and post-patch re-indexing.
- Markdown, TXT, and PDF PRD parsing with rule-based `ProjectSpec` extraction.
- Deterministic Django generation for fixed files, backend files, frontend templates, tests, docs, and summary reports.
- Generated-project setup, migrations, tests, and test-failure feedback loop.
- Backend and frontend consistency checks for generated Django projects.
- Todo, Expense Tracker, and Blog PRD fixtures.
- MVP benchmark report in `BENCHMARK.md`.

### Safety

- Runtime LLM calls are local-only.
- File reads/writes are scoped to the selected workspace sandbox.
- Install scripts use repo-local `.venv` and do not edit shell profiles, PATH, registry, or global Python.
- Commands run through guarded execution with risk classification, approval gates, timeouts, and redacted output.
- Session logs are workspace-local and redacted by default.

### Known Limitations

- SHAMSU is not a Docker/container or full OS sandbox.
- Generated projects are MVP-quality Django apps, not finished production products.
- Visual design is deterministic and functional rather than custom-designed per brand.
- Local LLM quality depends on installed Ollama models and available machine resources.
- Arbitrary shell execution is not exposed as a user-facing REPL command.
