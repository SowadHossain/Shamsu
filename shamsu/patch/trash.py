"""
Trash system: deletes performed through the Patch/File Mutation Engine are
never permanent. Deleted files move to `.shamsu/trash/<transaction-id>/`,
preserving their workspace-relative path, and can be listed or purged (with
confirmation handled by the caller) but are never removed on delete itself.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from shamsu.safety.sandbox import Sandbox


@dataclass(frozen=True)
class TrashEntry:
    transaction_id: str
    relative_path: str
    trashed_path: str
    size_bytes: int
    trashed_at: float


class TrashWorkspace:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.trash_root = self.workspace_root / ".shamsu" / "trash"

    def move_to_trash(self, target: Path, transaction_id: str, relative_path: str) -> Path:
        """Move an existing file/dir out of the workspace tree and into
        .shamsu/trash/<transaction-id>/<relative_path>, preserving structure
        so rollback can put it back exactly where it was."""
        destination = self.sandbox.validate(Path(".shamsu") / "trash" / transaction_id / relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(destination))
        return destination

    def list_entries(self) -> list[TrashEntry]:
        if not self.trash_root.is_dir():
            return []
        entries: list[TrashEntry] = []
        for transaction_dir in sorted(self.trash_root.iterdir()):
            if not transaction_dir.is_dir():
                continue
            for path in transaction_dir.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(transaction_dir).as_posix()
                stat = path.stat()
                entries.append(
                    TrashEntry(
                        transaction_id=transaction_dir.name,
                        relative_path=relative,
                        trashed_path=path.relative_to(self.workspace_root).as_posix(),
                        size_bytes=stat.st_size,
                        trashed_at=stat.st_mtime,
                    )
                )
        return entries

    def restore(self, transaction_id: str, relative_path: str, destination: Path) -> bool:
        source = self.trash_root / transaction_id / relative_path
        if not source.exists():
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return True

    def clean(self) -> int:
        """Permanently delete everything currently in trash. Caller is
        responsible for obtaining user confirmation first - this function
        itself never asks."""
        if not self.trash_root.is_dir():
            return 0
        count = len(self.list_entries())
        shutil.rmtree(self.trash_root)
        return count
