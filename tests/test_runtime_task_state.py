from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from shamsu.runtime.task_state import (
    CorruptRuntimeState,
    EvidenceRecord,
    EvidenceStatus,
    EvidenceType,
    InvalidStateTransition,
    RuntimeStateStore,
    StepState,
    bind_task_state,
)
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.types import ApprovalRequest, RunStatus, TaskStepStatus


def test_create_update_and_reload_task_state(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    store.create_run("run-a", status=RunStatus.RUNNING)
    task = store.create_task(
        run_id="run-a",
        task_id="task-a",
        project_id="project",
        user_request="build the thing",
    )

    assert task.task_id == "task-a"
    assert task.last_checkpoint.startswith("task_initialized:")

    task.status = RunStatus.RUNNING
    task.current_phase = "planned"
    task.required_evidence.append("tests pass")
    store.save_task(task, checkpoint_kind="plan_created")
    store.record_successful_step(
        "task-a",
        step_id="step-1",
        tool_call={"name": "read_file", "arguments": {"filepath": "app.py"}},
        tool_result={"tool": "read_file", "ok": True, "message": "Read app.py"},
        changed_files=[],
    )

    reloaded = RuntimeStateStore(tmp_path).require_task("task-a")
    assert reloaded.run_id == "run-a"
    assert reloaded.current_phase == "running"
    assert reloaded.action_count == 1
    assert reloaded.last_tool_call["name"] == "read_file"
    assert reloaded.required_evidence == ["tests pass"]
    assert reloaded.last_checkpoint.startswith("step_completed:")


def test_invalid_state_transition_is_rejected(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-a", task_id="task-a", user_request="x")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    assert store.request_task_complete("task-a").ok is True

    task.status = RunStatus.RUNNING
    with pytest.raises(InvalidStateTransition):
        store.save_task(task, checkpoint_kind="illegal_restart")


def test_concurrent_tasks_do_not_overwrite_each_other(tmp_path: Path):
    first = RuntimeStateStore(tmp_path)
    second = RuntimeStateStore(tmp_path)
    first.create_task(run_id="run-one", task_id="task-one", user_request="one")
    second.create_task(run_id="run-two", task_id="task-two", user_request="two")

    first.update_task_status("task-one", RunStatus.RUNNING, checkpoint_kind="started")
    second.update_task_status("task-two", RunStatus.RUNNING, checkpoint_kind="started")
    first.update_task_status("task-one", RunStatus.CANCELLED, checkpoint_kind="cancelled")
    assert second.request_task_complete("task-two").ok is True

    fresh = RuntimeStateStore(tmp_path)
    assert fresh.require_task("task-one").status == RunStatus.CANCELLED
    assert fresh.require_task("task-two").status == RunStatus.COMPLETED


def test_cancelled_task_reload(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-cancel", task_id="task-cancel", user_request="stop")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    store.update_task_status("task-cancel", RunStatus.CANCELLED, checkpoint_kind="cancelled")

    reloaded = RuntimeStateStore(tmp_path).require_task("task-cancel")
    assert reloaded.status == RunStatus.CANCELLED
    assert reloaded.last_checkpoint.startswith("cancelled:")


def test_completed_task_reload(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-done", task_id="task-done", user_request="finish")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    assert store.request_task_complete("task-done").ok is True

    reloaded = RuntimeStateStore(tmp_path).require_task("task-done")
    assert reloaded.status == RunStatus.COMPLETED
    assert reloaded.last_checkpoint.startswith("before_final_completion:")


def test_completion_with_valid_evidence(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-evidence", task_id="task-evidence", user_request="test")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    store.record_step(
        StepState(
            step_id="step-test",
            task_id="task-evidence",
            run_id="run-evidence",
            status=TaskStepStatus.RUNNING,
            required_evidence=[EvidenceType.TEST_PASSED.value],
            started_at="2026-01-01T00:00:00+00:00",
        )
    )
    store.record_evidence(
        EvidenceRecord(
            evidence_id="ev-test",
            task_id="task-evidence",
            step_id="step-test",
            evidence_type=EvidenceType.TEST_PASSED,
            source_tool="run_command",
            timestamp="2026-01-01T00:00:01+00:00",
            related_command="pytest",
            exit_code=0,
        )
    )

    step_gate = store.request_step_complete("task-evidence", "step-test")
    task_gate = store.request_task_complete("task-evidence")

    assert step_gate.ok is True
    assert task_gate.ok is True
    assert store.require_task("task-evidence").status == RunStatus.COMPLETED


def test_completion_with_missing_evidence_is_rejected(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-missing", task_id="task-missing", user_request="test")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    store.record_step(
        StepState(
            step_id="step-test",
            task_id="task-missing",
            run_id="run-missing",
            status=TaskStepStatus.RUNNING,
            required_evidence=[EvidenceType.TEST_PASSED.value],
        )
    )

    gate = store.request_step_complete("task-missing", "step-test")

    assert gate.ok is False
    assert gate.missing_evidence == (EvidenceType.TEST_PASSED.value,)
    assert store.load_step("task-missing", "step-test").status == TaskStepStatus.RUNNING


def test_stale_evidence_is_rejected(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-stale", task_id="task-stale", user_request="test")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    store.record_step(
        StepState(
            step_id="step-test",
            task_id="task-stale",
            run_id="run-stale",
            status=TaskStepStatus.RUNNING,
            required_evidence=[EvidenceType.TEST_PASSED.value],
            started_at="2026-01-02T00:00:00+00:00",
        )
    )
    store.record_evidence(
        EvidenceRecord(
            evidence_id="ev-stale",
            task_id="task-stale",
            step_id="step-test",
            evidence_type=EvidenceType.TEST_PASSED,
            source_tool="run_command",
            timestamp="2026-01-01T00:00:00+00:00",
            related_command="pytest",
            exit_code=0,
        )
    )

    gate = store.request_step_complete("task-stale", "step-test")

    assert gate.ok is False
    assert gate.stale_evidence == (EvidenceType.TEST_PASSED.value,)


def test_evidence_from_another_task_is_rejected(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-target", task_id="task-target", user_request="target")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    other = store.create_task(run_id="run-other", task_id="task-other", user_request="other")
    other.status = RunStatus.RUNNING
    store.save_task(other, checkpoint_kind="started")
    store.record_step(
        StepState(
            step_id="step-test",
            task_id="task-target",
            run_id="run-target",
            status=TaskStepStatus.RUNNING,
            required_evidence=[EvidenceType.TEST_PASSED.value],
        )
    )
    store.record_evidence(
        EvidenceRecord(
            evidence_id="ev-other",
            task_id="task-other",
            step_id="step-test",
            evidence_type=EvidenceType.TEST_PASSED,
            source_tool="run_command",
            related_command="pytest",
            exit_code=0,
        )
    )

    gate = store.request_step_complete("task-target", "step-test")

    assert gate.ok is False
    assert gate.missing_evidence == (EvidenceType.TEST_PASSED.value,)


def test_failed_test_recorded_as_evidence_does_not_satisfy_gate(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-fail", task_id="task-fail", user_request="test")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    store.record_step(
        StepState(
            step_id="step-test",
            task_id="task-fail",
            run_id="run-fail",
            status=TaskStepStatus.RUNNING,
            required_evidence=[EvidenceType.TEST_PASSED.value],
        )
    )
    store.record_evidence(
        EvidenceRecord(
            evidence_id="ev-fail",
            task_id="task-fail",
            step_id="step-test",
            evidence_type=EvidenceType.TEST_PASSED,
            source_tool="run_command",
            status=EvidenceStatus.FAILED,
            related_command="pytest",
            exit_code=1,
        )
    )

    gate = store.request_step_complete("task-fail", "step-test")

    assert gate.ok is False
    assert gate.failed_evidence == (EvidenceType.TEST_PASSED.value,)


def test_changed_file_without_diff_review_does_not_satisfy_completion(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-diff", task_id="task-diff", user_request="edit")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")

    store.record_successful_step(
        "task-diff",
        step_id="step-edit",
        tool_call={"name": "write_file", "arguments": {"filepath": "app.py"}},
        tool_result={"tool": "write_file", "ok": True, "message": "Wrote app.py"},
        changed_files=["app.py"],
        required_evidence=[EvidenceType.GIT_DIFF_REVIEWED],
    )

    step = store.load_step("task-diff", "step-edit")
    assert step.status == TaskStepStatus.RUNNING
    evidence = store.list_evidence("task-diff", step_id="step-edit")
    assert [record.evidence_type for record in evidence] == [EvidenceType.FILE_CHANGED]


def test_duplicate_evidence_is_deduped(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    store.create_task(run_id="run-dupe", task_id="task-dupe", user_request="test")
    evidence = EvidenceRecord(
        evidence_id="ev-one",
        task_id="task-dupe",
        step_id="step-test",
        evidence_type=EvidenceType.TEST_PASSED,
        source_tool="run_command",
        related_command="pytest",
        exit_code=0,
    )

    first = store.record_evidence(evidence)
    second = store.record_evidence(
        EvidenceRecord(
            evidence_id="ev-two",
            task_id="task-dupe",
            step_id="step-test",
            evidence_type=EvidenceType.TEST_PASSED,
            source_tool="run_command",
            related_command="pytest",
            exit_code=0,
        )
    )

    assert first.evidence_id == second.evidence_id
    assert len(store.list_evidence("task-dupe")) == 1


def test_task_completion_before_steps_complete_is_rejected(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-incomplete", task_id="task-incomplete", user_request="build")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    store.record_step(
        StepState(
            step_id="step-open",
            task_id="task-incomplete",
            run_id="run-incomplete",
            status=TaskStepStatus.RUNNING,
        )
    )

    gate = store.request_task_complete("task-incomplete")

    assert gate.ok is False
    assert gate.incomplete_steps == ("step-open",)
    assert store.require_task("task-incomplete").status == RunStatus.RUNNING


def test_checkpoint_recovery_recovers_latest_valid_snapshot(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-recover", task_id="task-recover", user_request="recover")
    task.status = RunStatus.RUNNING
    task.current_phase = "planned"
    store.save_task(task, checkpoint_kind="plan_created")
    store.corrupt_task_payload_for_test("task-recover", "{not-json")

    recovered = RuntimeStateStore(tmp_path).load_task("task-recover")

    assert recovered is not None
    assert recovered.current_phase == "planned"
    assert recovered.last_checkpoint.startswith("plan_created:")


def test_approval_checkpoints_are_persisted(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    task = store.create_task(run_id="run-approval", task_id="task-approval", user_request="edit")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    manager = ApprovalManager(approval_func=lambda _request: True)

    with bind_task_state(store, "task-approval"):
        approved = manager.ask(
            ApprovalRequest(
                action_type="file_edit",
                description="Edit app.py",
                risk_level="medium",
                target_paths=["app.py"],
            )
        )

    reloaded = store.require_task("task-approval")
    assert approved is True
    assert reloaded.status == RunStatus.RUNNING
    assert reloaded.pending_approvals[-1]["approved"] is True
    assert reloaded.last_checkpoint.startswith("approval_resolved:")


def test_missing_task_returns_none(tmp_path: Path):
    assert RuntimeStateStore(tmp_path).load_task("missing") is None


def test_corrupt_task_without_recovery_can_raise(tmp_path: Path):
    store = RuntimeStateStore(tmp_path)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            """
            INSERT INTO tasks (
                task_id, run_id, status, current_phase, current_step_id,
                updated_at, last_checkpoint, payload
            )
            VALUES ('corrupt', 'run', 'running', 'running', '', '', '', ?)
            """,
            ("{not-json",),
        )

    assert store.load_task("corrupt") is None
    with pytest.raises(CorruptRuntimeState):
        store.load_task("corrupt", recover=False)
