"""Recording evidence, and the completion gate that reads it.

This is where the gateway's *report* of evidence becomes a persisted, checkable
fact. The split matters: `ToolResult.evidence` says what a result would be
entitled to; only a row in `evidence`, keyed to a real `tool_events` row, makes
it true.

The rule is one line and the whole design serves it:

    required_evidence ⊆ verified_evidence

No evidence means no completion. There is deliberately no path through this
module by which a model assertion becomes a row.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from shamsu.interfaces.enums import EvidenceKind, Phase
from shamsu.interfaces.ids import EvidenceId, RunId, StepId, TaskId, ToolEventId
from shamsu.interfaces.tools import ToolRequest, ToolResult
from shamsu.state.records import EvidenceRecord, ToolEventRecord, new_id
from shamsu.state.store import StateStore


@dataclass(frozen=True)
class Recorded:
    """What one recorded tool call produced."""

    event_id: ToolEventId
    evidence: tuple[EvidenceKind, ...]

    @property
    def produced_evidence(self) -> bool:
        return bool(self.evidence)


class EvidenceRecorder:
    """Persists tool calls and the evidence they legitimately produced."""

    def __init__(self, store: StateStore, run_id: RunId, task_id: TaskId) -> None:
        self._store = store
        self._run_id = run_id
        self._task_id = task_id

    def record(
        self,
        request: ToolRequest,
        result: ToolResult,
        phase: Phase,
        *,
        step_id: StepId | None = None,
    ) -> Recorded:
        """Persist a tool call, then register the evidence it earned.

        The event is written whether the call succeeded or not — a ledger that
        only remembers successes cannot explain a failure. Evidence is
        registered only for `ok=True`, and only for the kinds the result
        actually carries.
        """
        event = self._store.record_tool_event(
            ToolEventRecord(
                event_id=ToolEventId(new_id()),
                run_id=self._run_id,
                task_id=self._task_id,
                step_id=step_id,
                tool=request.tool,
                phase=phase,
                arguments_json=json.dumps(dict(request.arguments), default=str),
                ok=result.ok,
                output=result.output,
                error=result.error,
                truncated=result.truncated,
                original_bytes=result.original_bytes,
                duration_seconds=result.duration_seconds,
            )
        )

        if not result.ok:
            # A tool that did not do its job did not produce proof of doing it.
            return Recorded(event_id=event.event_id, evidence=())

        registered: list[EvidenceKind] = []
        for kind in sorted(result.evidence, key=lambda item: item.value):
            self._store.record_evidence(
                EvidenceRecord(
                    evidence_id=EvidenceId(new_id()),
                    task_id=self._task_id,
                    step_id=step_id,
                    kind=kind,
                    source_event_id=event.event_id,
                    detail=_detail(result),
                )
            )
            registered.append(kind)

        return Recorded(event_id=event.event_id, evidence=tuple(registered))

    def verified(self, step_id: StepId | None = None) -> frozenset[EvidenceKind]:
        """Evidence actually registered, for the completion gate."""
        return self._store.verified_evidence(self._task_id, step_id)


@dataclass(frozen=True)
class GateResult:
    """Whether a claim of completion holds up."""

    satisfied: bool
    required: frozenset[EvidenceKind]
    verified: frozenset[EvidenceKind]

    @property
    def missing(self) -> frozenset[EvidenceKind]:
        return self.required - self.verified

    def explain(self) -> str:
        """Why the gate opened or refused, in one line.

        Named rather than counted: "missing 2 of 3" tells a user nothing they
        can act on, and this text ends up in the final report.
        """
        if self.satisfied:
            if not self.required:
                return "No evidence was required for this step."
            names = ", ".join(sorted(kind.value for kind in self.required))
            return f"All required evidence is verified: {names}."
        names = ", ".join(sorted(kind.value for kind in self.missing))
        return f"Not complete. Missing verified evidence: {names}."


def check_completion(
    required: Sequence[EvidenceKind] | frozenset[EvidenceKind],
    verified: frozenset[EvidenceKind],
) -> GateResult:
    """Apply `required ⊆ verified`.

    A pure function taking two sets, so the rule that governs every completion
    decision can be exercised without a database, a runtime, or a model.
    """
    required_set = frozenset(required)
    return GateResult(
        satisfied=required_set <= verified,
        required=required_set,
        verified=verified,
    )


#: What each claim needs before the runtime will accept it (plan §25).
CLAIM_REQUIREMENTS: Mapping[str, frozenset[EvidenceKind]] = {
    "file_modified": frozenset({EvidenceKind.FILE_CHANGED, EvidenceKind.GIT_DIFF_REVIEWED}),
    "tests_pass": frozenset({EvidenceKind.TESTS_PASSED}),
    "build_succeeds": frozenset({EvidenceKind.BUILD_SUCCEEDED}),
    "app_runs": frozenset({EvidenceKind.HEALTH_CHECK_PASSED, EvidenceKind.SMOKE_TEST_PASSED}),
    "migration_succeeds": frozenset({EvidenceKind.MIGRATION_APPLIED, EvidenceKind.SCHEMA_VERIFIED}),
}


def requirements_for(claim: str) -> frozenset[EvidenceKind]:
    """Evidence a named claim requires.

    An unknown claim requires nothing *and that is deliberate*: the caller is
    expected to have validated the claim name. Silently returning an empty
    requirement for a typo would let `tests_pas` complete a task, so callers
    should use `CLAIM_REQUIREMENTS` membership to validate first.
    """
    return CLAIM_REQUIREMENTS.get(claim, frozenset())


def _detail(result: ToolResult) -> str:
    """A short, human-readable note attached to an evidence row."""
    first_line = result.output.strip().splitlines()[0] if result.output.strip() else ""
    return first_line[:200]


__all__ = [
    "CLAIM_REQUIREMENTS",
    "EvidenceRecorder",
    "GateResult",
    "Recorded",
    "check_completion",
    "requirements_for",
]
