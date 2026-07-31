---
name: prd-planner
description: Compile PRDs into requirement records, assumptions, milestones, and acceptance evidence.
---
# PRD Planner Skill

Use this skill when a prompt references a PRD, acceptance criteria, or a complete
project build from a requirements document.

- Extract explicit acceptance commands and expected output.
- Assign stable requirement IDs for user-visible features, data, scripts, and tests.
- Separate assumptions from confirmed requirements.
- Map each in-scope requirement to a milestone and verifier.
- Mark unsupported or ambiguous requirements as visible blockers.
- Keep the PRD contract in the prompt; do not rely on memory of earlier turns.
- Final output must report verified requirements and remaining gaps.
