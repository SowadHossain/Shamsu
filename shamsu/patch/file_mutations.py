"""
Non-diff file operations for the Patch/File Mutation Engine: create_file,
edit_file (full rewrite), rename_file, move_file, delete_file, and
create_directory. `apply_patch`/diff-based edits stay in shamsu/patch/engine.py,
reusing its existing tested unified-diff parser/applier instead of a second
implementation here.

Every method here assumes the caller (PatchEngine) already has an open
transaction and has obtained approval - this module only enforces path
safety (Sandbox + .git-internals) and does the actual disk operation plus
transaction backup/hash bookkeeping.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from shamsu.abstract.context import _names as _names_from
from shamsu.patch.safety import MutationSafetyError, validate_mutation_path
from shamsu.patch.trash import TrashWorkspace
from shamsu.patch.transactions import TransactionWorkspace
from shamsu.safety.sandbox import Sandbox


@dataclass(frozen=True)
class MutationOutcome:
    ok: bool
    op: str
    path: str
    dest_path: str = ""
    error: str = ""
    codebase_memory_note: str = ""


def build_impact_note(workspace_root: Path, path: str, memory_adapter) -> str:
    """Best-effort Codebase-Memory MCP impact check for delete/rename/move.
    Never fakes a fact: if the adapter is unavailable this says so instead
    of fabricating a "no references" claim."""
    if memory_adapter is None:
        return f"Codebase-Memory MCP not configured; impact of touching {path} was not checked."
    health = memory_adapter.healthcheck(workspace_root)
    if not health.ok:
        return f"Codebase-Memory MCP unavailable, impact of touching {path} could not be checked: {health.message}"
    references = memory_adapter.get_references(workspace_root, path)
    names = _names_from(references) if references.get("ok") else []
    if names:
        return f"{path} is referenced by: {', '.join(names[:12])}"
    return f"Codebase-Memory MCP found no references to {path}."


class FileMutationOps:
    def __init__(
        self,
        workspace_root: Path,
        transactions: TransactionWorkspace,
        trash: TrashWorkspace | None = None,
        memory_adapter=None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.transactions = transactions
        self.trash = trash or TrashWorkspace(self.workspace_root)
        self.memory_adapter = memory_adapter

    def impact_note(self, path: str) -> str:
        return build_impact_note(self.workspace_root, path, self.memory_adapter)

    def create_file(self, transaction_id: str, relative_path: str, content: str) -> MutationOutcome:
        try:
            target = validate_mutation_path(self.sandbox, relative_path)
        except MutationSafetyError as exc:
            return MutationOutcome(False, "create_file", relative_path, error=str(exc))
        if target.exists():
            return MutationOutcome(
                False, "create_file", relative_path,
                error=f"Refusing to overwrite existing file (use edit_file/apply_patch): {relative_path}",
            )
        self.transactions.backup_file(transaction_id, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.transactions.record_after(transaction_id, relative_path)
        return MutationOutcome(True, "create_file", relative_path)

    def edit_file(self, transaction_id: str, relative_path: str, new_content: str) -> MutationOutcome:
        try:
            target = validate_mutation_path(self.sandbox, relative_path)
        except MutationSafetyError as exc:
            return MutationOutcome(False, "edit_file", relative_path, error=str(exc))
        if not target.is_file():
            return MutationOutcome(False, "edit_file", relative_path, error=f"Cannot edit missing file: {relative_path}")
        self.transactions.backup_file(transaction_id, relative_path)
        target.write_text(new_content, encoding="utf-8")
        self.transactions.record_after(transaction_id, relative_path)
        return MutationOutcome(True, "edit_file", relative_path)

    def create_directory(self, transaction_id: str, relative_path: str) -> MutationOutcome:
        try:
            target = validate_mutation_path(self.sandbox, relative_path)
        except MutationSafetyError as exc:
            return MutationOutcome(False, "create_directory", relative_path, error=str(exc))
        self.transactions.backup_file(transaction_id, relative_path)
        target.mkdir(parents=True, exist_ok=True)
        self.transactions.record_after(transaction_id, relative_path)
        return MutationOutcome(True, "create_directory", relative_path)

    def delete_file(self, transaction_id: str, relative_path: str) -> MutationOutcome:
        try:
            target = validate_mutation_path(self.sandbox, relative_path)
        except MutationSafetyError as exc:
            return MutationOutcome(False, "delete_file", relative_path, error=str(exc))
        if not target.exists():
            return MutationOutcome(False, "delete_file", relative_path, error=f"Cannot delete missing path: {relative_path}")
        note = self.impact_note(relative_path)
        self.transactions.backup_file(transaction_id, relative_path)
        self.trash.move_to_trash(target, transaction_id, relative_path)
        self.transactions.record_after(transaction_id, relative_path)
        return MutationOutcome(True, "delete_file", relative_path, codebase_memory_note=note)

    def rename_file(self, transaction_id: str, relative_path: str, dest_path: str) -> MutationOutcome:
        return self._relocate(transaction_id, "rename_file", relative_path, dest_path)

    def move_file(self, transaction_id: str, relative_path: str, dest_path: str) -> MutationOutcome:
        return self._relocate(transaction_id, "move_file", relative_path, dest_path)

    def _relocate(self, transaction_id: str, op: str, relative_path: str, dest_path: str) -> MutationOutcome:
        try:
            source = validate_mutation_path(self.sandbox, relative_path)
            destination = validate_mutation_path(self.sandbox, dest_path)
        except MutationSafetyError as exc:
            return MutationOutcome(False, op, relative_path, dest_path, error=str(exc))
        if not source.exists():
            return MutationOutcome(False, op, relative_path, dest_path, error=f"Source does not exist: {relative_path}")
        if destination.exists():
            return MutationOutcome(False, op, relative_path, dest_path, error=f"Refusing to overwrite existing destination: {dest_path}")
        note = self.impact_note(relative_path)
        self.transactions.backup_file(transaction_id, relative_path)
        self.transactions.backup_file(transaction_id, dest_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        self.transactions.record_after(transaction_id, relative_path)
        self.transactions.record_after(transaction_id, dest_path)
        return MutationOutcome(True, op, relative_path, dest_path, codebase_memory_note=note)
