"""Repair-state helpers for runtime agent execution."""
from __future__ import annotations

from shamsu.runtime.task_state import RuntimeStateStore


class RepairRecorder:
    def __init__(self, store: RuntimeStateStore, task_id: str) -> None:
        self.store = store
        self.task_id = task_id

    def record_attempt(self, target_files: list[str]) -> None:
        self.store.record_repair_attempt(
            self.task_id,
            target_files=list(target_files),
        )
