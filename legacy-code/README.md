# legacy-code

Code the running harness cannot reach. **Moved, not deleted** - every file
keeps its original path, so restoring one is a move back to the repo root.

## How these were chosen

Three independent checks had to agree a module was dead:

1. it is not in `sys.modules` after the CLI's entry points are imported,
2. it is not statically reachable, following imports with `ast` from every
   entry point to a fixpoint, and
3. nothing in `evals/` or `scripts/` references it.

Two of those exist because each one alone was wrong. Matching import text
with a regex missed `from shamsu.diagnostics import root_cause` and
reported a live module dead - moving it broke the CLI immediately. Static
analysis alone called the whole `shamsu.memory` package dead while the
banner prints "Project memory: SQLite ready".

## What is NOT here, and why

The bulk of the legacy router still ships, because **the CLI imports 271 of
292 modules at startup**: `shamsu/cli/repl.py` is a single 18,780-line
module holding both the legacy router AND the REPL loop, slash commands,
approvals and startup that simple mode needs. Nothing behind it can be
separated until that file is split. That split is the next step, and it is
what unlocks the remaining ~47,000 lines.
