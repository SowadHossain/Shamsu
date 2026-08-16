"""Completion gate coordinator for persistent runtime tasks."""
from __future__ import annotations

from shamsu.runtime.task_state import CompletionGateResult, RuntimeStateStore


class CompletionCoordinator:
    def __init__(self, store: RuntimeStateStore, task_id: str) -> None:
        self.store = store
        self.task_id = task_id

    def request_completion(self) -> CompletionGateResult | None:
        active = self.store.current_active_step(self.task_id)
        if active is not None:
            step_gate = self.store.complete_plan_step(self.task_id, active.step_id)
            if not step_gate.ok:
                return step_gate
        return self.store.request_task_complete(self.task_id)
