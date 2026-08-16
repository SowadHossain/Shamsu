"""Normalized runtime failure records and deterministic recovery policy."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from shamsu.runtime.timeouts import TimeoutCategory


class FailureType(str, Enum):
    TOOL_NOT_CALLED = "TOOL_NOT_CALLED"
    WRONG_TOOL = "WRONG_TOOL"
    TOOL_SCHEMA_ERROR = "TOOL_SCHEMA_ERROR"
    REPEATED_ACTION = "REPEATED_ACTION"
    PREMATURE_COMPLETION = "PREMATURE_COMPLETION"
    PHASE_VIOLATION = "PHASE_VIOLATION"
    RETRIEVAL_NOISE = "RETRIEVAL_NOISE"
    STALE_MEMORY = "STALE_MEMORY"
    ENVIRONMENT_MISMATCH = "ENVIRONMENT_MISMATCH"
    VERIFICATION_FAILURE = "VERIFICATION_FAILURE"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    FIRST_TOKEN_TIMEOUT = "FIRST_TOKEN_TIMEOUT"
    TOKEN_IDLE_TIMEOUT = "TOKEN_IDLE_TIMEOUT"
    TOTAL_GENERATION_TIMEOUT = "TOTAL_GENERATION_TIMEOUT"
    STEP_TIMEOUT = "STEP_TIMEOUT"
    TASK_TIMEOUT = "TASK_TIMEOUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


class RecoveryAction(str, Enum):
    SAFE_ARGUMENT_REPAIR = "SAFE_ARGUMENT_REPAIR"
    BLOCK_IDENTICAL_CALL = "BLOCK_IDENTICAL_CALL"
    INVALIDATE_MEMORY_AND_REREAD = "INVALIDATE_MEMORY_AND_REREAD"
    ENTER_REPAIR = "ENTER_REPAIR"
    REJECT_ACTION = "REJECT_ACTION"
    REJECT_COMPLETION = "REJECT_COMPLETION"
    RETRY_WITH_TOOL_CONTRACT = "RETRY_WITH_TOOL_CONTRACT"
    REQUEST_INPUT = "REQUEST_INPUT"
    REPLAN = "REPLAN"
    STOP = "STOP"


@dataclass(frozen=True)
class RecoveryPolicy:
    action: RecoveryAction
    max_retries: int
    message: str


@dataclass
class FailureRecord:
    failure_type: FailureType
    task_id: str
    step_id: str = ""
    action: str = ""
    error_signature: str = ""
    evidence: list[str] = field(default_factory=list)
    retry_count: int = 0
    first_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failure_type"] = self.failure_type.value
        return payload


RECOVERY_POLICIES: dict[FailureType, RecoveryPolicy] = {
    FailureType.TOOL_NOT_CALLED: RecoveryPolicy(
        RecoveryAction.RETRY_WITH_TOOL_CONTRACT,
        2,
        "Require the next response to call an allowed tool or ask for a specific missing input.",
    ),
    FailureType.WRONG_TOOL: RecoveryPolicy(
        RecoveryAction.REJECT_ACTION,
        1,
        "Reject the requested tool and provide the allowed alternatives.",
    ),
    FailureType.TOOL_SCHEMA_ERROR: RecoveryPolicy(
        RecoveryAction.SAFE_ARGUMENT_REPAIR,
        1,
        "Permit one safe argument repair.",
    ),
    FailureType.REPEATED_ACTION: RecoveryPolicy(
        RecoveryAction.BLOCK_IDENTICAL_CALL,
        0,
        "Block the identical call and require a different action.",
    ),
    FailureType.PREMATURE_COMPLETION: RecoveryPolicy(
        RecoveryAction.REJECT_COMPLETION,
        0,
        "Reject completion until required evidence exists.",
    ),
    FailureType.PHASE_VIOLATION: RecoveryPolicy(
        RecoveryAction.REJECT_ACTION,
        0,
        "Reject the action because the current phase forbids it.",
    ),
    FailureType.RETRIEVAL_NOISE: RecoveryPolicy(
        RecoveryAction.REPLAN,
        1,
        "Drop noisy retrieval context and recompile from runtime state.",
    ),
    FailureType.STALE_MEMORY: RecoveryPolicy(
        RecoveryAction.INVALIDATE_MEMORY_AND_REREAD,
        1,
        "Invalidate stale memory and reread fresh source evidence.",
    ),
    FailureType.ENVIRONMENT_MISMATCH: RecoveryPolicy(
        RecoveryAction.REQUEST_INPUT,
        1,
        "Ask for or repair missing environment prerequisites after one bounded retry.",
    ),
    FailureType.VERIFICATION_FAILURE: RecoveryPolicy(
        RecoveryAction.ENTER_REPAIR,
        1,
        "Enter the repair phase with the failing verification evidence.",
    ),
    FailureType.MODEL_TIMEOUT: RecoveryPolicy(
        RecoveryAction.STOP,
        0,
        "Stop the run or surface timeout status.",
    ),
    FailureType.TOOL_TIMEOUT: RecoveryPolicy(
        RecoveryAction.STOP,
        0,
        "Stop the tool attempt and report timeout evidence.",
    ),
    FailureType.CONNECT_TIMEOUT: RecoveryPolicy(
        RecoveryAction.STOP,
        0,
        "Stop because the model transport could not connect.",
    ),
    FailureType.FIRST_TOKEN_TIMEOUT: RecoveryPolicy(
        RecoveryAction.STOP,
        0,
        "Stop because generation never started.",
    ),
    FailureType.TOKEN_IDLE_TIMEOUT: RecoveryPolicy(
        RecoveryAction.STOP,
        0,
        "Stop because generation started and then went silent.",
    ),
    FailureType.TOTAL_GENERATION_TIMEOUT: RecoveryPolicy(
        RecoveryAction.STOP,
        0,
        "Stop because the optional total generation cap was reached.",
    ),
    FailureType.STEP_TIMEOUT: RecoveryPolicy(
        RecoveryAction.REPLAN,
        0,
        "Stop or replan the timed-out step.",
    ),
    FailureType.TASK_TIMEOUT: RecoveryPolicy(
        RecoveryAction.STOP,
        0,
        "Stop because the overall task deadline was reached.",
    ),
    FailureType.PERMISSION_DENIED: RecoveryPolicy(
        RecoveryAction.REJECT_ACTION,
        0,
        "Do not retry denied actions.",
    ),
    FailureType.UNKNOWN_FAILURE: RecoveryPolicy(
        RecoveryAction.REPLAN,
        1,
        "Request a higher-level replan after bounded retries.",
    ),
}


def recovery_policy(failure_type: FailureType | str) -> RecoveryPolicy:
    normalized = normalize_failure_type(failure_type)
    return RECOVERY_POLICIES[normalized]


def normalize_failure_type(value: FailureType | str) -> FailureType:
    if isinstance(value, FailureType):
        return value
    try:
        return FailureType(str(value))
    except ValueError:
        return FailureType.UNKNOWN_FAILURE


def make_error_signature(
    failure_type: FailureType | str,
    *,
    action: str = "",
    evidence: list[str] | tuple[str, ...] | None = None,
    detail: Any = None,
) -> str:
    payload = {
        "failure_type": normalize_failure_type(failure_type).value,
        "action": action,
        "evidence": list(evidence or []),
        "detail": detail,
    }
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def failure_type_for_timeout(category: TimeoutCategory | str) -> FailureType:
    try:
        normalized = category if isinstance(category, TimeoutCategory) else TimeoutCategory(str(category))
    except ValueError:
        return FailureType.UNKNOWN_FAILURE
    return {
        TimeoutCategory.CONNECT_TIMEOUT: FailureType.CONNECT_TIMEOUT,
        TimeoutCategory.FIRST_TOKEN_TIMEOUT: FailureType.FIRST_TOKEN_TIMEOUT,
        TimeoutCategory.TOKEN_IDLE_TIMEOUT: FailureType.TOKEN_IDLE_TIMEOUT,
        TimeoutCategory.TOTAL_GENERATION_TIMEOUT: FailureType.TOTAL_GENERATION_TIMEOUT,
        TimeoutCategory.TOOL_TIMEOUT: FailureType.TOOL_TIMEOUT,
        TimeoutCategory.STEP_TIMEOUT: FailureType.STEP_TIMEOUT,
        TimeoutCategory.TASK_TIMEOUT: FailureType.TASK_TIMEOUT,
    }.get(normalized, FailureType.UNKNOWN_FAILURE)


class FailureTracker:
    """Small facade that normalizes observed failures before recovery branches act."""

    def __init__(self, store: Any, task_id: str, step_id_getter: Any | None = None) -> None:
        self.store = store
        self.task_id = task_id
        self._step_id_getter = step_id_getter

    def record(
        self,
        failure_type: FailureType | str,
        *,
        action: str = "",
        evidence: list[str] | tuple[str, ...] | None = None,
        detail: Any = None,
        step_id: str = "",
    ) -> FailureRecord:
        normalized = normalize_failure_type(failure_type)
        current_step = step_id or self._current_step_id()
        if hasattr(self.store, "create_failure"):
            return self.store.create_failure(
                self.task_id,
                normalized,
                step_id=current_step,
                action=action,
                evidence=list(evidence or []),
                detail=detail,
            )
        return FailureRecord(
            failure_type=normalized,
            task_id=self.task_id,
            step_id=current_step,
            action=action,
            error_signature=make_error_signature(
                normalized,
                action=action,
                evidence=list(evidence or []),
                detail=detail,
            ),
            evidence=list(evidence or []),
        )

    def policy_for(self, failure: FailureRecord) -> RecoveryPolicy:
        return recovery_policy(failure.failure_type)

    def decision(
        self,
        failure_type: FailureType | str,
        *,
        action: str = "",
        evidence: list[str] | tuple[str, ...] | None = None,
        detail: Any = None,
        step_id: str = "",
    ) -> tuple[FailureRecord, RecoveryPolicy]:
        record = self.record(
            failure_type,
            action=action,
            evidence=evidence,
            detail=detail,
            step_id=step_id,
        )
        return record, self.policy_for(record)

    def classify_tool_result(self, name: str, result: Any) -> FailureType | None:
        if bool(getattr(result, "ok", False)):
            return None
        data = getattr(result, "data", None)
        data = data if isinstance(data, dict) else {}
        message = str(getattr(result, "message", "") or "").lower()
        reason = str(data.get("reason") or "").lower()
        if "denied by phase contract" in message or "phase" in data.get("current_phase", ""):
            return FailureType.PHASE_VIOLATION
        if data.get("blocked_tool") or "not allowed for the current orchestrated step" in message:
            return FailureType.WRONG_TOOL
        if "denied by user" in message or "permission denied" in message:
            return FailureType.PERMISSION_DENIED
        if "timeout" in message or "timed out" in message or data.get("timeout"):
            return FailureType.TOOL_TIMEOUT
        if (
            "missing" in message
            or "invalid" in message
            or "required" in message
            or "schema" in message
            or "argument" in reason
        ):
            return FailureType.TOOL_SCHEMA_ERROR
        if "not found" in message or "not a file" in message or "no similar files" in message:
            return FailureType.RETRIEVAL_NOISE
        if "environment" in message or "not installed" in message or "executable" in message:
            return FailureType.ENVIRONMENT_MISMATCH
        return FailureType.UNKNOWN_FAILURE

    def record_tool_result(self, name: str, arguments: dict[str, Any], result: Any) -> FailureRecord | None:
        failure_type = self.classify_tool_result(name, result)
        if failure_type is None:
            return None
        evidence = [str(getattr(result, "message", "") or "")]
        detail = {
            "tool": name,
            "arguments": arguments,
            "data": getattr(result, "data", None),
        }
        return self.record(failure_type, action=name, evidence=evidence, detail=detail)

    def _current_step_id(self) -> str:
        if self._step_id_getter is None:
            return ""
        try:
            return str(self._step_id_getter() or "")
        except Exception:
            return ""
