"""Reliability metrics computed from state (plan §31).

The metric that matters most is `false_success_rate`, and the point of
computing it from rows is that it can be *wrong* about the runtime. v1
incremented a counter at the site that believed it had succeeded, so the number
measured whether the loop had noticed its own mistake — which reads zero
exactly when things are worst.

The test that earns its place here is the one that constructs a bypassed gate
directly in the database and checks the metric catches it.
"""

from __future__ import annotations

import pytest

from shamsu.interfaces.enums import AgentState, EvidenceKind, Phase, StepOutcome
from shamsu.interfaces.ids import (
    EvidenceId,
    PlanId,
    ProjectId,
    RunId,
    StepId,
    TaskId,
    ToolEventId,
)
from shamsu.state import (
    EvidenceRecord,
    PlanRecord,
    PlanStepRecord,
    ProjectRecord,
    RunRecord,
    StateStore,
    TaskRecord,
    ToolEventRecord,
    new_id,
)
from shamsu.telemetry import ReliabilityMetrics

FLOOR = (EvidenceKind.FILE_CHANGED, EvidenceKind.TESTS_PASSED)


@pytest.fixture
def store() -> StateStore:
    return StateStore(":memory:")


@pytest.fixture
def project(store: StateStore) -> ProjectRecord:
    return store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root="/workspace", name="demo")
    )


@pytest.fixture
def metrics(store: StateStore) -> ReliabilityMetrics:
    return ReliabilityMetrics(store)


def _task(
    store: StateStore,
    project: ProjectRecord,
    *,
    state: AgentState = AgentState.FINAL_REPORT,
    repairs: int = 0,
    replans: int = 0,
) -> TaskRecord:
    task = store.create_task(
        TaskRecord(task_id=TaskId(new_id()), project_id=project.project_id, request="do the thing")
    )
    return store.save_task(
        task.model_copy(update={"state": state, "repair_count": repairs, "replan_count": replans})
    )


def _plan(store: StateStore, task: TaskRecord, *, steps: int = 1) -> PlanId:
    plan_id = PlanId(new_id())
    store.create_plan(
        PlanRecord(plan_id=plan_id, task_id=task.task_id, version=1, summary="s"),
        [
            PlanStepRecord(
                step_id=StepId(new_id()),
                plan_id=plan_id,
                ordinal=index,
                title=f"step {index + 1}",
                required_evidence=FLOOR,
            )
            for index in range(steps)
        ],
    )
    store.save_task(task.model_copy(update={"plan_id": plan_id}))
    return plan_id


def _prove(
    store: StateStore,
    task: TaskRecord,
    plan_id: PlanId,
    *,
    tool: str = "file.patch",
    arguments: str = '{"path": "a.py"}',
    ok: bool = True,
    evidence: bool = True,
) -> None:
    run = store.create_run(
        RunRecord(run_id=RunId(new_id()), project_id=task.project_id, task_id=task.task_id)
    )
    for step in store.get_steps(plan_id):
        event = store.record_tool_event(
            ToolEventRecord(
                event_id=ToolEventId(new_id()),
                run_id=run.run_id,
                task_id=task.task_id,
                step_id=step.step_id,
                tool=tool,
                phase=Phase.AUTHOR,
                arguments_json=arguments,
                ok=ok,
                output="done",
            )
        )
        if evidence:
            for kind in FLOOR:
                store.record_evidence(
                    EvidenceRecord(
                        evidence_id=EvidenceId(new_id()),
                        task_id=task.task_id,
                        step_id=step.step_id,
                        kind=kind,
                        source_event_id=event.event_id,
                    )
                )
        store.save_step(step.model_copy(update={"outcome": StepOutcome.PASS}))


class TestIntegrity:
    def test_a_properly_completed_task_is_sound(
        self, store: StateStore, project: ProjectRecord, metrics: ReliabilityMetrics
    ) -> None:
        task = _task(store, project)
        _prove(store, task, _plan(store, task))

        report = metrics.report(project.project_id)
        assert report.verified_task_success_rate == 1.0
        assert report.false_success_rate == 0.0
        assert report.sound is True

    def test_a_bypassed_gate_is_caught(
        self, store: StateStore, project: ProjectRecord, metrics: ReliabilityMetrics
    ) -> None:
        """Written straight into the database, as a runtime defect would.

        `CompletionGate` cannot produce this state. If the metric can never
        report it, the metric is measuring the runtime's opinion of itself.
        """
        task = _task(store, project, state=AgentState.FINAL_REPORT)
        plan_id = _plan(store, task)
        for step in store.get_steps(plan_id):
            store.save_step(step.model_copy(update={"outcome": StepOutcome.PASS}))

        report = metrics.report(project.project_id)
        assert report.false_success_rate == 1.0
        assert report.integrity_violations == (task.task_id,)
        assert report.sound is False
        assert "INTEGRITY VIOLATION" in report.render()

    def test_a_task_marked_complete_with_an_unfinished_step_is_caught(
        self, store: StateStore, project: ProjectRecord, metrics: ReliabilityMetrics
    ) -> None:
        task = _task(store, project)
        plan_id = _plan(store, task, steps=2)
        _prove(store, task, plan_id)
        # Undo one step's outcome: evidence exists, the step never passed.
        first = store.get_steps(plan_id)[0]
        store.save_step(first.model_copy(update={"outcome": None}))

        assert metrics.report(project.project_id).sound is False

    def test_the_report_says_the_gate_held_when_it_did(
        self, store: StateStore, project: ProjectRecord, metrics: ReliabilityMetrics
    ) -> None:
        task = _task(store, project)
        _prove(store, task, _plan(store, task))
        assert "gate held" in metrics.report(project.project_id).render()


class TestRates:
    def test_an_unfinished_task_is_not_a_success_or_a_violation(
        self, store: StateStore, project: ProjectRecord, metrics: ReliabilityMetrics
    ) -> None:
        """Stopping honestly is neither."""
        task = _task(store, project, state=AgentState.BLOCKED)
        _plan(store, task)

        report = metrics.report(project.project_id)
        assert report.verified_task_success_rate == 0.0
        assert report.sound is True

    def test_first_pass_excludes_tasks_that_needed_repair(
        self, store: StateStore, project: ProjectRecord, metrics: ReliabilityMetrics
    ) -> None:
        clean = _task(store, project)
        _prove(store, clean, _plan(store, clean))

        repaired = _task(store, project, repairs=1)
        _prove(store, repaired, _plan(store, repaired))

        report = metrics.report(project.project_id)
        assert report.verified_task_success_rate == 1.0
        assert report.first_pass_verified_rate == 0.5

    def test_repair_success_is_measured_over_tasks_that_repaired(
        self, store: StateStore, project: ProjectRecord, metrics: ReliabilityMetrics
    ) -> None:
        succeeded = _task(store, project, repairs=1)
        _prove(store, succeeded, _plan(store, succeeded))

        failed = _task(store, project, state=AgentState.BLOCKED, repairs=2)
        _plan(store, failed)

        assert metrics.report(project.project_id).repair_success_rate == 0.5

    def test_consecutive_identical_calls_count_as_repeats(
        self, store: StateStore, project: ProjectRecord, metrics: ReliabilityMetrics
    ) -> None:
        """Reading a file twice in a row is the loop spinning."""
        task = _task(store, project)
        plan_id = _plan(store, task, steps=3)
        _prove(store, task, plan_id)  # three identical calls, same arguments

        assert metrics.report(project.project_id).repeated_action_rate > 0.0

    def test_distinct_calls_are_not_repeats(
        self, store: StateStore, project: ProjectRecord, metrics: ReliabilityMetrics
    ) -> None:
        task = _task(store, project)
        plan_id = _plan(store, task)
        _prove(store, task, plan_id)

        assert metrics.report(project.project_id).repeated_action_rate == 0.0

    def test_refused_calls_show_up_as_wrong_tool_use(
        self, store: StateStore, project: ProjectRecord, metrics: ReliabilityMetrics
    ) -> None:
        task = _task(store, project, state=AgentState.BLOCKED)
        plan_id = _plan(store, task)
        _prove(store, task, plan_id, ok=False, evidence=False)

        assert metrics.report(project.project_id).wrong_tool_rate == 1.0

    def test_an_empty_project_reports_nothing_rather_than_dividing_by_zero(
        self, project: ProjectRecord, metrics: ReliabilityMetrics
    ) -> None:
        report = metrics.report(project.project_id)
        assert report.tasks == 0
        assert report.verified_task_success_rate == 0.0
        assert report.render() == "No tasks measured."

    def test_metrics_are_scoped_to_one_project(
        self, store: StateStore, metrics: ReliabilityMetrics
    ) -> None:
        first = store.upsert_project(
            ProjectRecord(project_id=ProjectId(new_id()), root="/a", name="a")
        )
        second = store.upsert_project(
            ProjectRecord(project_id=ProjectId(new_id()), root="/b", name="b")
        )
        task = _task(store, first)
        _prove(store, task, _plan(store, task))

        assert metrics.report(first.project_id).tasks == 1
        assert metrics.report(second.project_id).tasks == 0
