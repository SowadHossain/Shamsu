"""Executor boundary for the live chat-loop implementation."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from shamsu.runtime.run_control import ControlledRun


@dataclass(frozen=True)
class StepExecutionLimits:
    max_actions_per_step: int = 6
    max_repairs_per_step: int = 2
    max_replans_per_task: int = 2
    max_consecutive_failures: int = 3


DEFAULT_STEP_EXECUTION_LIMITS = StepExecutionLimits()
LONG_RUNNING_STEP_EXECUTION_LIMITS = StepExecutionLimits(max_actions_per_step=12)


class StepExecutionDecision(str, Enum):
    CONTINUE = "continue"
    VERIFY = "verify"
    REPAIR = "repair"
    REPLAN = "replan"
    BLOCK = "block"


@dataclass(frozen=True)
class StepExecutionOutcome:
    decision: StepExecutionDecision
    reason: str = ""

    @property
    def should_stop(self) -> bool:
        return self.decision != StepExecutionDecision.CONTINUE


class StepExecutionController:
    def __init__(self, *, step_id: str = "", limits: StepExecutionLimits | None = None) -> None:
        self.step_id = step_id
        self.limits = limits or StepExecutionLimits()
        self.model_decisions = 0
        self.tool_actions = 0
        self.repairs = 0
        self.replans = 0
        self.consecutive_failures = 0
        self.progress_events = 0

    def before_model_decision(self) -> StepExecutionOutcome:
        if self.model_decisions >= self.limits.max_actions_per_step:
            return StepExecutionOutcome(
                StepExecutionDecision.BLOCK,
                f"step action budget exhausted ({self.limits.max_actions_per_step})",
            )
        self.model_decisions += 1
        return StepExecutionOutcome(StepExecutionDecision.CONTINUE)

    def note_success(self) -> StepExecutionOutcome:
        self.tool_actions += 1
        self.progress_events += 1
        self.consecutive_failures = 0
        return StepExecutionOutcome(StepExecutionDecision.VERIFY, "tool action succeeded")

    def note_failure(self, *, repairable: bool = False, replan_needed: bool = False) -> StepExecutionOutcome:
        self.tool_actions += 1
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.limits.max_consecutive_failures:
            return StepExecutionOutcome(
                StepExecutionDecision.BLOCK,
                f"consecutive failure budget exhausted ({self.limits.max_consecutive_failures})",
            )
        if repairable and self.repairs < self.limits.max_repairs_per_step:
            self.repairs += 1
            return StepExecutionOutcome(StepExecutionDecision.REPAIR, "failure is repairable")
        if replan_needed and self.replans < self.limits.max_replans_per_task:
            self.replans += 1
            return StepExecutionOutcome(StepExecutionDecision.REPLAN, "plan needs revision")
        return StepExecutionOutcome(StepExecutionDecision.CONTINUE)

    def note_no_progress(self, reason: str = "model produced no executable action") -> StepExecutionOutcome:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.limits.max_consecutive_failures:
            return StepExecutionOutcome(StepExecutionDecision.BLOCK, reason)
        return StepExecutionOutcome(StepExecutionDecision.CONTINUE, reason)

    def enforce_single_action(self, actions: list[Any]) -> tuple[list[Any], StepExecutionOutcome]:
        if len(actions) <= 1:
            return actions, StepExecutionOutcome(StepExecutionDecision.CONTINUE)
        self.consecutive_failures += 1
        return actions[:1], StepExecutionOutcome(
            StepExecutionDecision.CONTINUE,
            "model returned multiple actions; only the first action was accepted",
        )


class AgentExecutor:
    def __init__(
        self,
        execute_loop: Callable[[str, ControlledRun], Awaitable[Any]],
        step_runner: Callable[[Any, str, ControlledRun, StepExecutionLimits], Awaitable[Any]] | None = None,
        limits: StepExecutionLimits | None = None,
    ) -> None:
        self._execute_loop = execute_loop
        self._step_runner = step_runner
        self.limits = limits or StepExecutionLimits()

    async def execute(self, user_input: str, control: ControlledRun) -> Any:
        return await self._execute_loop(user_input, control)

    async def run_step(self, step: Any, user_input: str, control: ControlledRun) -> Any:
        if self._step_runner is None:
            return await self._execute_loop(user_input, control)
        return await self._step_runner(step, user_input, control, self.limits)
