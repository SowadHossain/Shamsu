---
name: testing
description: Add deterministic verification, acceptance tests, and build checks.
---
# Testing Skill

Use this skill when the task asks for tests, acceptance checks, verification,
or bug reproduction.

- Prefer deterministic unit tests for pure logic and contract checks.
- Keep generated tests runnable with the project's existing test command.
- For PRDs, include tests that map to acceptance criteria rather than only snapshot structure.
- Run the failing command before repair when possible.
- After repair, rerun the narrow failing command before broader checks.
- Report commands, exit codes, and any skipped checks honestly.
