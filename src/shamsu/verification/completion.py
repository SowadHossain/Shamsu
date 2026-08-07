"""Claim validation, the final completion gate, and the evidence report.

Plan §20.7 states the rule this module exists to enforce: *the model cannot set
completion directly*. It may propose. The runtime decides, from rows.

Three layers, narrowest first.

**Claim validation** answers "does this named claim hold?" A claim is a key
into a requirement set the runtime defined in advance (plan §25). An unknown
claim is *rejected*, not treated as requiring nothing — otherwise `tests_pas`
completes a task that `tests_pass` would not, which is a typo with the blast
radius of a security hole.

**The final gate** answers "is this task done?" It is not just the task-level
union of evidence, because that union is satisfiable by one thorough step. A
task is complete when every step passed its own gate, at its own scope, and
nothing is left unfinished. Evidence earned by step 1 does not finish step 4.

**The evidence report** is generated from `tool_events` and `evidence` rows —
never from model prose. It is the artifact a user reads to decide whether to
trust the run, so its provenance has to be the same as the gate's.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from shamsu.interfaces.enums import AgentState, EvidenceKind, StepOutcome
from shamsu.interfaces.ids import PlanId, StepId, TaskId
from shamsu.state.records import PlanStepRecord, ToolEventRecord
from shamsu.state.store import StateStore
from shamsu.verification.evidence import CLAIM_REQUIREMENTS, GateResult, check_completion

#: The task-level claim. Kept out of `CLAIM_REQUIREMENTS` because its
#: requirements are not a fixed set: they come from the plan that was actually
#: created, so "task complete" means something different for every task.
TASK_COMPLETE = "task_complete"


def known_claims() -> tuple[str, ...]:
    """Every claim name the runtime will consider, sorted."""
    return tuple(sorted([*CLAIM_REQUIREMENTS, TASK_COMPLETE]))


@dataclass(frozen=True)
class ClaimVerdict:
    """Whether a named claim is supported by registered evidence."""

    accepted: bool
    claim: str
    required: frozenset[EvidenceKind]
    verified: frozenset[EvidenceKind]
    reason: str

    @property
    def missing(self) -> frozenset[EvidenceKind]:
        return self.required - self.verified


def validate_claim(claim: str, verified: frozenset[EvidenceKind]) -> ClaimVerdict:
    """Check a model's completion claim against evidence rows.

    An unrecognised claim is refused rather than defaulted. `requirements_for`
    returns an empty set for an unknown name, and an empty requirement set is
    trivially satisfied — so a mistyped claim would sail through the very
    check that exists to stop it.
    """
    name = claim.strip()

    if name == TASK_COMPLETE:
        return ClaimVerdict(
            accepted=False,
            claim=name,
            required=frozenset(),
            verified=verified,
            reason=(
                "'task_complete' is decided by the final gate against the plan, not by this check"
            ),
        )

    required = CLAIM_REQUIREMENTS.get(name)
    if required is None:
        return ClaimVerdict(
            accepted=False,
            claim=name,
            required=frozenset(),
            verified=verified,
            reason=f"unknown claim {claim!r}; known claims: {', '.join(known_claims())}",
        )

    result = check_completion(required, verified)
    return ClaimVerdict(
        accepted=result.satisfied,
        claim=name,
        required=required,
        verified=verified,
        reason=result.explain(),
    )


@dataclass(frozen=True)
class StepVerdict:
    """One step's standing at the final gate."""

    step_id: StepId
    ordinal: int
    title: str
    outcome: StepOutcome | None
    required: frozenset[EvidenceKind]
    verified: frozenset[EvidenceKind]

    @property
    def passed(self) -> bool:
        return self.outcome is StepOutcome.PASS

    @property
    def satisfied(self) -> bool:
        return self.required <= self.verified

    @property
    def missing(self) -> frozenset[EvidenceKind]:
        return self.required - self.verified


@dataclass(frozen=True)
class TaskCompletion:
    """Whether a task may be reported complete, and why not when it may not."""

    satisfied: bool
    steps: tuple[StepVerdict, ...]
    reason: str

    @property
    def unfinished(self) -> tuple[StepVerdict, ...]:
        return tuple(step for step in self.steps if not step.passed)

    @property
    def missing(self) -> frozenset[EvidenceKind]:
        """Every requirement not met, across all steps."""
        missing: set[EvidenceKind] = set()
        for step in self.steps:
            missing |= step.missing
        return frozenset(missing)

    @property
    def unverified(self) -> bool:
        """Satisfied, but with no evidence behind it at all.

        A plan made entirely of `investigate` steps requires nothing, so it
        passes the gate trivially. That is correct — those steps cannot write —
        but reporting it as plain "COMPLETE" would put the same word on a task
        that changed code and proved it and a task that did nothing.

        This is plan §31's `success_without_verification_rate` as a property
        rather than a statistic: the distinction has to reach the user looking
        at the report, not only the person reading the metrics later.
        """
        return self.satisfied and not any(step.required for step in self.steps)


class CompletionGate:
    """Decides whether a step or a task is finished, from rows only."""

    def __init__(self, store: StateStore, task_id: TaskId) -> None:
        self._store = store
        self._task_id = task_id

    def check_step(self, step: PlanStepRecord) -> GateResult:
        """`required ⊆ verified`, scoped to the step that earned the evidence."""
        return check_completion(
            step.required_evidence, self._store.verified_evidence(self._task_id, step.step_id)
        )

    def check_claim(self, claim: str, *, step_id: StepId | None = None) -> ClaimVerdict:
        """Validate a named claim against evidence at the requested scope."""
        return validate_claim(claim, self._store.verified_evidence(self._task_id, step_id))

    def check_task(self, plan_id: PlanId) -> TaskCompletion:
        """The final gate.

        Deliberately *not* the task-level union of evidence. A four-step plan
        whose first step ran tests, patched a file, and reviewed a diff would
        satisfy a union check outright, and the remaining three steps would
        complete having done nothing. So each step is judged at its own scope,
        and the task is complete only when all of them are.
        """
        steps = self._store.get_steps(plan_id)

        if not steps:
            return TaskCompletion(
                satisfied=False,
                steps=(),
                reason="the plan has no steps; a task that planned nothing has proven nothing",
            )

        verdicts = tuple(
            StepVerdict(
                step_id=step.step_id,
                ordinal=step.ordinal,
                title=step.title,
                outcome=step.outcome,
                required=frozenset(step.required_evidence),
                verified=self._store.verified_evidence(self._task_id, step.step_id),
            )
            for step in steps
        )

        unfinished = [step for step in verdicts if not step.passed]
        unproven = [step for step in verdicts if not step.satisfied]

        if unfinished:
            return TaskCompletion(
                satisfied=False,
                steps=verdicts,
                reason="Not complete. Unfinished steps: " + _titles(unfinished),
            )

        if unproven:
            # Reachable when a step was closed and its evidence later became
            # unverifiable -- a resumed run, a rebuilt store. The step outcome
            # is a cached decision; the rows are the fact, and the rows win.
            return TaskCompletion(
                satisfied=False,
                steps=verdicts,
                reason=(
                    "Not complete. Steps marked passed but missing evidence: " + _titles(unproven)
                ),
            )

        return TaskCompletion(
            satisfied=True,
            steps=verdicts,
            reason=f"All {len(verdicts)} step(s) passed with verified evidence.",
        )


def _titles(steps: Sequence[StepVerdict]) -> str:
    return ", ".join(f"{step.ordinal + 1}. {step.title}" for step in steps)


# ---------------------------------------------------------------------------
# Evidence report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceEntry:
    """One piece of proof, with the execution that produced it."""

    kind: EvidenceKind
    tool: str
    step_title: str
    detail: str


@dataclass(frozen=True)
class EvidenceReport:
    """What the run actually did, derived from rows.

    Every field here traces to an observed tool execution. Nothing is carried
    over from what the model said about its own work — plan §20.7 allows the
    complete phase exactly one action, and this is it.
    """

    task_id: TaskId
    request: str
    verdict: TaskCompletion
    entries: tuple[EvidenceEntry, ...]
    changed_files: tuple[str, ...]
    failed_calls: int

    def render(self) -> str:
        if not self.verdict.satisfied:
            headline = "NOT COMPLETE"
        elif self.verdict.unverified:
            # The same word must not describe a proven change and a plan that
            # only looked at things.
            headline = "COMPLETE (NOTHING VERIFIED)"
        else:
            headline = "COMPLETE"

        lines = [f"Task: {self.request}", "", f"{headline} — {self.verdict.reason}"]

        if self.verdict.unverified:
            lines.append(
                "  No step required evidence, so nothing was proved. This plan "
                "only inspected; it did not change anything."
            )

        if self.verdict.steps:
            lines.extend(["", "Steps:"])
            for step in self.verdict.steps:
                mark = "✓" if step.passed and step.satisfied else "✗"
                lines.append(f"  [{mark}] {step.ordinal + 1}. {step.title}")
                if step.missing:
                    names = ", ".join(sorted(kind.value for kind in step.missing))
                    lines.append(f"        missing: {names}")

        if self.changed_files:
            lines.extend(["", "Files changed:"])
            lines.extend(f"  {path}" for path in self.changed_files)

        if self.entries:
            lines.extend(["", "Evidence:"])
            for entry in self.entries:
                where = f" ({entry.step_title})" if entry.step_title else ""
                detail = f" — {entry.detail}" if entry.detail else ""
                lines.append(f"  {entry.kind.value} via {entry.tool}{where}{detail}")
        else:
            lines.extend(["", "Evidence: none registered."])

        if self.failed_calls:
            lines.extend(
                [
                    "",
                    f"{self.failed_calls} tool call(s) failed during this task. "
                    "Failures are kept: a ledger that only remembers successes "
                    "cannot explain what went wrong.",
                ]
            )

        return "\n".join(lines)


def build_report(store: StateStore, task_id: TaskId, plan_id: PlanId) -> EvidenceReport:
    """Assemble the final report from `tool_events` and `evidence`.

    Raises:
        KeyError: no such task. A report for a task that does not exist would
            be a fabrication with a plausible shape, which is the worst kind.
    """
    task = store.get_task(task_id)
    if task is None:
        raise KeyError(f"no task {task_id!r}")

    verdict = CompletionGate(store, task_id).check_task(plan_id)
    titles: Mapping[StepId, str] = {step.step_id: step.title for step in store.get_steps(plan_id)}

    events = store.tool_events_for(task_id)
    by_id = {event.event_id: event for event in events}

    entries = tuple(
        EvidenceEntry(
            kind=record.kind,
            tool=by_id[record.source_event_id].tool
            if record.source_event_id in by_id
            else "(unknown)",
            step_title=titles.get(record.step_id, "") if record.step_id else "",
            detail=record.detail,
        )
        for record in store.evidence_for(task_id)
    )

    return EvidenceReport(
        task_id=task_id,
        request=task.request,
        verdict=verdict,
        entries=entries,
        changed_files=_changed_files(events),
        failed_calls=sum(1 for event in events if not event.ok),
    )


def _changed_files(events: Sequence[ToolEventRecord]) -> tuple[str, ...]:
    """Files a successful patch actually touched, in first-touched order.

    Read off the recorded arguments of successful `file.patch` executions
    rather than from a running list the agent maintains, because a list the
    agent maintains is a list the agent can be wrong about.
    """
    paths: list[str] = []
    for event in events:
        if event.tool != "file.patch" or not event.ok:
            continue
        try:
            arguments = json.loads(event.arguments_json or "{}")
        except json.JSONDecodeError:  # pragma: no cover - defensive
            continue
        path = arguments.get("path") if isinstance(arguments, dict) else None
        if isinstance(path, str) and path and path not in paths:
            paths.append(path)
    return tuple(paths)


def next_after_completion_gate(verdict: TaskCompletion, *, can_replan: bool) -> AgentState:
    """Where COMPLETION_GATE goes, given its own result.

    Refusal does not end the run: a task that fell short with re-plans left
    goes back to planning. It ends as BLOCKED only when there is nothing left
    to try, so "we gave up" is distinguishable from "we ran out of budget".
    """
    if verdict.satisfied:
        return AgentState.FINAL_REPORT
    return AgentState.REPLAN if can_replan else AgentState.BLOCKED


__all__ = [
    "TASK_COMPLETE",
    "ClaimVerdict",
    "CompletionGate",
    "EvidenceEntry",
    "EvidenceReport",
    "StepVerdict",
    "TaskCompletion",
    "build_report",
    "known_claims",
    "next_after_completion_gate",
    "validate_claim",
]
