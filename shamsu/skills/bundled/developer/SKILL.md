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
- Use `edit_file` with a unique existing anchor for replacements, `append_file`
  for content added at the end of an existing file, and `write_file` for new
  files or intentional full rewrites.
- Run Python and package commands normally through `run_command`; the command
  harness selects an existing project environment or creates a local `.venv`
  before a bare package install.
- Run the narrowest meaningful verifier after each mutation.
- Treat command/test output as evidence, not decoration.
- If verification fails, repair the first actionable root cause before broadening scope.
- Never claim completion without file, command, test, browser, or acceptance evidence.
