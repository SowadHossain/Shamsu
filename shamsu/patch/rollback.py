"""
Rollback for the Patch/File Mutation Engine: restores every file touched by
a transaction back to its pre-mutation state, using the backups recorded in
that transaction's manifest.json. Never guesses - if a backup is missing for
a file the manifest says was touched, rollback stops and reports it instead
of silently leaving the workspace half-restored.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from shamsu.patch.transactions import TransactionWorkspace
from shamsu.safety.sandbox import Sandbox, SecurityError


def rollback_transaction(workspace_root: Path, transaction_id: str) -> tuple[bool, str]:
    workspace_root = Path(workspace_root).resolve()
    store = TransactionWorkspace(workspace_root)
    manifest = store.load_manifest(transaction_id)
    if manifest is None:
        return False, f"Unknown transaction: {transaction_id}"
    if manifest.get("status") == "rolled_back":
        return False, f"Transaction {transaction_id} was already rolled back."

    sandbox = Sandbox(workspace_root)
    transaction_dir = store.mutations_root / transaction_id
    before_hashes: dict = manifest.get("before_hashes", {})
    backups: dict = manifest.get("backups", {})
    restored: list[str] = []

    for relative_path in manifest.get("touched_files", []):
        try:
            target = sandbox.validate(relative_path)
        except SecurityError as exc:
            return False, f"Rollback aborted, unsafe path recorded in manifest: {exc}"

        existed_before = relative_path in before_hashes and before_hashes[relative_path] is not None
        if not existed_before:
            # This path did not exist before the transaction (a create, or the
            # destination of a rename/move) - rolling back means removing it.
            if target.is_file():
                target.unlink()
            elif target.is_dir():
                try:
                    target.rmdir()
                except OSError:
                    pass  # non-empty directory: leave it, nothing unsafe about that
            restored.append(relative_path)
            continue

        backup_rel = backups.get(relative_path)
        if not backup_rel:
            return False, f"Rollback aborted: no backup recorded for {relative_path}."
        backup_path = transaction_dir / backup_rel
        if not backup_path.exists():
            return False, f"Rollback aborted: backup file missing for {relative_path}."
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup_path, target)
        restored.append(relative_path)

    store.finalize(transaction_id, "rolled_back", error="")
    _queue_code_memory_refresh(workspace_root)
    return True, f"Restored {len(restored)} file(s) from transaction {transaction_id}."


def _queue_code_memory_refresh(workspace_root: Path) -> None:
    """Best-effort: never let code-memory bookkeeping break a rollback."""
    try:
        from shamsu.abstract.service import AbstractService

        AbstractService(workspace_root).queue_refresh()
    except Exception:
        pass
