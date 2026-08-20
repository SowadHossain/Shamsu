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
- Change part of an existing file with `patch_file`, not by re-emitting the
  whole file. Its cost does not grow with the file, and it cannot lose the parts
  you did not mean to touch. A failed match means read the real text and copy it
  exactly - not a whole-file rewrite.
- Keep every `write_file` and `append_file` under 60 lines. Build anything
  larger in sections: `write_file` the first 60 lines, then `append_file` each
  following section. Calls much larger than that cannot be parsed reliably and
  are refused at the door.
- Read a large file by its outline first, then `read_symbol` for the one
  function or class you need. Reading the whole thing wastes the window on parts
  the task never touches.
- Change one file per turn.
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
