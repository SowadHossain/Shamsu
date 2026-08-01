# Shamsu PRD Test Feedback: Canva-Like App PRD

## PRD Used

File: `SHAMSU_CANVA_LIKE_APP_PRD.md`

Project title: `StudioForge`

Concept: A Canva-like local-first visual design application with a real browser editor, canvas object model, templates, brand kits, asset uploads, layers, undo/redo, export/import, CLI automation, persistence, and tests.

## Test Environment

- Shamsu directory: `C:\Users\Mastu\Desktop\Shamsu`
- Workspace shown by Shamsu: `C:\Users\Mastu\Desktop\Shamsu`
- Runtime: local Ollama
- Shamsu prompt: interactive `shamsu>` REPL

## What Worked

Shamsu parsed the PRD and saved plan metadata.

The `/plan-prd SHAMSU_CANVA_LIKE_APP_PRD.md` output showed:

```text
Project: product_requirements_document_studio_forge
App: forge
Theme: corporate
Status: ready
Entities: 10
Endpoints: 50
Pages: 40
Files planned: 2
Extraction confidence: 100%
```

Positive signal:

- Shamsu found the PRD.
- Shamsu extracted 10 entities.
- Shamsu extracted 50 endpoints.
- Shamsu extracted 40 pages.
- Extraction confidence was 100%.

Concern:

- `Files planned: 2` is too shallow for this PRD. A Canva-like editor needs a real app scaffold, canvas/editor modules, data model, persistence, asset/template systems, CLI, tests, and export/import code.

## Approval Step

Shamsu requested approval to write:

```text
.shamsu/generation-state.json
```

Reason shown:

```text
M3 only stores resume metadata; it does not generate project files.
```

This part looked reasonable. It saved the plan metadata after approval.

## Failure 1: Plan-Only Follow-Up Routed Into Build Pipeline

After the shallow plan, the user asked Shamsu not to generate and requested a detailed milestone implementation plan only:

```text
The plan is too shallow. Files planned is only 2, but this PRD requires a real Canva-like editor.

Do not generate yet.

Re-read SHAMSU_CANVA_LIKE_APP_PRD.md and produce a detailed milestone implementation plan only.

Include:
- data model tables
- editor canvas architecture
- UI views
- CLI commands
- tests
- export/import strategy
- which files you would create for Milestone 1 only

Do not write files yet.
```

Observed result:

```text
Full Project Pipeline
Result: failed
Pipeline Error: no generation plan
```

Issue:

The request was explicitly read-only / planning-only, but Shamsu routed it into the full project generation pipeline.

Expected behavior:

Shamsu should have answered in plain text with a detailed implementation plan, not invoked the build pipeline.

## Failure 2: Plain-Text Summary Request Still Routed Into PRD Build

The user then tried an even more explicit read-only prompt:

```text
Answer in plain text only.

Read SHAMSU_CANVA_LIKE_APP_PRD.md and summarize it.

Do not build.
Do not generate.
Do not run pipeline.
Do not write files.

I only want a bullet list containing:
1. entities
2. UI screens
3. editor systems
4. CLI commands
5. tests
6. Milestone 1 files
```

Observed result:

```text
PRD Build Needs Input
The requested project folder is not empty: product_requirements_document_studio_forge. Choose a new folder so the PRD build cannot overwrite unrelated work.
```

Issue:

Even with clear negative instructions, Shamsu still routed the request into PRD build mode and asked for a new project folder.

Expected behavior:

Shamsu should have handled this as read-only PRD summarization.

## Main Issue

Shamsu is over-routing PRD-related follow-up prompts into build/generation mode.

The route should distinguish between:

- `parse PRD`
- `plan PRD`
- `summarize PRD`
- `inspect last parsed PRD`
- `produce implementation plan text`
- `build/generate from PRD`

Currently, follow-up prompts containing words like `implementation plan`, `Milestone`, or PRD file names can accidentally trigger the build pipeline even when the user explicitly says not to build or write files.

## Suggested Fixes

1. Add a read-only PRD summary route with higher priority than PRD build.

   Example prompts that should be read-only:

   - "summarize this PRD"
   - "list the entities from the PRD"
   - "what pages did you find?"
   - "produce a milestone plan only"
   - "do not build"
   - "do not generate"
   - "do not write files"

2. Treat explicit negative build instructions as hard blockers.

   If the prompt contains:

   - `do not build`
   - `do not generate`
   - `do not write files`
   - `plain text only`
   - `plan only`

   then Shamsu should not enter PRD build mode.

3. Add a `/prd summary <file>` command.

   This would avoid relying on route classification for common PRD inspection workflows.

4. Add a `/prd entities <file>` command.

   This would help users validate extraction quality without triggering generation.

5. Add a `/prd milestone-plan <file>` command.

   This should output a detailed implementation plan and never write files.

6. Improve the PRD plan preview when files planned are suspiciously low.

   For example, if a PRD has 10 entities, 40 pages, and 50 endpoints but only 2 files planned, Shamsu should warn:

   ```text
   This plan may be too shallow for the PRD. Do you want a detailed implementation plan instead?
   ```

## Reproduction Steps

From PowerShell:

```powershell
cd "C:\Users\Mastu\Desktop\Shamsu"
.\scripts\run-shamsu.ps1
```

Inside Shamsu:

```text
/parse-prd SHAMSU_CANVA_LIKE_APP_PRD.md
/plan-prd SHAMSU_CANVA_LIKE_APP_PRD.md
```

Approve the metadata write.

Then run:

```text
The plan is too shallow. Files planned is only 2, but this PRD requires a real Canva-like editor.

Do not generate yet.

Re-read SHAMSU_CANVA_LIKE_APP_PRD.md and produce a detailed milestone implementation plan only.

Include:
- data model tables
- editor canvas architecture
- UI views
- CLI commands
- tests
- export/import strategy
- which files you would create for Milestone 1 only

Do not write files yet.
```

Observed:

```text
Full Project Pipeline
Pipeline Error: no generation plan
```

Then run:

```text
Answer in plain text only.

Read SHAMSU_CANVA_LIKE_APP_PRD.md and summarize it.

Do not build.
Do not generate.
Do not run pipeline.
Do not write files.

I only want a bullet list containing:
1. entities
2. UI screens
3. editor systems
4. CLI commands
5. tests
6. Milestone 1 files
```

Observed:

```text
PRD Build Needs Input
The requested project folder is not empty: product_requirements_document_studio_forge. Choose a new folder so the PRD build cannot overwrite unrelated work.
```

## Expected Result

Shamsu should respond with a read-only summary like:

```text
Entities:
- UserProfile
- DesignProject
- DesignPage
- CanvasObject
- TextStyle
- Asset
- Template
- BrandKit
- ExportJob
- ProjectHistoryEvent

UI screens:
- Editor
- Project dashboard
- Template gallery
- Brand kit editor
- Asset library
- Export dialog
- Settings

Milestone 1 files:
- package.json
- src/App.tsx
- src/editor/EditorShell.tsx
- src/editor/CanvasWorkspace.tsx
- src/dashboard/ProjectDashboard.tsx
- src/cli/index.ts
- tests/smoke.test.ts
```

No build pipeline should run until the user explicitly asks to build or generate files.

