"""Small deterministic status transitions for agent runtime results."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from shamsu.runtime.task_state import CompletionGateResult
from shamsu.types import RunStatus


def status_from_result(result: Any) -> RunStatus:
    status = getattr(result, "status", RunStatus.COMPLETED)
    if status != RunStatus.COMPLETED:
        return status
    if getattr(result, "timeout_category", None):
        return RunStatus.TIMED_OUT
    if getattr(result, "stopped", False) and not getattr(result, "awaiting_user", False):
        return RunStatus.FAILED
    return RunStatus.COMPLETED


def apply_completion_gate_failure(result: Any, gate: CompletionGateResult | None) -> Any:
    if gate is None:
        return replace(
            result,
            final=(
                f"{result.final}\n\n"
                "Completion not registered: runtime completion gate was unavailable."
            ).strip(),
            stopped=True,
        )
    reason = gate.message or "Completion evidence gate failed."
    detail = ""
    if gate.missing_evidence:
        detail = " Missing evidence: " + ", ".join(gate.missing_evidence) + "."
    if gate.incomplete_steps:
        detail += " Incomplete steps: " + ", ".join(gate.incomplete_steps) + "."
    return replace(
        result,
        final=f"{result.final}\n\nCompletion not registered: {reason}{detail}".strip(),
        stopped=True,
    )
