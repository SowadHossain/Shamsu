"""Turning a proposed plan into executable, gated state.

A model emits an `ImplementationPlan`: prose titles, prose acceptance criteria,
and prose descriptions of what proof it thinks each step needs. None of that is
executable, and none of it is trustworthy on its own. This module is the seam
where a proposal becomes rows in `plans` and `plan_steps` — with the parts that
govern completion decided by the runtime rather than by the model.

Three rules shape it.

**The model may raise its own bar, never lower it.** `required_evidence` on a
step is the union of what the model asked for and what the runtime demands for
that kind of step. A change step always requires `FILE_CHANGED` and
`GIT_DIFF_REVIEWED`, whatever the plan says. The only way to get a weaker
requirement is to declare the step `investigate`, which also strips every
mutating tool from its allowlist — so weakening the gate costs the ability to
write. That is a trade a model can be trusted with; "please require less proof"
is not.

**Free-text evidence phrases are mapped, not adopted.** The model writes
"targeted authentication tests pass"; the runtime decides that means
`TESTS_PASSED`. A phrase that maps to nothing is not dropped and not guessed
at: it survives as an acceptance criterion, where it is visible prose rather
than a mechanical requirement nothing can satisfy.

**Re-planning supersedes, it does not edit.** A new plan is a new version with
its own steps; the old one keeps its rows and gains a `superseded_by` pointer.
Completed work is *not* copied forward — evidence rows key to the step that
earned them, and duplicating a step under a new id would silently orphan its
proof. What crosses the boundary is a list of finished step titles, so the
next plan can be told what not to redo.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from shamsu.interfaces.enums import EvidenceKind, Risk, StepOutcome
from shamsu.interfaces.ids import PlanId, StepId, TaskId
from shamsu.models.contracts import ImplementationPlan, PlanStepProposal
from shamsu.runtime.limits import DEFAULT_LIMITS, ExecutionLimits
from shamsu.security.paths import PathEscape, PathSandbox
from shamsu.state.records import PlanRecord, PlanStepRecord, TaskRecord, new_id
from shamsu.state.store import StateStore
from shamsu.verification.evidence import GateResult, check_completion

#: Tools a step may use, by kind. Assigned by the runtime; the model does not
#: propose its own allowlist, because a step that could name its own tools
#: could name `file.patch` while calling itself an investigation.
READ_ONLY_TOOLS: tuple[str, ...] = (
    "project.inspect",
    "code.search",
    "file.read",
    "git.inspect",
)
CHANGE_TOOLS: tuple[str, ...] = (
    *READ_ONLY_TOOLS,
    "file.patch",
    "test.run",
    "git.checkpoint",
)

#: The floor a change step cannot go below (plan §25: "File modified →
#: successful patch and Git diff"). Required whether or not the plan asked.
CHANGE_FLOOR: frozenset[EvidenceKind] = frozenset(
    {EvidenceKind.FILE_CHANGED, EvidenceKind.GIT_DIFF_REVIEWED}
)

#: Free-text phrase → evidence kind, most specific first. The first match wins:
#: one phrase describes one piece of proof. Order is load-bearing — "smoke
#: test" contains "test", so it must be tried before the generic test pattern.
_VOCABULARY: tuple[tuple[re.Pattern[str], EvidenceKind], ...] = (
    (re.compile(r"\bsmoke\b"), EvidenceKind.SMOKE_TEST_PASSED),
    (
        re.compile(r"\bhealth[\s-]?check|\bhealthy\b|\bservice (is )?up\b"),
        EvidenceKind.HEALTH_CHECK_PASSED,
    ),
    (re.compile(r"\bmigrat"), EvidenceKind.MIGRATION_APPLIED),
    (re.compile(r"\bschema\b"), EvidenceKind.SCHEMA_VERIFIED),
    (re.compile(r"\bdiff\b"), EvidenceKind.GIT_DIFF_REVIEWED),
    (re.compile(r"\bcheckpoint\b|\bcommit(ted)?\b"), EvidenceKind.CHECKPOINT_CREATED),
    (re.compile(r"\blint|\bruff\b|\bflake8\b|\bformat"), EvidenceKind.LINT_PASSED),
    (
        re.compile(r"\btype[\s-]?check|\bmypy\b|\btypes? (are )?correct"),
        EvidenceKind.TYPECHECK_PASSED,
    ),
    (re.compile(r"\bbuild\b|\bcompiles?\b|\bbundle"), EvidenceKind.BUILD_SUCCEEDED),
    (re.compile(r"\btests?\b|\bpytest\b|\bspecs?\b|\bsuite\b"), EvidenceKind.TESTS_PASSED),
    (
        re.compile(r"\bpatch(ed)?\b|\bfiles? (is |are )?(changed|modified|edited|created)"),
        EvidenceKind.FILE_CHANGED,
    ),
)


class PlanRejected(Exception):
    """A proposed plan cannot be persisted.

    Raised rather than returned so an invalid plan has no path into the store.
    A caller that wants to inspect the problems first calls `validate_plan`.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True)
class EvidenceMapping:
    """What a step's free-text evidence phrases resolved to."""

    kinds: frozenset[EvidenceKind]
    unrecognised: tuple[str, ...]


def map_required_evidence(phrases: Sequence[str]) -> EvidenceMapping:
    """Resolve prose evidence requirements onto real evidence kinds.

    Deliberately conservative: a phrase matching nothing in the vocabulary is
    reported as unrecognised rather than mapped to a nearest neighbour. A wrong
    mapping produces a gate that opens on the wrong proof, which is worse than
    a gate that never mentions the phrase at all.
    """
    kinds: set[EvidenceKind] = set()
    unrecognised: list[str] = []

    for phrase in phrases:
        text = phrase.strip()
        if not text:
            continue
        for pattern, kind in _VOCABULARY:
            if pattern.search(text.lower()):
                kinds.add(kind)
                break
        else:
            unrecognised.append(text)

    return EvidenceMapping(kinds=frozenset(kinds), unrecognised=tuple(unrecognised))


def evidence_floor(kind: str) -> frozenset[EvidenceKind]:
    """The minimum evidence the runtime requires for a step of this kind."""
    return CHANGE_FLOOR if kind == "change" else frozenset()


def allowed_tools_for(kind: str) -> tuple[str, ...]:
    """The tools a step of this kind may reach."""
    return CHANGE_TOOLS if kind == "change" else READ_ONLY_TOOLS


@dataclass(frozen=True)
class PlanValidation:
    """Whether a proposal can be executed, and what is questionable about it.

    `problems` block persistence; `notes` do not. The split is deliberate: a
    plan naming a path outside the workspace is unexecutable and must be
    refused, while a plan with a vague acceptance criterion is merely weak, and
    refusing every weak plan would leave the runtime with nothing to do.
    """

    ok: bool
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def validate_plan(
    plan: ImplementationPlan,
    *,
    sandbox: PathSandbox | None = None,
    files_seen: Sequence[str] = (),
) -> PlanValidation:
    """Check a proposal before it becomes state.

    The path check is the one with teeth: a step declaring it will edit
    `../../etc/passwd` is caught here, at plan time, rather than when the patch
    tool refuses three decisions later with the run's budget already spent.
    """
    problems: list[str] = []
    notes: list[str] = []
    seen = set(files_seen)

    titles: set[str] = set()
    for index, step in enumerate(plan.steps, start=1):
        label = f"step {index} ({step.title!r})"

        if sandbox is not None:
            for path in step.files:
                try:
                    sandbox.resolve(path)
                except PathEscape:
                    problems.append(f"{label} names {path!r}, which is outside the workspace")

        lowered = step.title.strip().lower()
        if lowered in titles:
            notes.append(f"{label} repeats an earlier step title; the run log will be ambiguous")
        titles.add(lowered)

        if step.kind == "change" and not step.acceptance_criteria:
            notes.append(f"{label} changes code but defines no acceptance criteria")
        if step.kind == "change" and not step.files:
            notes.append(f"{label} changes code but names no files")

    if files_seen:
        invented = [path for path in plan.grounded_in if path not in seen]
        if invented:
            problems.append(f"plan cites files that were never read: {', '.join(sorted(invented))}")

    return PlanValidation(ok=not problems, problems=tuple(problems), notes=tuple(notes))


@dataclass(frozen=True)
class MaterialisedPlan:
    """A proposal turned into records, with what the mapping could not resolve."""

    record: PlanRecord
    steps: tuple[PlanStepRecord, ...]
    unmapped_evidence: tuple[str, ...] = ()

    @property
    def plan_id(self) -> PlanId:
        return self.record.plan_id


def materialise(
    task_id: TaskId,
    plan: ImplementationPlan,
    *,
    version: int = 1,
) -> MaterialisedPlan:
    """Build plan and step records from a proposal. Pure; touches no store.

    Separated from persistence so the interesting decisions — the evidence
    floor, the tool allowlist, the approval rule — can be exercised without a
    database.
    """
    plan_id = PlanId(new_id())
    steps: list[PlanStepRecord] = []
    unmapped: list[str] = []

    for ordinal, proposal in enumerate(plan.steps):
        mapping = map_required_evidence(proposal.required_evidence)
        unmapped.extend(mapping.unrecognised)

        # Union, never replacement. A plan proposing no evidence at all still
        # gets the floor for its kind.
        required = mapping.kinds | evidence_floor(proposal.kind)

        steps.append(
            PlanStepRecord(
                step_id=StepId(new_id()),
                plan_id=plan_id,
                ordinal=ordinal,
                title=proposal.title,
                inputs=proposal.files,
                outputs=(),
                constraints=(proposal.intent,) if proposal.intent else (),
                allowed_tools=allowed_tools_for(proposal.kind),
                acceptance_criteria=_criteria(proposal, mapping),
                required_evidence=tuple(sorted(required, key=lambda kind: kind.value)),
                risk=Risk(proposal.risk),
                approval_required=_needs_approval(proposal),
            )
        )

    return MaterialisedPlan(
        record=PlanRecord(
            plan_id=plan_id,
            task_id=task_id,
            version=version,
            summary=plan.summary,
        ),
        steps=tuple(steps),
        unmapped_evidence=tuple(dict.fromkeys(unmapped)),
    )


def _criteria(proposal: PlanStepProposal, mapping: EvidenceMapping) -> tuple[str, ...]:
    """Acceptance criteria, plus any evidence phrase that mapped to nothing.

    An unmapped phrase is still a statement about what "done" means. Keeping it
    as prose loses its mechanical force but not its content, and a criterion
    the runtime cannot check is at least one a reviewer can read.
    """
    return tuple(dict.fromkeys([*proposal.acceptance_criteria, *mapping.unrecognised]))


def _needs_approval(proposal: PlanStepProposal) -> bool:
    """The runtime decides approval, not the plan.

    `PlanStepProposal` has no approval field on purpose: a model that could
    declare its own step pre-approved would route around the only human gate in
    the system.
    """
    return Risk(proposal.risk) in (Risk.HIGH, Risk.CRITICAL)


@dataclass(frozen=True)
class PlanProgress:
    """How far through a plan a task is."""

    total: int
    completed: int
    remaining: int

    @property
    def done(self) -> bool:
        return self.total > 0 and self.completed == self.total


class Planner:
    """Persists plans, hands out steps, and gates their completion.

    A bounded controller, not a loop: every method answers one question and
    returns. The runtime decides what to do with the answer.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        limits: ExecutionLimits | None = None,
        sandbox: PathSandbox | None = None,
    ) -> None:
        self._store = store
        self._limits = limits or DEFAULT_LIMITS
        self._sandbox = sandbox

    # -- creating and replacing plans --------------------------------------

    def create(
        self,
        task: TaskRecord,
        plan: ImplementationPlan,
        *,
        files_seen: Sequence[str] = (),
    ) -> MaterialisedPlan:
        """Validate, materialise, and persist a first plan.

        Raises:
            PlanRejected: the proposal has a fatal problem. Nothing is written.
        """
        return self._persist(task, plan, version=1, files_seen=files_seen)

    def replan(
        self,
        task: TaskRecord,
        plan: ImplementationPlan,
        *,
        files_seen: Sequence[str] = (),
    ) -> MaterialisedPlan:
        """Supersede the current plan with a new version.

        The re-plan budget is checked *before* anything is written, so a task at
        its limit ends with its existing plan intact rather than with a
        half-applied replacement.

        Raises:
            LimitExceeded: the task has used its re-plan budget.
            PlanRejected: the new proposal has a fatal problem.
        """
        self._limits.check_replans(task.replan_count)

        previous = self._store.latest_plan(task.task_id)
        version = previous.version + 1 if previous else 1

        materialised = self._persist(task, plan, version=version, files_seen=files_seen)

        if previous is not None:
            self._store.supersede_plan(previous.plan_id, materialised.plan_id)

        self._store.save_task(
            task.model_copy(
                update={
                    "plan_id": materialised.plan_id,
                    "current_step_id": None,
                    "replan_count": task.replan_count + 1,
                }
            )
        )
        return materialised

    def _persist(
        self,
        task: TaskRecord,
        plan: ImplementationPlan,
        *,
        version: int,
        files_seen: Sequence[str],
    ) -> MaterialisedPlan:
        validation = validate_plan(plan, sandbox=self._sandbox, files_seen=files_seen)
        if not validation.ok:
            raise PlanRejected(validation.problems)

        materialised = materialise(task.task_id, plan, version=version)
        self._store.create_plan(materialised.record, materialised.steps)

        if version == 1:
            self._store.save_task(
                task.model_copy(update={"plan_id": materialised.plan_id, "current_step_id": None})
            )
        return materialised

    # -- executing steps ---------------------------------------------------

    def next_step(self, plan_id: PlanId) -> PlanStepRecord | None:
        """The lowest-ordinal step with no outcome yet, or None when done.

        A step whose gate refused keeps `outcome=None` and is therefore handed
        back — "not proven done" and "not attempted" are the same thing to the
        scheduler, and the attempt counter is what bounds the retrying.
        """
        for step in self._store.get_steps(plan_id):
            if step.outcome is None:
                return step
        return None

    def begin_step(self, step: PlanStepRecord) -> PlanStepRecord:
        """Record an attempt on a step.

        Raises:
            LimitExceeded: this step has already used its repair budget. The
                count is incremented only on success, so a refused begin does
                not consume the very budget it is reporting on.
        """
        if step.attempts > 0:
            self._limits.check_repairs(step.attempts - 1)
        updated = step.model_copy(update={"attempts": step.attempts + 1})
        self._store.save_step(updated)
        return updated

    def close_step(
        self, step: PlanStepRecord, verified: frozenset[EvidenceKind]
    ) -> tuple[PlanStepRecord, GateResult]:
        """Apply the step completion gate.

        `required ⊆ verified`, evaluated against evidence rows rather than
        against anything the model said. On refusal the step is left pending —
        the gate does not decide that a step is unrepairable, only that it is
        not finished.
        """
        result = check_completion(step.required_evidence, verified)
        if not result.satisfied:
            return step, result

        updated = step.model_copy(update={"outcome": StepOutcome.PASS})
        self._store.save_step(updated)
        return updated, result

    def fail_step(self, step: PlanStepRecord, outcome: StepOutcome) -> PlanStepRecord:
        """Close a step with a non-passing outcome.

        Separate from `close_step` because a passing outcome is earned from
        evidence while every other outcome is a runtime decision — and having
        one method that could write `PASS` from a caller's argument would put a
        forgeable path right next to the gate.
        """
        if outcome is StepOutcome.PASS:
            raise ValueError("use close_step; a passing outcome must come from the evidence gate")
        updated = step.model_copy(update={"outcome": outcome})
        self._store.save_step(updated)
        return updated

    # -- reporting ---------------------------------------------------------

    def progress(self, plan_id: PlanId) -> PlanProgress:
        steps = self._store.get_steps(plan_id)
        completed = sum(1 for step in steps if step.outcome is StepOutcome.PASS)
        return PlanProgress(total=len(steps), completed=completed, remaining=len(steps) - completed)

    def completed_titles(self, plan_id: PlanId) -> tuple[str, ...]:
        """Titles of steps that passed their gate.

        What crosses the boundary into a re-plan. Titles rather than records
        because the new plan gets new steps; carrying the records themselves
        would duplicate rows whose evidence keys to the old ones.
        """
        return tuple(
            step.title
            for step in self._store.get_steps(plan_id)
            if step.outcome is StepOutcome.PASS
        )


def render_plan_summary(
    plan: PlanRecord, steps: Sequence[PlanStepRecord], *, current: StepId | None = None
) -> str:
    """The compact plan view that enters a prompt (plan §21).

    Only the summary and step titles — not inputs, not constraints, not
    evidence. The full step is rendered separately for the current step alone,
    because a twelve-step plan rendered in full would consume the source-code
    budget that makes the current step answerable.
    """
    lines = [plan.summary, ""]
    for step in steps:
        marker = "✓" if step.outcome is StepOutcome.PASS else " "
        pointer = " ← current" if current is not None and step.step_id == current else ""
        lines.append(f"[{marker}] {step.ordinal + 1}. {step.title}{pointer}")
    return "\n".join(lines).strip()


def render_step(step: PlanStepRecord) -> str:
    """The current step, in full, for the hot section of a frame."""
    lines = [f"Step {step.ordinal + 1}: {step.title}"]
    if step.constraints:
        lines.append("Intent: " + " ".join(step.constraints))
    if step.inputs:
        lines.append("Files: " + ", ".join(step.inputs))
    if step.required_evidence:
        names = ", ".join(kind.value for kind in step.required_evidence)
        lines.append(f"This step is only complete once these are verified: {names}")
    return "\n".join(lines)


__all__ = [
    "CHANGE_FLOOR",
    "CHANGE_TOOLS",
    "READ_ONLY_TOOLS",
    "EvidenceMapping",
    "MaterialisedPlan",
    "PlanProgress",
    "PlanRejected",
    "PlanValidation",
    "Planner",
    "allowed_tools_for",
    "evidence_floor",
    "map_required_evidence",
    "materialise",
    "render_plan_summary",
    "render_step",
    "validate_plan",
]
