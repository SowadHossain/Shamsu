from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.runtime.task_state import (
    EvidenceRecord,
    EvidenceType,
    ExecutionPlan,
    InvalidStateTransition,
    PlanStep,
    PlanStepStatus,
    RiskLevel,
    RuntimeStateStore,
    validate_execution_plan,
)
from shamsu.types import RunStatus, TaskStepStatus


def _running_task(store: RuntimeStateStore, task_id: str = "task-plan"):
    task = store.create_task(run_id="run-plan", task_id=task_id, user_request="do it")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    return task


def _plan(task_id: str = "task-plan") -> ExecutionPlan:
    return ExecutionPlan(
        plan_id="plan-a",
        task_id=task_id,
        run_id="run-plan",
        title="Implement task",
        summary="Read, edit, verify.",
        steps=[
            PlanStep(
                step_id="read",
                title="Read target",
                goal="Inspect the file",
                allowed_tools=["read_file"],
                acceptance_criteria=["The target file has been inspected."],
                risk_level=RiskLevel.LOW,
            ),
            PlanStep(
                step_id="edit",
                title="Edit target",
                goal="Apply the requested change",
                allowed_tools=["edit_file"],
                acceptance_criteria=["The requested change is present."],
                required_evidence=[EvidenceType.FILE_CHANGED.value],
                risk_level=RiskLevel.MEDIUM,
                approval_required=True,
                dependencies=["read"],
            ),
        ],
    )


def test_execution_plan_persists_and_reloads(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    _running_task(store)

    saved = store.save_execution_plan(_plan(), valid_tool_names={"read_file", "edit_file"})
    reloaded = RuntimeStateStore(tmp_path).load_execution_plan(saved.plan_id)

    assert reloaded is not None
    assert reloaded.plan_id == "plan-a"
    assert [step.step_id for step in reloaded.steps] == ["read", "edit"]
    assert reloaded.steps[1].required_evidence == [EvidenceType.FILE_CHANGED.value]
    assert store.require_task("task-plan").plan_id == "plan-a"


def test_plan_validation_requires_acceptance_criteria():
    plan = _plan()
    plan.steps[0].acceptance_criteria = []

    result = validate_execution_plan(plan, valid_tool_names={"read_file", "edit_file"})

    assert result.ok is False
    assert any("acceptance_criteria" in error for error in result.errors)


def test_plan_validation_requires_evidence_for_mutating_step():
    plan = _plan()
    plan.steps[1].required_evidence = []

    result = validate_execution_plan(plan, valid_tool_names={"read_file", "edit_file"})

    assert result.ok is False
    assert any("requires evidence" in error for error in result.errors)


def test_plan_validation_rejects_bad_dependency_unknown_tool_bad_risk_and_cycle():
    plan = _plan()
    plan.steps[0].dependencies = ["edit"]
    plan.steps[1].dependencies = ["read", "missing"]
    plan.steps[1].allowed_tools = ["phantom_tool"]
    plan.steps[1].risk_level = "spicy"

    result = validate_execution_plan(plan, valid_tool_names={"read_file", "edit_file"})

    assert result.ok is False
    assert any("unknown dependency missing" in error for error in result.errors)
    assert any("unknown tool phantom_tool" in error for error in result.errors)
    assert any("invalid risk level" in error for error in result.errors)
    assert any("cyclic dependency" in error for error in result.errors)


def test_store_rejects_invalid_plan_contract(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    _running_task(store)
    plan = _plan()
    plan.steps[1].required_evidence = []

    with pytest.raises(InvalidStateTransition):
        store.save_execution_plan(plan, valid_tool_names={"read_file", "edit_file"})


def test_runtime_determines_active_step_from_dependencies(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    _running_task(store)
    store.save_execution_plan(_plan(), valid_tool_names={"read_file", "edit_file"})

    active = store.current_active_step("task-plan")
    assert active is not None
    assert active.step_id == "read"
    store.record_successful_step(
        "task-plan",
        step_id="read",
        tool_call={"name": "read_file", "arguments": {"filepath": "app.py"}},
        tool_result={"tool": "read_file", "ok": True, "message": "Read app.py"},
    )
    gate = store.complete_plan_step("task-plan", "read")
    assert gate.ok is True

    next_active = store.current_active_step("task-plan")
    assert next_active is not None
    assert next_active.step_id == "edit"


def test_plan_step_completion_waits_for_required_evidence(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    _running_task(store)
    store.save_execution_plan(_plan(), valid_tool_names={"read_file", "edit_file"})
    store.current_active_step("task-plan")
    store.record_successful_step(
        "task-plan",
        step_id="read",
        tool_call={"name": "read_file", "arguments": {"filepath": "app.py"}},
        tool_result={"tool": "read_file", "ok": True, "message": "Read app.py"},
    )
    store.complete_plan_step("task-plan", "read")
    store.current_active_step("task-plan")

    gate = store.complete_plan_step("task-plan", "edit")

    assert gate.ok is False
    assert gate.missing_evidence == (EvidenceType.FILE_CHANGED.value,)

    store.record_evidence(
        EvidenceRecord(
            evidence_id="ev-edit",
            task_id="task-plan",
            step_id="edit",
            evidence_type=EvidenceType.FILE_CHANGED,
            source_tool="edit_file",
            related_files=["app.py"],
        )
    )
    gate = store.complete_plan_step("task-plan", "edit")

    assert gate.ok is True
    assert store.load_step("task-plan", "edit").status == TaskStepStatus.DONE
    assert store.load_task_plan("task-plan").steps[1].status == PlanStepStatus.COMPLETED


def test_task_completion_rejects_incomplete_plan_steps(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    _running_task(store)
    store.save_execution_plan(_plan(), valid_tool_names={"read_file", "edit_file"})
    store.current_active_step("task-plan")

    gate = store.request_task_complete("task-plan")

    assert gate.ok is False
    assert gate.incomplete_steps == ("read", "edit")
    assert store.require_task("task-plan").status == RunStatus.RUNNING
