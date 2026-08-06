"""
Append-only journal of Patch/File Mutation Engine transactions, at
`.shamsu/mutations/journal.jsonl`. Backs `/patch journal`, `/patch last`,
and `/patch diff <transaction-id>` without needing to reload every
transaction's manifest.json from disk.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class MutationJournal:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.mutations_dir = self.workspace_root / ".shamsu" / "mutations"
        self.journal_path = self.mutations_dir / "journal.jsonl"

    def append(self, entry: dict[str, Any]) -> None:
        self.mutations_dir.mkdir(parents=True, exist_ok=True)
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        results: list[dict[str, Any]] = []
        for line in self.journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                results.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return results

    def last(self) -> dict[str, Any] | None:
        entries = self.entries()
        return entries[-1] if entries else None

    def get(self, transaction_id: str) -> dict[str, Any] | None:
        """Latest journal entry for a transaction id (last write wins, e.g.
        a rollback entry appended after the original apply/fail entry)."""
        match: dict[str, Any] | None = None
        for entry in self.entries():
            if entry.get("transaction_id") == transaction_id:
                match = entry
        return match
