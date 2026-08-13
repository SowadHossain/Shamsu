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
    "file.list",
    "file.read",
    "git.inspect",
)
CHANGE_TOOLS: tuple[str, ...] = (
    *READ_ONLY_TOOLS,
    "file.patch",
    "test.run",
    "check.run",
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


#: Verbs that mean a step only looks at things. Anchored to the start of the
#: title, because a step *begins* with what it does.
_INVESTIGATIVE = re.compile(
    r"^(understand|review|analys|analyz|examine|inspect|investigat|identif|determin"
    r"|assess|explor|read|locate|find|search|survey|audit|research|study|clarif"
    r"|gather|collect|map|list|trace|diagnos|confirm|evaluat|consider|plan)\w*\b"
)

#: Verbs that mean a step intends to write. Searched anywhere in the text: a
#: step titled "Understand and fix the login bug" is a change step, and the
#: word that settles it is not at the front.
#:
#: Two rules make this reliable where a naive verb list is not.
#:
#: **Whole words, not `verb\w*` stems.** The stem form matched nouns:
#: `configur\w*` claimed "Read the configuration" as a change step, which is
#: precisely the misclassification this module exists to prevent.
#:
#: **A determiner in front makes it a noun.** English does the disambiguation
#: for us — "examine the patch format" is a noun and "review the code and patch
#: the parser" is a verb, and the word before it is what says so. Without this,
#: every verb that doubles as a common noun (patch, build, fix, update, change)
#: had to be dropped from the list entirely, which lost the real verb uses too.
#:
#: **No third-person `-s` forms.** A title *starting* with a mutating verb
#: never reaches the investigative branch anyway, because `_INVESTIGATIVE` is
#: anchored — so this pattern only has to catch mutating verbs in non-initial
#: position. Step titles are imperative ("Add a route"), which means a
#: non-initial `-s` form is almost always *describing* existing code rather
#: than commanding a change: "Analyse how the planner builds a frame" is an
#: investigation, and `builds` is the wrong word to read as intent.
_MUTATING = re.compile(
    r"(?<!\bthe )(?<!\ba )(?<!\bthis )(?<!\bits )(?<!\bany )"
    r"\b("
    r"add(?:ing|ed)?|creat(?:e|ing|ed)|writ(?:e|ing)|wrote"
    r"|implement(?:ing|ed)?|fix(?:ing|ed)?|updat(?:e|ing|ed)"
    r"|modif(?:y|ying|ied)|edit(?:ing|ed)?|chang(?:e|ing|ed)"
    r"|remov(?:e|ing|ed)|delet(?:e|ing|ed)|renam(?:e|ing|ed)"
    r"|refactor(?:ing|ed)?|patch(?:ing|ed)?|migrat(?:e|ing|ed)"
    r"|install(?:ing|ed)?|configur(?:e|ing|ed)|wir(?:e|ing|ed)"
    r"|replac(?:e|ing|ed)|introduc(?:e|ing|ed)|extend(?:ing|ed)?"
    r"|build(?:ing)?|built|set up|setting up|generat(?:e|ing|ed)"
    r"|appl(?:y|ying|ied)|rewrit(?:e|ing)|rewrote"
    r"|correct(?:ing|ed)?|repair(?:ing|ed)?"
    r")\b"
)


def effective_kind(proposal: PlanStepProposal, *, read_only: bool = False) -> str:
    """The kind the runtime will actually execute a step as.

    `read_only` is plan mode, and it is absolute: every step becomes
    `investigate`, which strips every mutating tool from its allowlist. The
    model is not asked to refrain from editing — it is not given anything that
    can edit. That is the difference between a mode and a prompt instruction,
    and it is the whole reason the setting is worth having.

    `PlanStepProposal.kind` defaults to `change`, which is the stricter option
    on the axis the default was chosen for — a change step cannot lower its
    evidence floor. But strictness on that axis is not free on the other one: a
    change step *must* produce `file_changed` and `git_diff_reviewed`, so a
    step that was never going to write anything becomes unsatisfiable rather
    than merely well-guarded. The gate then refuses forever, correctly and
    uselessly.

    That is not hypothetical. A small model asked to plan a greeting proposed a
    single step titled "Understand the task", omitted `kind`, and the run ended
    BLOCKED on missing `file_changed` — evidence no honest execution of that
    step could ever have produced.

    So a `change` proposal is re-read as `investigate` when its title opens with
    an investigative verb and nothing in the title or intent says it will write.
    The reclassification is *safe by construction* — `investigate` strips every
    mutating tool — so the failure mode of being wrong here is a step that
    cannot edit, which the repair path already reports honestly. Being wrong in
    the other direction produces a run that can never finish.

    **The verb outranks the file list, and that ordering was earned.** An
    earlier version checked `proposal.files` first, on the theory that a step
    naming files intends to edit them. The §31.1 evaluation showed otherwise:
    qwen2.5-coder:7b planned *"Locate the add function"* with `files:
    ["calc.py"]` and *"Locate the slugify function"* with `files: ["slug.py"]`,
    because the file is where it intends to *look*. Both became change steps
    carrying `file_changed` + `git_diff_reviewed`, so a plan's opening
    orientation step demanded a patch and a diff review before the real work
    began — and since evidence is scoped per step, every later step had to earn
    the same pair again. Two of the first three eval tasks did the job
    correctly and still ended BLOCKED on that.

    Naming a file you will read is not naming a file you will write, and the
    verb is what distinguishes them. `files` still decides when the title says
    nothing either way.
    """
    if read_only or proposal.kind == "investigate":
        return "investigate"

    text = f"{proposal.title} {proposal.intent}".strip().lower()
    if _MUTATING.search(text):
        return "change"
    if _INVESTIGATIVE.match(text):
        return "investigate"

    # The title said nothing either way. A named file is then the best signal
    # available, and `change` is the safe default when there is none.
    return "change"


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


#: Phrasings that mean a *request* wants something edited. Deliberately a
#: separate pattern from `_MUTATING`, which reads step titles: a title is an
#: imperative written by a model, while a request is prose written by a person
#: and states requirements as often as actions. "charge() must raise a
#: ValueError when amount is negative" contains no verb from `_MUTATING` and is
#: unmistakably a change request.
#:
#: Tuned to over-detect. Deciding a request wants an edit when it only wanted
#: an explanation costs a rejected plan and a re-plan; deciding the reverse
#: lets an all-investigate plan report success with nothing done.
_CHANGE_REQUEST = re.compile(
    # A copula in front makes the word a predicate, not an instruction:
    # "confirm add() is correct" describes a state, "correct the typo" asks for
    # work. Same trick as `_MUTATING`'s determiner guard, one part of speech up.
    r"(?<!\bis )(?<!\bare )(?<!\bwas )(?<!\bwere )(?<!\bbe )(?<!\bbeen )"
    r"(?<!\blooks )(?<!\bseems )(?<!\bstays )"
    # And a determiner in front makes it a noun, exactly as in `_MUTATING`:
    # "is the build passing?" asks about a build, it does not ask for one.
    r"(?<!\bthe )(?<!\ba )(?<!\bthis )(?<!\bits )(?<!\bany )(?<!\bour )"
    r"\b("
    # Direct actions.
    r"add|creat\w*|writ\w*|implement\w*|fix\w*|updat\w*|modif\w*|edit\w*"
    r"|chang\w*|remov\w*|delet\w*|renam\w*|refactor\w*|patch\w*|migrat\w*"
    r"|install\w*|configur\w*|wire|replac\w*|introduc\w*|extend\w*|build"
    r"|set up|generat\w*|appl\w*|rewrit\w*|correct\w*|repair\w*"
    # Requirements. A person states what the code must do at least as often as
    # they state what to do to it.
    #
    # Deliberately excludes `support`, `handle`, `allow`, `prevent`, `enable`
    # and `improve`. Each is as often a question about a capability as a demand
    # for one — "do you support typescript" and "does the parser handle
    # unicode" are questions, and reading them as change requests sent them
    # down a path that can only edit and prove. The requirement sense is
    # carried well enough by `must`, `should` and `needs to`.
    r"|must|should|needs? to|has to|make|ensure|reject\w*|raise|validat\w*|clean up"
    # Not when it is a symbol name. "look at add()" is a question about a
    # function that happens to be called `add`, not an instruction to add
    # something — and `\b` alone cannot tell those apart.
    r")\b(?!\s*\()"
)


def asks_for_a_change(request: str) -> bool:
    """Whether the request wants something edited, rather than explained."""
    return bool(_CHANGE_REQUEST.search(request.strip().lower()))


def validate_plan(
    plan: ImplementationPlan,
    *,
    sandbox: PathSandbox | None = None,
    files_seen: Sequence[str] = (),
    request: str = "",
    read_only: bool = False,
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

        kind = effective_kind(step)
        if kind != step.kind:
            notes.append(
                f"{label} was proposed as a change but names no files and reads as "
                "investigation; it will run read-only"
            )
        if kind == "change" and not step.acceptance_criteria:
            notes.append(f"{label} changes code but defines no acceptance criteria")
        if kind == "change" and not step.files:
            notes.append(f"{label} changes code but names no files")

    if files_seen:
        invented = [path for path in plan.grounded_in if path not in seen]
        if invented:
            problems.append(f"plan cites files that were never read: {', '.join(sorted(invented))}")

    # A plan that only investigates cannot satisfy a request to change
    # something, and — because an `investigate` step requires no evidence — it
    # passes every gate it meets. The §31.1 evaluation caught exactly that: a
    # 7B asked to fix a failing test planned "Inspect the Project Structure",
    # "Identify Dependencies", "Review Failing Tests", all three legitimately
    # read-only, all three trivially satisfied. The run reported completion
    # with the bug untouched — a **false success**, the one outcome the
    # evidence architecture exists to prevent.
    #
    # A problem rather than a note, so it forces a re-plan instead of being
    # reported after the damage. Skipped in read-only mode, where a plan with
    # no change step is the entire point.
    wants_a_change = bool(request) and not read_only and asks_for_a_change(request)
    if wants_a_change and not any(effective_kind(step) == "change" for step in plan.steps):
        problems.append(
            "the request asks for a change but no step in this plan would edit "
            "anything; a plan made only of investigation cannot carry it out"
        )

    return PlanValidation(ok=not problems, problems=tuple(problems), notes=tuple(notes))


@dataclass(frozen=True)
class MaterialisedPlan:
    """A proposal turned into records, with what the mapping could not resolve."""

    record: PlanRecord
    steps: tuple[PlanStepRecord, ...]
    unmapped_evidence: tuple[str, ...] = ()

    #: Titles of steps the runtime executed as `investigate` despite the
    #: proposal saying `change`. Surfaced rather than silent: a step that
    #: quietly lost its ability to write is a confusing run to debug.
    reclassified: tuple[str, ...] = ()

    @property
    def plan_id(self) -> PlanId:
        return self.record.plan_id


def materialise(
    task_id: TaskId,
    plan: ImplementationPlan,
    *,
    version: int = 1,
    read_only: bool = False,
) -> MaterialisedPlan:
    """Build plan and step records from a proposal. Pure; touches no store.

    Separated from persistence so the interesting decisions — the evidence
    floor, the tool allowlist, the approval rule — can be exercised without a
    database.
    """
    plan_id = PlanId(new_id())
    steps: list[PlanStepRecord] = []
    unmapped: list[str] = []
    reclassified: list[str] = []

    for ordinal, proposal in enumerate(plan.steps):
        mapping = map_required_evidence(proposal.required_evidence)
        unmapped.extend(mapping.unrecognised)

        # What the step *is*, which is not always what the proposal called it.
        kind = effective_kind(proposal, read_only=read_only)
        if kind != proposal.kind:
            reclassified.append(proposal.title)

        # Union, never replacement. A plan proposing no evidence at all still
        # gets the floor for its kind.
        required = mapping.kinds | evidence_floor(kind)

        # An investigate step cannot patch, so a mapped requirement for
        # FILE_CHANGED is a requirement it has no tool to satisfy. Dropping it
        # keeps the model's *raised* bar meaningful while refusing to build a
        # gate with no key -- the phrase survives as an acceptance criterion.
        if kind == "investigate":
            required -= CHANGE_FLOOR

        steps.append(
            PlanStepRecord(
                step_id=StepId(new_id()),
                plan_id=plan_id,
                ordinal=ordinal,
                title=proposal.title,
                inputs=proposal.files,
                outputs=(),
                constraints=(proposal.intent,) if proposal.intent else (),
                allowed_tools=allowed_tools_for(kind),
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
        reclassified=tuple(dict.fromkeys(reclassified)),
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
        read_only: bool = False,
    ) -> None:
        self._store = store
        self._limits = limits or DEFAULT_LIMITS
        self._sandbox = sandbox
        self._read_only = read_only

    @property
    def read_only(self) -> bool:
        """Whether every step is forced to `investigate` (plan mode)."""
        return self._read_only

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
        validation = validate_plan(
            plan,
            sandbox=self._sandbox,
            files_seen=files_seen,
            request=task.request,
            read_only=self._read_only,
        )
        if not validation.ok:
            raise PlanRejected(validation.problems)

        materialised = materialise(task.task_id, plan, version=version, read_only=self._read_only)
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
    "asks_for_a_change",
    "effective_kind",
    "evidence_floor",
    "map_required_evidence",
    "materialise",
    "render_plan_summary",
    "render_step",
    "validate_plan",
]
