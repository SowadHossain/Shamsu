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


def latest_undoable_transaction(workspace_root: Path) -> tuple[str, dict] | None:
    """The most recent transaction that can still be rolled back, or None.

    Every model-driven write already opens its own transaction (backup + hash),
    so an undo path existed all along - it just required knowing that
    `/patch rollback` exists AND digging the right id out of `.shamsu/mutations`.
    Nobody does that in the moment their code just got mangled (gap G2). This
    resolves "the last change" so `/undo` can.

    Ordering uses the manifest's `created_at` (microsecond ISO), NOT the id.
    Ids only carry `%Y%m%dT%H%M%S` - second resolution - so two writes in the
    same second sort by their random uuid suffix, which would make `/undo`
    revert an arbitrary one of them. Agent writes are routinely sub-second
    apart, so that is the common case, not an edge case.
    """
    store = TransactionWorkspace(Path(workspace_root).resolve())
    candidates: list[tuple[str, str, dict]] = []
    for transaction_id in store.list_transaction_ids():
        manifest = store.load_manifest(transaction_id)
        if manifest is None or manifest.get("status") == "rolled_back":
            continue
        candidates.append((str(manifest.get("created_at", "")), transaction_id, manifest))
    if not candidates:
        return None
    # created_at first; the id breaks exact ties deterministically.
    created_at, transaction_id, manifest = max(candidates, key=lambda item: (item[0], item[1]))
    return transaction_id, manifest


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
