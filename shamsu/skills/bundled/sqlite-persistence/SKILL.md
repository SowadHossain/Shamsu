---
name: sqlite-persistence
description: Build local persistence, seed data, migrations, and restart-safe status scripts.
---
# SQLite Persistence Skill

Use this skill for SQLite, JSON fallback persistence, migrations, seed data,
status scripts, and data durability requirements.

- Prefer real SQLite when dependencies are already available or explicitly required.
- Use a deterministic JSON fallback when the environment cannot compile native SQLite packages.
- Seed commands must create the exact records requested by the PRD.
- Status commands must compute counts from persisted data, not print hardcoded summaries.
- Keep persistence files in the project root unless the PRD names a path.
- Add tests for seed count, restart/readback, and status computations.
