from __future__ import annotations

from types import SimpleNamespace

from shamsu.runtime.failures import (
    FailureTracker,
    FailureType,
    RecoveryAction,
    failure_type_for_timeout,
    recovery_policy,
)
from shamsu.runtime.task_state import RuntimeStateStore
from shamsu.runtime.timeouts import TimeoutCategory, TimeoutConfig


def test_failure_policy_maps_known_recoveries():
    assert recovery_policy(FailureType.TOOL_SCHEMA_ERROR).action == RecoveryAction.SAFE_ARGUMENT_REPAIR
    assert recovery_policy(FailureType.REPEATED_ACTION).action == RecoveryAction.BLOCK_IDENTICAL_CALL
    assert recovery_policy(FailureType.STALE_MEMORY).action == RecoveryAction.INVALIDATE_MEMORY_AND_REREAD
    assert recovery_policy(FailureType.VERIFICATION_FAILURE).action == RecoveryAction.ENTER_REPAIR
    assert recovery_policy(FailureType.PHASE_VIOLATION).action == RecoveryAction.REJECT_ACTION
    assert recovery_policy(FailureType.PREMATURE_COMPLETION).action == RecoveryAction.REJECT_COMPLETION
    assert recovery_policy(FailureType.UNKNOWN_FAILURE).action == RecoveryAction.REPLAN
    assert recovery_policy(FailureType.FIRST_TOKEN_TIMEOUT).action == RecoveryAction.STOP
    assert recovery_policy(FailureType.TOKEN_IDLE_TIMEOUT).action == RecoveryAction.STOP
    assert recovery_policy(FailureType.TASK_TIMEOUT).action == RecoveryAction.STOP


def test_timeout_categories_map_to_failure_types():
    assert failure_type_for_timeout(TimeoutCategory.CONNECT_TIMEOUT) == FailureType.CONNECT_TIMEOUT
    assert failure_type_for_timeout(TimeoutCategory.FIRST_TOKEN_TIMEOUT) == FailureType.FIRST_TOKEN_TIMEOUT
    assert failure_type_for_timeout(TimeoutCategory.TOKEN_IDLE_TIMEOUT) == FailureType.TOKEN_IDLE_TIMEOUT
    assert failure_type_for_timeout(TimeoutCategory.TOTAL_GENERATION_TIMEOUT) == FailureType.TOTAL_GENERATION_TIMEOUT
    assert failure_type_for_timeout(TimeoutCategory.TOOL_TIMEOUT) == FailureType.TOOL_TIMEOUT
    assert failure_type_for_timeout(TimeoutCategory.STEP_TIMEOUT) == FailureType.STEP_TIMEOUT
    assert failure_type_for_timeout(TimeoutCategory.TASK_TIMEOUT) == FailureType.TASK_TIMEOUT
    assert failure_type_for_timeout("not-a-timeout") == FailureType.UNKNOWN_FAILURE
    assert TimeoutConfig().total_generation_timeout == 0.0


def test_failure_records_persist_and_increment_retry_count(tmp_path):
    store = RuntimeStateStore(tmp_path)
    store.create_task(run_id="run", task_id="task", user_request="fix")
    tracker = FailureTracker(store, "task")

    first = tracker.record(
        FailureType.REPEATED_ACTION,
        action="file.read",
        evidence=["same app.py read"],
        detail={"filepath": "app.py"},
    )
    second = tracker.record(
        FailureType.REPEATED_ACTION,
        action="file.read",
        evidence=["same app.py read"],
        detail={"filepath": "app.py"},
    )

    reloaded = RuntimeStateStore(tmp_path).list_failures("task", FailureType.REPEATED_ACTION)
    assert second.error_signature == first.error_signature
    assert second.retry_count == 1
    assert len(reloaded) == 1
    assert reloaded[0].retry_count == 1
    assert reloaded[0].failure_type == FailureType.REPEATED_ACTION


def test_tool_result_classifier_normalizes_denials_and_schema_errors(tmp_path):
    store = RuntimeStateStore(tmp_path)
    store.create_task(run_id="run", task_id="task", user_request="fix")
    tracker = FailureTracker(store, "task")

    phase = tracker.record_tool_result(
        "write_file",
        {},
        SimpleNamespace(
            ok=False,
            message="Tool write_file denied by phase contract: no mutation",
            data={"requested_tool": "write_file", "current_phase": "VERIFY"},
        ),
    )
    schema = tracker.record_tool_result(
        "file.read",
        {},
        SimpleNamespace(ok=False, message="Missing filepath.", data={}),
    )
    timeout = tracker.record_tool_result(
        "run_command",
        {"command": "pytest"},
        SimpleNamespace(ok=False, message="Command timed out.", data={"timeout": True}),
    )
    blocked = tracker.record_tool_result(
        "file.patch",
        {"filepath": "app.py"},
        SimpleNamespace(
            ok=False,
            message="Tool file.patch is not allowed for the current orchestrated step.",
            data={
                "blocked_tool": "file.patch",
                "requested_tool": "file.patch",
                "allowed_tools": ["read_file"],
                "reason": "Tool is not in the active plan step's allowed tools.",
            },
        ),
    )

    assert phase is not None
    assert phase.failure_type == FailureType.PHASE_VIOLATION
    assert schema is not None
    assert schema.failure_type == FailureType.TOOL_SCHEMA_ERROR
    assert timeout is not None
    assert timeout.failure_type == FailureType.TOOL_TIMEOUT
    assert blocked is not None
    assert blocked.failure_type == FailureType.PERMISSION_DENIED
    assert recovery_policy(blocked.failure_type).max_retries == 0


def test_premature_completion_failure_can_be_recorded_from_gate(tmp_path):
    store = RuntimeStateStore(tmp_path)
    store.create_task(run_id="run", task_id="task", user_request="finish")

    failure = store.create_failure(
        "task",
        FailureType.PREMATURE_COMPLETION,
        action="TASK_COMPLETE",
        evidence=["test_passed"],
        detail="missing evidence",
    )

    assert failure.failure_type == FailureType.PREMATURE_COMPLETION
    assert failure.action == "TASK_COMPLETE"
    assert store.list_failures("task", FailureType.PREMATURE_COMPLETION)[0].evidence == ["test_passed"]
