# SHAMSU MVP Demo

This walkthrough shows the intended MVP flow from PRD to generated Django project.

## 1. Install

From the SHAMSU repo root:

```powershell
.\scripts\install.ps1 -Yes
```

For a faster docs/demo run without pulling models:

```powershell
.\scripts\install.ps1 -Yes -SkipModels
```

## 2. Start SHAMSU In A Workspace

Use a workspace where generated files can be written:

```powershell
mkdir demo-workspace
cd demo-workspace
& "<path-to-Shamsu>\scripts\run-shamsu.ps1" --new-session "MVP demo"
```

## 3. Add A PRD

Copy one of the bundled fixtures into the workspace, or write your own:

```powershell
Copy-Item "<path-to-Shamsu>\tests\fixtures\prds\todo.md" .\todo.md
```

## 4. Inspect And Plan

Inside the REPL:

```text
index
parse-prd todo.md
plan-prd todo.md
```

Review the preview before approving.

## 5. Generate The Django MVP

```text
generate-prd todo.md --output generated_todo
```

The full pipeline writes Django files, checks consistency, runs setup, runs tests,
and writes:

```text
generated_todo/README.md
generated_todo/SHAMSU_SUMMARY.md
```

## 6. Re-run Generated-Project Helpers

```text
django setup generated_todo
django test generated_todo
django fix-tests generated_todo
```

## 7. Review Session Logs

```text
sessions current
log tail
sessions export <session-id>
```

The export bundle is written under:

```text
.shamsu/sessions/<session-id>/exports/
```

## Expected Demo Result

- SHAMSU stays inside the selected workspace.
- File edits and generated writes require approval where applicable.
- Commands are run through guarded execution.
- Logs are redacted and local.
- The generated Django project has deterministic files, tests, and run
  instructions.
