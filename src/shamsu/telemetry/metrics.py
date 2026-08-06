"""Reliability metrics, computed from state (plan §31).

Migrated in intent from `legacy-code/shamsu/telemetry/reliability.py` (788
lines), but the numbers are computed differently and that is the whole point.
v1 counted what the loop *told* it: a counter incremented at the site that
believed it had succeeded. So `false_success_rate` was, structurally, the rate
at which the loop noticed it had been wrong — which is not the same quantity
and is exactly the one that reads zero when things are worst.

Here every metric is a query over `tasks`, `evidence`, `tool_events`, and
`failures`. Nothing is incremented by the component being measured.

The headline metric is the one that tests the architecture rather than the
agent:

    false_success_rate — tasks reported complete whose required evidence was
    never verified.

In v2 this should be **structurally zero**, because `CompletionGate` is the
only path to a completed task and it reads the evidence table. A non-zero
reading is not a quality problem to be tuned; it means the gate has been
bypassed, and it should be treated as a defect in the runtime.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from shamsu.interfaces.enums import AgentState, EvidenceKind, StepOutcome
from shamsu.interfaces.ids import ProjectId, TaskId
from shamsu.state.records import TaskRecord
from shamsu.state.store import StateStore


def _rate(numerator: int, denominator: int) -> float:
    """A rate, or 0.0 when there is nothing to divide.

    Zero rather than None: every consumer would otherwise need the same
    special case, and "no tasks yet" is not a quality signal worth propagating
    through a report as a null.
    """
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True)
class ReliabilityReport:
    """Plan §31's metric set, measured over a set of tasks."""

    tasks: int = 0

    verified_task_success_rate: float = 0.0
    false_success_rate: float = 0.0
    success_without_verification_rate: float = 0.0
    first_pass_verified_rate: float = 0.0
    repair_success_rate: float = 0.0
    repeated_action_rate: float = 0.0
    wrong_tool_rate: float = 0.0
    rollback_rate: float = 0.0

    #: Set when `false_success_rate` is non-zero. Named separately because it
    #: is not a quality reading -- it means the completion gate was bypassed.
    integrity_violations: tuple[TaskId, ...] = ()

    @property
    def sound(self) -> bool:
        """Whether the evidence architecture held over this sample."""
        return not self.integrity_violations

    def render(self) -> str:
        if not self.tasks:
            return "No tasks measured."

        lines = [
            f"Tasks measured: {self.tasks}",
            f"  verified success        {self.verified_task_success_rate:.0%}",
            f"  first pass verified     {self.first_pass_verified_rate:.0%}",
            f"  repair success          {self.repair_success_rate:.0%}",
            f"  repeated actions        {self.repeated_action_rate:.0%}",
            f"  wrong tool              {self.wrong_tool_rate:.0%}",
            f"  rollbacks               {self.rollback_rate:.0%}",
        ]

        if self.integrity_violations:
            lines.append("")
            lines.append(
                f"INTEGRITY VIOLATION: {len(self.integrity_violations)} task(s) reported "
                "complete without verified evidence. The completion gate was bypassed; "
                "this is a runtime defect, not a quality metric."
            )
        else:
            lines.append("  false success           0% (gate held)")

        return "\n".join(lines)


class ReliabilityMetrics:
    """Computes plan §31's metrics from the state store."""

    def __init__(self, store: StateStore) -> None:
        self._store = store

    def report(self, project_id: ProjectId) -> ReliabilityReport:
        """Measure every task recorded for a project."""
        return self.report_for(self._tasks(project_id))

    def report_for(self, tasks: Sequence[TaskRecord]) -> ReliabilityReport:
        """Measure a specific set of tasks.

        Takes records rather than ids so an evaluation harness can measure one
        suite without the store having to know what a suite is.
        """
        if not tasks:
            return ReliabilityReport()

        verified = 0
        first_pass = 0
        repaired = 0
        repair_attempted = 0
        rolled_back = 0
        violations: list[TaskId] = []

        total_actions = 0
        repeated_actions = 0
        refused_calls = 0

        for task in tasks:
            complete = task.state is AgentState.FINAL_REPORT
            proven = self._evidence_holds(task)

            if complete and proven:
                verified += 1
                if task.repair_count == 0 and task.replan_count == 0:
                    first_pass += 1
            elif complete and not proven:
                # Reported complete with the gate unsatisfied. Structurally
                # impossible through `CompletionGate`; counted anyway, because
                # a check that can only ever pass measures nothing.
                violations.append(task.task_id)

            if task.repair_count > 0:
                repair_attempted += 1
                if complete and proven:
                    repaired += 1

            events = self._store.tool_events_for(task.task_id)
            total_actions += len(events)
            repeated_actions += _repeats(events)
            refused_calls += sum(1 for event in events if not event.ok)

            if any(event.tool == "git.checkpoint" for event in events) and not complete:
                rolled_back += 1

        return ReliabilityReport(
            tasks=len(tasks),
            verified_task_success_rate=_rate(verified, len(tasks)),
            false_success_rate=_rate(len(violations), len(tasks)),
            success_without_verification_rate=_rate(len(violations), len(tasks)),
            first_pass_verified_rate=_rate(first_pass, len(tasks)),
            repair_success_rate=_rate(repaired, repair_attempted),
            repeated_action_rate=_rate(repeated_actions, total_actions),
            wrong_tool_rate=_rate(refused_calls, total_actions),
            rollback_rate=_rate(rolled_back, len(tasks)),
            integrity_violations=tuple(violations),
        )

    # -- internals ---------------------------------------------------------

    def _tasks(self, project_id: ProjectId) -> Sequence[TaskRecord]:
        with self._store.reading() as connection:
            rows = connection.execute(
                "SELECT task_id FROM tasks WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
        found = [self._store.get_task(TaskId(row["task_id"])) for row in rows]
        return [task for task in found if task is not None]

    def _evidence_holds(self, task: TaskRecord) -> bool:
        """Whether every step of the task's plan has its required evidence.

        Re-derived from rows rather than read from a stored verdict. A stored
        verdict measures what the runtime concluded; this measures whether the
        conclusion was earned.
        """
        if task.plan_id is None:
            # A DIRECT task has no plan. It is verified when it registered any
            # evidence at all -- there is no plan to require more than that.
            return bool(self._store.verified_evidence(task.task_id))

        steps = self._store.get_steps(task.plan_id)
        if not steps:
            return False

        for step in steps:
            if step.outcome is not StepOutcome.PASS:
                return False
            required: frozenset[EvidenceKind] = frozenset(step.required_evidence)
            if not required <= self._store.verified_evidence(task.task_id, step.step_id):
                return False
        return True


def _repeats(events: Sequence[object]) -> int:
    """Consecutive identical tool calls.

    Consecutive rather than total: reading the same file at the start and end
    of a task is ordinary, and reading it twice in a row is the loop spinning.
    v1 counted total repeats and the metric was dominated by the first case.
    """
    seen: list[tuple[str, str]] = []
    repeats = 0
    for event in events:
        tool = str(getattr(event, "tool", ""))
        arguments = str(getattr(event, "arguments_json", ""))
        key = (tool, arguments)
        if seen and seen[-1] == key:
            repeats += 1
        seen.append(key)
    return repeats


__all__ = ["ReliabilityMetrics", "ReliabilityReport"]
