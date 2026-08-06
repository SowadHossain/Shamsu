"""
Transaction system for the Patch/File Mutation Engine.

Every mutation SHAMSU applies happens inside a transaction stored under
`.shamsu/mutations/<transaction-id>/`:

  manifest.json   - reason, operations, touched files, before/after hashes,
                     verification result, status, rollback metadata
  patch.diff       - the unified diff that was applied (if any)
  backups/<path>   - a copy of each touched file exactly as it was before
                     the mutation, so rollback can restore it byte-for-byte

This is deliberately not a version control system: no history graph, no
commits, no refs - just enough deterministic bookkeeping to answer "what
changed, what did it look like before, and can I safely put it back."
"""
from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shamsu.patch.journal import MutationJournal
from shamsu.safety.sandbox import Sandbox

MANIFEST_NAME = "manifest.json"
PATCH_NAME = "patch.diff"
BACKUPS_DIRNAME = "backups"


def _hash_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def new_transaction_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


class TransactionWorkspace:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.mutations_root = self.workspace_root / ".shamsu" / "mutations"
        self.journal = MutationJournal(self.workspace_root)

    def _dir(self, transaction_id: str) -> Path:
        return self.mutations_root / transaction_id

    def _manifest_path(self, transaction_id: str) -> Path:
        return self._dir(transaction_id) / MANIFEST_NAME

    def _patch_path(self, transaction_id: str) -> Path:
        return self._dir(transaction_id) / PATCH_NAME

    def _backup_path(self, transaction_id: str, relative_path: str) -> Path:
        return self._dir(transaction_id) / BACKUPS_DIRNAME / relative_path

    # -- lifecycle ------------------------------------------------------------

    def begin(self, reason: str, operations: list[dict[str, Any]], destructive: bool) -> str:
        transaction_id = new_transaction_id()
        self._dir(transaction_id).mkdir(parents=True, exist_ok=True)
        manifest = {
            "transaction_id": transaction_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "operations": operations,
            "destructive": destructive,
            "status": "pending",
            "touched_files": [],
            "before_hashes": {},
            "after_hashes": {},
            "backups": {},
            "verification": None,
            "error": "",
        }
        self._write_manifest(transaction_id, manifest)
        return transaction_id

    def backup_file(self, transaction_id: str, relative_path: str) -> None:
        """Snapshot a file's current state before it is mutated. Safe to call
        more than once for the same path within a transaction (idempotent)."""
        manifest = self.load_manifest(transaction_id)
        if manifest is None:
            raise ValueError(f"Unknown transaction: {transaction_id}")
        if relative_path in manifest["before_hashes"]:
            return
        target = self.sandbox.validate(relative_path)
        manifest["before_hashes"][relative_path] = _hash_file(target)
        if target.is_file():
            backup = self._backup_path(transaction_id, relative_path)
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            manifest["backups"][relative_path] = str(Path(BACKUPS_DIRNAME) / relative_path)
        if relative_path not in manifest["touched_files"]:
            manifest["touched_files"].append(relative_path)
        self._write_manifest(transaction_id, manifest)

    def record_after(self, transaction_id: str, relative_path: str) -> None:
        manifest = self.load_manifest(transaction_id)
        if manifest is None:
            raise ValueError(f"Unknown transaction: {transaction_id}")
        try:
            target = self.sandbox.validate(relative_path)
            manifest["after_hashes"][relative_path] = _hash_file(target)
        except Exception:
            manifest["after_hashes"][relative_path] = None
        if relative_path not in manifest["touched_files"]:
            manifest["touched_files"].append(relative_path)
        self._write_manifest(transaction_id, manifest)

    def save_patch(self, transaction_id: str, patch_text: str) -> None:
        if not patch_text:
            return
        self._patch_path(transaction_id).write_text(patch_text, encoding="utf-8")

    def load_patch(self, transaction_id: str) -> str:
        path = self._patch_path(transaction_id)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def save_verification(self, transaction_id: str, verification: dict[str, Any] | None) -> None:
        manifest = self.load_manifest(transaction_id)
        if manifest is None:
            raise ValueError(f"Unknown transaction: {transaction_id}")
        manifest["verification"] = verification
        self._write_manifest(transaction_id, manifest)

    def finalize(self, transaction_id: str, status: str, error: str = "") -> dict[str, Any]:
        manifest = self.load_manifest(transaction_id)
        if manifest is None:
            raise ValueError(f"Unknown transaction: {transaction_id}")
        manifest["status"] = status
        manifest["error"] = error
        self._write_manifest(transaction_id, manifest)
        self.journal.append(
            {
                "transaction_id": transaction_id,
                "created_at": manifest["created_at"],
                "reason": manifest["reason"],
                "status": status,
                "error": error,
                "touched_files": manifest["touched_files"],
                "destructive": manifest["destructive"],
                "verification": manifest.get("verification"),
            }
        )
        return manifest

    # -- reads ------------------------------------------------------------------

    def load_manifest(self, transaction_id: str) -> dict[str, Any] | None:
        path = self._manifest_path(transaction_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def list_transaction_ids(self) -> list[str]:
        if not self.mutations_root.is_dir():
            return []
        return sorted(
            child.name
            for child in self.mutations_root.iterdir()
            if child.is_dir() and (child / MANIFEST_NAME).exists()
        )

    def _write_manifest(self, transaction_id: str, manifest: dict[str, Any]) -> None:
        self._manifest_path(transaction_id).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
