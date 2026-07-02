# Milestone 2 Finish Plan

Last updated: 2026-07-02

This is the takeover plan for finishing Milestone 2: Real Workflows On Existing
Projects. It records what is already done, what must still be merged/verified,
and what each dev should do next.

## Current State

- Active PR: #43, `dev-b -> develop`
- PR URL: https://github.com/SowadHossain/Shamsu/pull/43
- PR status: open, clean, CI passing
- Local branch: `dev-b`
- Local verification: `116 passed`, Ruff clean
- Milestone 2 is implementation-complete on `dev-b`, but not officially
  complete until PR #43 is merged into `develop` and issues close.

## Milestone 2 Issues

Done and already closed:

- [x] #5 Real Indexed QA As Default
- [x] #7 Audit Workflow

Implemented in PR #43, still open until PR merge:

- [ ] #6 Bug Fix Workflow
- [ ] #8 Test Generation Workflow
- [ ] #9 Documentation Generation Workflow
- [ ] #10 CLI Workflow Routing

Extra work included in PR #43:

- [x] Native local Ollama runtime bootstrap
- [x] Local-only LLM endpoint guard
- [x] Installer flags for one-command setup
- [x] REPL `models status`, `models pull`, and `models repair`

## Dev Responsibilities

### Dev 2 / SowadHossain

Owner for #6, #8, #9 and current PR #43.

Do next:

1. Review PR #43 one last time.
2. Merge PR #43 into `develop`.
3. Confirm GitHub closes #6, #8, #9, and #10.
4. If GitHub does not auto-close any issue, close it manually with a comment:
   `Completed in PR #43 and merged into develop.`
5. Sync local branches:

```powershell
git checkout develop
git pull origin develop
git checkout dev-b
git merge develop
git push origin dev-b
```

### Dev 1 / Masturajannat

Reviewer for code safety and patch/index behavior.

Review focus:

- `PatchEngine` is still the only write path for edit/fix/test/docs workflows.
- Bug fix and test generation workflows reject malformed LLM output.
- Post-patch indexing still runs after successful patch apply.
- Native runtime changes do not weaken sandbox rules.

Recommended smoke checks after #43 merges:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_patch_engine.py tests/test_bugfix_workflow.py tests/test_test_generation_workflow.py -q
.\.venv\Scripts\python.exe -m ruff check shamsu tests
```

### Dev 3 / abidjan00

Original owner for #10 CLI Workflow Routing. PR #43 implements #10, so Dev 3
should verify CLI behavior before or immediately after merge.

Review focus:

- `help` lists natural prompts and workflow commands.
- Free-form prompts route to QA, edit, bug fix, audit, test generation, or docs.
- `models status|pull|repair` are visible and friendly.
- Missing Ollama does not crash startup.
- Runtime inference remains local-only.

Recommended smoke checks:

```powershell
"exit" | .\scripts\run-shamsu.ps1
.\.venv\Scripts\python.exe -m shamsu.runtime.ollama status
```

Then interactive:

```text
help
models status
index
status
search BugFixWorkflow
symbols BugFixWorkflow
exit
```

## Merge Checklist

Before merge:

- [ ] PR #43 CI is passing.
- [ ] PR #43 merge state is clean.
- [ ] README explains one-command local runtime setup.
- [ ] `agent context/PROGRESS.md` says `116 passed`.
- [ ] Dev 1 is comfortable with patch/safety behavior.
- [ ] Dev 3 is comfortable with CLI behavior.

Merge:

- [ ] Merge PR #43 into `develop`.
- [ ] Do not merge directly to `main`.
- [ ] Do not squash away issue-closing keywords unless the final merge message
      still references `Closes #6`, `Closes #8`, `Closes #9`, and `Closes #10`.

After merge:

- [ ] Confirm #6, #8, #9, #10 are closed.
- [ ] Confirm Milestone 2 has no open issues.
- [ ] Pull `develop` locally.
- [ ] Re-run full verification on `develop`.
- [ ] Update `agent context/PROGRESS.md` from "PR open" to "Milestone 2 merged".
- [ ] Sync `dev-a`, `dev-b`, and `dev-c` from `develop`.

## Full Verification Commands

Run on `develop` after PR #43 merges:

```powershell
git checkout develop
git pull origin develop
.\.venv\Scripts\python.exe -m pytest tests/ -q
.\.venv\Scripts\python.exe -m ruff check shamsu tests
"exit" | .\scripts\run-shamsu.ps1
```

Expected:

```text
116 passed
All checks passed!
REPL boots and exits cleanly
```

## Milestone 2 Done Definition

Milestone 2 is done only when all are true:

- [ ] PR #43 is merged into `develop`.
- [ ] #5, #6, #7, #8, #9, and #10 are closed.
- [ ] Full tests pass on `develop`.
- [ ] Ruff passes on `develop`.
- [ ] CLI boots without Ollama and shows a friendly local runtime message.
- [ ] `models status` works.
- [ ] Natural prompts route through the CLI.
- [ ] File-changing workflows still require patch preview and approval.
- [ ] `agent context/PROGRESS.md` is updated after merge.

## What Starts After Milestone 2

Move to Milestone 3: PRD To Project Planning.

Recommended next issues:

- Dev C: #11 TXT/PDF PRD input
- Dev C: #12 PRD Extractor V2
- Dev C: #13 Project Plan Preview And Approval
- Dev B: #14 File Generation Order And Resume State

Do not start Django file generation until Milestone 3 plan preview and approval
are stable.
