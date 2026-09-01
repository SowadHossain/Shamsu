---
name: developer
description: Default coding discipline for inspecting, editing, verifying, and reporting workspace changes.
---
# Developer Skill

For coding, bug fixes, generated projects, tests and docs that touch files.

**Before editing**

- Read the file first. Follow the conventions already in it.
- Read a large file by its outline, then `read_symbol` for the one function you
  need. Reading it whole spends the window on parts you will not touch.

**Editing**

- One bounded change at a time. One file per turn.
- Change part of a file with `patch_file`, not by re-emitting the whole file.
  A failed match means read the real text and copy it exactly - it does not
  mean rewrite the file.
- Keep every `write_file` and `append_file` under 60 lines. Build anything
  larger in sections: `write_file` the first 60, then `append_file` the rest.
  Much larger calls cannot be parsed reliably and are refused.

**Running**

- Run Python and package commands through `run_command`; it selects the project
  environment or creates a `.venv` before a bare install.
- For external API facts, use `web_search` and `fetch_url` rather than guessing.

**Finishing**

- Run the narrowest meaningful check after each change.
- Command and test output is evidence. Everything else is a claim.
- If it fails, repair the first real cause before widening scope.
- Never report completion without evidence.
