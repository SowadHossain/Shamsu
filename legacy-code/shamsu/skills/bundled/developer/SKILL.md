---
name: developer
description: Default coding discipline for inspecting, editing, verifying, and reporting workspace changes.
---
# Developer Skill

Use this skill for coding, bug fixes, generated projects, tests, and docs that
touch workspace files.

- Inspect relevant files before editing.
- Make one bounded change at a time.
- Prefer existing project conventions and helper APIs.
- Use transactional file tools for mutations.
- Default to `write_file` with the COMPLETE file content, for new files and for
  changes to existing ones alike: read the file, then re-emit all of it with the
  change applied. Reserve `edit_file` for files too large to re-emit, and
  `append_file` for content added at the end. A failed `edit_file` match should
  become a `write_file` call, not a retry.
- Change one file per turn. Re-emitting a whole file is only safe when that file
  is the turn's single target.
- Run Python and package commands normally through `run_command`; the command
  harness selects an existing project environment or creates a local `.venv`
  before a bare package install.
- When the user asks to retain a small library guide, use `ingest_docs` instead
  of copying it with a generic file tool. For external API facts that have not
  been ingested, use `web_search` and `fetch_url` rather than guessing.
- Run the narrowest meaningful verifier after each mutation.
- Treat command/test output as evidence, not decoration.
- If verification fails, repair the first actionable root cause before broadening scope.
- Never claim completion without file, command, test, browser, or acceptance evidence.
