# SHAMSU 0.4 Beta Demo Script

This script is the presenter path for a short PRD-to-Django walkthrough.

## Setup

Use a clean workspace and a small Todo PRD.

```powershell
mkdir demo-workspace
cd demo-workspace
```

Create `TODO_PRD.md`:

```markdown
# Todo App

## Overview
- A small task tracker for one user.

## Entities
- Task: title (text), description (text), due_date (date), done (boolean)
- Category: name (text)

## Pages
- Dashboard: show open tasks and completion counts
- Task List: list, add, complete, and delete tasks

## API
- GET /api/tasks/
- POST /api/tasks/
- DELETE /api/tasks/{id}/

## Auth
- Users must log in before managing tasks.
```

Start SHAMSU from the repo root:

```powershell
.\scripts\run-shamsu.ps1 -Workspace .\demo-workspace
```

## Walkthrough

1. Show the selected workspace.

   ```text
   shamsu> status
   ```

2. Parse the PRD.

   ```text
   shamsu> parse-prd TODO_PRD.md
   ```

3. Generate and verify the complete Django project.

   ```text
   shamsu> generate project from TODO_PRD.md into generated
   ```

4. Inspect the canonical run and its truthful outcome.

   ```text
   shamsu> /run show
   shamsu> /run validate
   shamsu> /run diff
   ```

   Point out structured decision summaries, tool outcomes, changed files,
   verification, and final output. Raw private chain-of-thought is intentionally
   not persisted.

5. Re-run setup or tests through approval-backed command execution if needed.

   ```text
   shamsu> django setup generated
   ```

   Point out the approval prompt, captured output, and redaction behavior.

6. Start Django.

   ```powershell
   cd .\demo-workspace\generated
   python manage.py runserver
   ```

7. Inspect the browser from SHAMSU.

   ```text
   shamsu> /browse open http://127.0.0.1:8000/
   shamsu> /browse read
   shamsu> /browse screenshot
   ```

8. Show the local operational view.

   ```text
   shamsu> status
   shamsu> /doctor
   shamsu> /runs
   ```

9. Close with the boundaries.

   - Local-first Ollama runtime.
   - Workspace-scoped files, indexes, and logs under `.shamsu/`.
   - Approval-backed command and patch flows.
   - SQLite/local-development generated apps for MVP.
   - No Node or frontend build step for generated Django templates.
   - Default/light model quality is measured separately from deterministic
     harness correctness.
