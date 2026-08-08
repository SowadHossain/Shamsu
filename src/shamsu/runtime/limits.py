"""Execution limits, in one place.

v1's bounds were spread across nine files -- chat rounds in one, repair attempts
in another, error-feedback iterations in a third, same-error retries in a
fourth. Nobody could answer "how much work can this task cause?" without
reading all of them, and long-running mode multiplied several at once with no
progress-based stopping criterion.

Here they are one frozen object. The values are plan section 11's initial
table.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class LimitExceeded(Exception):
    """A bound was reached.

    Carries which one, because "the agent stopped" is not an actionable report
    and v1's exhaustion paths were frequently indistinguishable from failure.
    """

    def __init__(self, limit: str, value: int | float) -> None:
        super().__init__(f"limit reached: {limit} = {value}")
        self.limit = limit
        self.value = value


class ExecutionLimits(BaseModel):
    """Bounds on a single task.

    Defaults are intentionally tight. Plan section 34: reliability matters more
    than autonomy, and higher ceilings must be earned by evaluation results
    rather than assumed.
    """

    model_config = ConfigDict(frozen=True)

    #: Raised from 4 on measurement, which is what plan §34 asks for.
    #:
    #: The commonest step shape requires `file_changed`, `git_diff_reviewed`
    #: and `tests_passed`. That is four calls before anything goes wrong —
    #: locate, patch, review the diff, run the tests — so a budget of four left
    #: no room to find the code first, and none at all for a mistake. A live
    #: run against qwen2.5-coder:14b spent its whole budget on a search, one
    #: patch rejected for a mistyped argument, the real patch, and the diff
    #: review, then stopped one call short of the tests it needed.
    #:
    #: Eight is twice that minimum: enough to investigate, edit, verify, and
    #: repair once. Still a bound, and still far below "let it run".
    actions_per_step: int = Field(default=8, ge=1)
    repair_attempts_per_step: int = Field(default=2, ge=0)
    replans_per_task: int = Field(default=2, ge=0)
    consecutive_failed_actions: int = Field(default=3, ge=1)

    mutating_calls_per_decision: int = Field(
        default=1,
        ge=1,
        description=(
            "At most one side effect per model decision, so a single "
            "misunderstood instruction cannot cascade."
        ),
    )
    logical_actions_per_turn: int = Field(default=1, ge=1)

    wall_clock_seconds: float = Field(default=1800.0, gt=0)

    long_running_enabled: bool = Field(
        default=False,
        description="Disabled until evaluations justify it (plan section 34.13).",
    )
    automatic_production_actions: bool = Field(
        default=False,
        description="Never enabled by default. Production access is opt-in per run.",
    )

    def check_actions(self, count: int) -> None:
        if count >= self.actions_per_step:
            raise LimitExceeded("actions_per_step", self.actions_per_step)

    def check_repairs(self, count: int) -> None:
        if count >= self.repair_attempts_per_step:
            raise LimitExceeded("repair_attempts_per_step", self.repair_attempts_per_step)

    def check_replans(self, count: int) -> None:
        if count >= self.replans_per_task:
            raise LimitExceeded("replans_per_task", self.replans_per_task)

    def check_consecutive_failures(self, count: int) -> None:
        if count >= self.consecutive_failed_actions:
            raise LimitExceeded("consecutive_failed_actions", self.consecutive_failed_actions)

    def exhausted(self, *, actions: int = 0, repairs: int = 0, replans: int = 0) -> bool:
        """Whether any bound is spent, without raising.

        For deciding whether to attempt more work, as opposed to reporting why
        work stopped.
        """
        return (
            actions >= self.actions_per_step
            or repairs >= self.repair_attempts_per_step
            or replans >= self.replans_per_task
        )


DEFAULT_LIMITS = ExecutionLimits()

__all__ = ["DEFAULT_LIMITS", "ExecutionLimits", "LimitExceeded"]
