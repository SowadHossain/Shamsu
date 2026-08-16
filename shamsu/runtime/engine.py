"""Production runtime engine for AgentChatLoop compatibility."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Protocol

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.runtime.failures import FailureType
from shamsu.runtime.run_control import ControlledRun
from shamsu.runtime.run_controller import RunController
from shamsu.runtime.task_state import RuntimeStateStore, bind_task_state
from shamsu.runtime.transitions import apply_completion_gate_failure, status_from_result
from shamsu.session.manager import SessionLogger
from shamsu.types import RunStatus
from shamsu.verification.completion import CompletionCoordinator


class RuntimeAgent(Protocol):
    run_id: str
    runtime_task_id: str
    session_logger: SessionLogger | None
    action_ledger: ActionLedger | None
    max_runtime_seconds: float | None
    runtime_state_store: RuntimeStateStore
    executor: Any

    def _initialize_runtime_task(self, user_input: str, control: ControlledRun) -> Any:
        ...

    def _checkpoint_task_status(
        self,
        status: RunStatus,
        phase: str,
        checkpoint_kind: str,
    ) -> None:
        ...

    def _make_terminal_result(self, final: str, status: RunStatus, *, stopped: bool = True) -> Any:
        ...


class RuntimeEngine:
    def __init__(self, agent: RuntimeAgent) -> None:
        self.agent = agent
        self.controller = RunController(
            run_id=agent.run_id,
            session_logger=agent.session_logger,
            action_ledger=agent.action_ledger,
            max_runtime_seconds=agent.max_runtime_seconds,
        )
        self.completion = CompletionCoordinator(
            agent.runtime_state_store,
            agent.runtime_task_id,
        )

    async def run(self, user_input: str) -> Any:
        control = self.controller.start()
        with self.controller.bound(control):
            try:
                await asyncio.sleep(0)
                self.agent._initialize_runtime_task(user_input, control)
                with bind_task_state(
                    self.agent.runtime_state_store,
                    self.agent.runtime_task_id,
                ):
                    result = await self.agent.executor.execute(user_input, control)
            except asyncio.CancelledError:
                final = "Run cancelled."
                status = RunStatus.CANCELLED if control.cancel_event.is_set() else RunStatus.FAILED
                self.agent._checkpoint_task_status(status, status.value, "cancelled")
                self.controller.complete(status, final)
                return self.agent._make_terminal_result(final, status)
            except Exception as exc:
                self.agent._checkpoint_task_status(RunStatus.FAILED, "failed", "failed")
                self.controller.complete(RunStatus.FAILED, f"{type(exc).__name__}: {exc}")
                raise

        status = status_from_result(result)
        if status == RunStatus.COMPLETED:
            gate = self.completion.request_completion()
            if gate is None or not gate.ok:
                evidence: list[str] = []
                if gate is not None:
                    evidence.extend(gate.missing_evidence)
                    evidence.extend(gate.incomplete_steps)
                    evidence.extend(gate.failed_evidence)
                    evidence.extend(gate.stale_evidence)
                try:
                    self.agent.runtime_state_store.create_failure(
                        self.agent.runtime_task_id,
                        FailureType.PREMATURE_COMPLETION,
                        action="TASK_COMPLETE",
                        evidence=evidence,
                        detail=gate.message if gate is not None else "completion gate unavailable",
                    )
                except Exception:
                    pass
                status = RunStatus.FAILED
                result = apply_completion_gate_failure(result, gate)
                self.agent._checkpoint_task_status(status, "failed", "completion_gate_failed")
        else:
            self.agent._checkpoint_task_status(status, status.value, status.value)

        self.controller.complete(status, result.final[:500])
        return replace(
            result,
            run_id=self.agent.run_id,
            task_id=self.agent.runtime_task_id,
            status=status,
        )
