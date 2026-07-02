# SHAMSU MVP Demo Script

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

3. Generate the Django project when the active branch includes the full
   PRD-to-project pipeline.

   ```text
   shamsu> generate project from TODO_PRD.md into generated
   ```

   If that command is not available on the current branch, explain that the MVP
   docs and setup runner are ready, while the generator command is still
   branch-dependent.

4. Install dependencies and run migrations through approval-backed command
   execution.

   ```text
   shamsu> django setup generated
   ```

   Point out the approval prompt, captured output, and redaction behavior.

5. Start Django.

   ```powershell
   cd .\demo-workspace\generated
   python manage.py runserver
   ```

6. Open the browser.

   ```text
   http://127.0.0.1:8000/
   http://127.0.0.1:8000/admin/
   ```

7. Show the local operational view.

   ```text
   shamsu> status
   shamsu> log 50
   ```

8. Close with the boundaries.

   - Local-first Ollama runtime.
   - Workspace-scoped files, indexes, and logs under `.shamsu/`.
   - Approval-backed command and patch flows.
   - SQLite/local-development generated apps for MVP.
   - No Node or frontend build step for generated Django templates.
