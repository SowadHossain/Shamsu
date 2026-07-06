
You are working inside the SHAMSU repo.

Task:
Implement SHAMSU’s safe Patch/File Mutation Engine.

Goal:
The LLM should never directly overwrite, delete, rename, or create files blindly.

SHAMSU must use a deterministic mutation layer:

model proposes change
→ PatchEngine validates it
→ transaction snapshot is created
→ patch/file operation is applied safely
→ formatter runs if configured
→ verification command runs
→ diagnostics are parsed if verification fails
→ rollback is available
→ SHAMSU reports success only after verification passes

Use existing local tools first:

- Git for diff, patch validation, patch apply, rollback support
- unified diff as the main patch format
- git apply --check before git apply
- git apply --3way only as a controlled fallback
- diff-match-patch or similar only as optional high-confidence fuzzy fallback
- project formatters only if already configured
- existing CommandRunner for verification
- Codebase-Memory MCP for impact checks
- DiagnosticDigest for failed verification output

Do NOT:

- build a custom version control system
- let the model directly overwrite files
- permanently delete files first
- edit outside the workspace
- bypass sandbox/approvals
- bypass CommandRunner
- fake patch success
- fake build success
- claim success unless verification exits with code 0
- upload code anywhere
- use cloud services

Core features to implement:

1. PatchEngine
   Accept structured model output:

- change reason
- operations
- unified diff
- touched files
- verification command
- destructive flag

Validate the patch before applying:

- paths stay inside workspace
- no path traversal
- no symlink escape
- no editing .git internals
- no silent secret-file edits
- no destructive operation without approval
- patch applies cleanly with git apply --check

2. File operations
   Support:

- create_file
- edit_file
- apply_patch
- rename_file
- move_file
- delete_file
- create_directory

Rules:

- edits should prefer unified diff
- full-file rewrite only for small files or explicit approval
- create_file must not overwrite unless approved
- delete_file must move files to .shamsu/trash first
- rename/move/delete must use Codebase-Memory MCP impact checks

3. Transaction system
   Every mutation must create a transaction.

Store under:
<workspace></workspace>/.shamsu/mutations/<transaction-id></transaction>/

Each transaction should save:

- manifest.json
- before hashes
- after hashes
- backups of touched files
- applied patch
- verification result
- rollback metadata

Rollback must restore touched files safely.

4. Trash system
   Deletes should not be permanent.

Move deleted files to:
<workspace></workspace>/.shamsu/trash/<transaction-id></transaction>/

Add ability to list and clean trash with confirmation.

5. Verification
   After applying changes:

- run formatter on touched files when configured
- run verification command
- parse failure output with DiagnosticDigest
- save result in transaction
- compare repeated failures to avoid blind retry loops

Never say fixed/build passing unless verification exit code is 0.

6. Codebase-Memory MCP integration
   Before risky edits:

- get impact of touched files/symbols
- get imports/exports
- get who uses target files/symbols

Required for:

- delete
- rename
- move
- public export changes

After successful mutation:

- mark code memory stale
- refresh affected code memory/index

Do not fake code facts.

7. CLI commands
   Add:
   /patch status
   /patch preview
   /patch apply
   /patch rollback <transaction-id></transaction>
   /patch journal
   /patch last
   /patch diff <transaction-id></transaction>
   /patch trash
   /patch clean-trash
8. Model output contract
   Update coder/planner prompt so the model returns structured changes.

Preferred shape:

{
  "change_plan": {
    "reason": "Fix missing export without breaking importers",
    "operations": [
      {
        "op": "apply_patch",
        "path": "client/src/game/loop.ts",
        "reason": "Add compatibility alias export"
      }
    ],
    "verification_command": "npm run build",
    "destructive": false
  },
  "patch": "unified diff here"
}

The PatchEngine must validate this output. Never trust it blindly.

9. Tests
   Add tests for:

- path traversal blocked
- symlink escape blocked
- .git edits blocked
- secret-file edits require approval
- git apply --check runs before apply
- valid patch applies
- invalid patch rejects
- repeated invalid patch triggers stall guard
- create_file refuses overwrite
- delete requires approval
- delete moves to .shamsu/trash
- rename checks Codebase-Memory references
- transaction saves backups/hashes
- rollback restores files
- formatter only runs on touched files
- verification runs after mutation
- failed verification creates DiagnosticDigest ErrorPacket
- success only reported on exit code 0
- successful mutation refreshes Codebase-Memory
- failed mutation does not refresh
- /patch commands work

Suggested package structure:
shamsu/patching/
  patch_engine.py
  types.py
  file_mutations.py
  git_apply.py
  transactions.py
  rollback.py
  formatter.py
  verifier.py
  journal.py
  safety.py

Deliverables:

1. Inspect current SHAMSU file write/patch workflow.
2. Implement the smallest clean Patch/File Mutation Engine.
3. Integrate it with CommandRunner, Codebase-Memory MCP, and DiagnosticDigest.
4. Add transaction, rollback, trash, and journal support.
5. Add /patch commands.
6. Add tests.
7. Run targeted tests.
8. Summarize changes and remaining limitations.

Final rule:
All code/file changes must go through PatchEngine. The model proposes changes, but SHAMSU validates, applies, verifies, journals, and can roll back every mutation.
