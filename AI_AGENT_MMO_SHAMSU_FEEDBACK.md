# Shamsu PRD Test Feedback: AI Agent MMO PRD

## PRD Used

File: `AI_AGENT_MMO_PRD.md`

Project title: `Eidolon Realms`

Concept: A World of Warcraft-style fantasy MMO simulation designed for small and large populations of AI agents. The PRD includes autonomous agents, parties, guilds, combat, quests, economy, agent memory, simulation ticks, world events, CLI controls, UI views, persistence, scaling modes, and tests.

## Test Environment

- Shamsu directory: `C:\Users\Mastu\Desktop\Shamsu`
- Workspace shown by Shamsu: `C:\Users\Mastu\Desktop\Shamsu`
- Shamsu version shown: `v0.4.0b1`
- Model shown: `qwen2.5:3b-instruct`
- Tier shown: `light tier`
- Runtime: local Ollama
- Autonomy: off

## What Worked

Shamsu successfully recognized and parsed the PRD much better than earlier PRDs.

The `/plan-prd AI_AGENT_MMO_PRD.md` output showed:

- Project: `product_requirements_document_eidolon_realms`
- App: `realms`
- Theme: `nord`
- Status: `ready`
- Entities: `14`
- Endpoints: `70`
- Pages: `43`
- Files planned: `54`
- Extraction confidence: `100%`

This was a good sign because the PRD had many explicit entity sections and Shamsu detected a meaningful project shape.

## Approval Step

Shamsu requested approval for:

- Action: `file_write`
- Risk: `medium`
- Working dir: `C:\Users\Mastu\Desktop\Shamsu`
- Target: `.shamsu/generation-state.json`

Reason shown:

```text
M3 only stores resume metadata; it does not generate project files.
```

This part looked reasonable. It was asking to store resume/project-plan metadata, not to generate the actual application yet.

## Agent Response After Milestone Prompt

After being asked to implement only Milestone 1 from the PRD, Shamsu first timed out with:

```text
Timeout: Model call timed out (category: planner_returned_but_executor_stalled).
Agent stopped before completing all requested work.
```

The displayed explanation was:

```text
The model call timed out after 120s (category: planner_returned_but_executor_stalled).
The planner already returned a plan, so this is an agent-loop / executor stall waiting on the next model response - not necessarily a GPU problem. Retry, reduce context, or try `/models tier light`.
```

The agent then attempted a smaller plan and produced this intended file list:

```text
1. Create `shamsu/simulation/simulation.py` for the scaffold.
2. Create `shamsu/cli/simulation_cli.py` for the CLI scaffold.
3. Create `shamsu/db/simulation.db` for the SQLite schema placeholder.
4. Create `shamsu/db/seed_simulation.py` for the seed command placeholder.
5. Create `shamsu/dashboard/simulation_dashboard.py` for the dashboard placeholder.
6. Create `tests/test_simulation.py` for the basic tests.
```

However, it timed out again with the same category:

```text
Timeout: Model call timed out (category: planner_returned_but_executor_stalled).
Agent stopped before completing all requested work.
```

Final composite result:

```text
Composite: Failed
Step 1 (mutation): failed
Step 2 (mutation): failed
```

## Main Issue

The parser/planner did a good job extracting a project plan from the PRD, but execution stalled before file mutations completed.

This looks like an executor-loop or follow-up model-call stall, not a PRD parsing failure. The important clue is:

```text
planner_returned_but_executor_stalled
```

So the planner produced a plan, but the execution phase did not reliably write files.

## Quality Assessment

Strong:

- PRD recognition worked.
- Entity/page/endpoint extraction was much better than previous tests.
- Extraction confidence was high.
- Shamsu generated a plausible Milestone 1 file plan.
- Approval messaging was understandable.

Weak:

- The system stalled after planning.
- No successful mutation was confirmed.
- Composite execution failed.
- The agent could not complete even a reduced Milestone 1 scaffold request.
- The user has to manually retry with smaller prompts.

## Suggested Fixes

1. Add a deterministic fallback when `planner_returned_but_executor_stalled` occurs.

   If a valid plan already exists, Shamsu should execute the planned file writes without requiring another model response.

2. Split planning and mutation into explicit phases.

   Example:

   - Phase 1: plan
   - Phase 2: confirm exact file writes
   - Phase 3: execute file writes
   - Phase 4: verify

3. Add a "write from plan" recovery command.

   Example:

   ```text
   /resume-generation --from-last-plan
   ```

4. Reduce context sent to executor after the plan is already created.

   The screenshot showed context near the limit:

   ```text
   ctx chat 30.4k/32.8k 100%
   Context: Sending 28 messages (~32055 tokens)
   ```

   The executor probably does not need the full conversation once the file plan is known.

5. When mutation fails, report whether any files were actually created.

   The final output should include:

   - Files created
   - Files modified
   - Files attempted
   - Error per file
   - Suggested next command

6. Add a one-file-at-a-time fallback mode.

   If multi-file mutation fails, Shamsu should try creating the first file only, verify it exists, then continue.

## Suggested Reproduction Steps

From PowerShell:

```powershell
cd "C:\Users\Mastu\Desktop\Shamsu"
.\scripts\run-shamsu.ps1
```

Inside Shamsu:

```text
/parse-prd AI_AGENT_MMO_PRD.md
/plan-prd AI_AGENT_MMO_PRD.md
```

Approve the metadata write when prompted.

Then run:

```text
Implement only Milestone 1 from AI_AGENT_MMO_PRD.md. Create the scaffold, CLI scaffold, SQLite schema placeholder, seed command placeholder, dashboard placeholder, and basic tests. Stop after verification.
```

Observed result:

```text
Timeout: Model call timed out (category: planner_returned_but_executor_stalled).
Composite: Failed
Step 1 (mutation): failed
Step 2 (mutation): failed
```

## Expected Result

Shamsu should create the Milestone 1 scaffold files, or at minimum create one file at a time and report exact progress.

Expected example:

```text
Created:
- shamsu/simulation/simulation.py
- shamsu/cli/simulation_cli.py
- shamsu/db/seed_simulation.py
- shamsu/dashboard/simulation_dashboard.py
- tests/test_simulation.py

Skipped:
- shamsu/db/simulation.db, because binary database files should be generated by migration/seed code instead of written directly.

Verification:
- pytest tests/test_simulation.py passed
```

## Recommended Next Test

Retry with an extremely small mutation request:

```text
Do not plan. Do not call the model again. Write only one file now: shamsu/simulation/simulation.py with a minimal World, Agent, and SimulationEngine class.
```

If that succeeds, continue one file at a time.

