"""Turning a proposed plan into executable, gated state.

A model emits an `ImplementationPlan`: prose titles, prose acceptance criteria,
and prose descriptions of what proof it thinks each step needs. None of that is
executable, and none of it is trustworthy on its own. This module is the seam
where a proposal becomes rows in `plans` and `plan_steps` — with the parts that
govern completion decided by the runtime rather than by the model.

Three rules shape it.

**The model may raise its own bar, never lower it.** `required_evidence` on a
step is the union of what the model asked for and what the runtime demands for
that kind of step. A change step requires `FILE_CHANGED` and
`GIT_DIFF_REVIEWED`, whatever the plan says. The only way to get a weaker
requirement is to declare the step `investigate`, which also strips every
mutating tool from its allowlist — so weakening the gate costs the ability to
write. That is a trade a model can be trusted with; "please require less proof"
is not.

**But no bar is set where nothing can clear it.** Both the floor and the
model's additions are intersected with `producible`: what this workspace and
this tool set can actually prove. Outside a git repository there is no diff to
review; in a project with no tests there is no suite to pass; and four evidence
kinds have no producing tool at all while the vocabulary above still maps prose
onto them. Requiring any of those does not make the gate stricter, it makes it
unopenable — the run then fails for a reason no execution could have avoided.
What is dropped is reported in `MaterialisedPlan.unsatisfiable_evidence`, never
discarded quietly.

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
from dataclasses import dataclass, replace

from shamsu.agent.mentions import filenames_in
from shamsu.interfaces.enums import EvidenceKind, Risk, StepOutcome
from shamsu.interfaces.ids import PlanId, StepId, TaskId
from shamsu.models.contracts import ImplementationPlan, PlanStepProposal
from shamsu.runtime.limits import DEFAULT_LIMITS, ExecutionLimits
from shamsu.security.paths import PathEscape, PathSandbox, workspace_key
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
    r"|gather|collect|map|list|trace|diagnos|confirm|evaluat|consider|plan"
    # Added from a live plan: "Check for existing Python files" was a *change*
    # step demanding `file_changed`, because `check` was not on this list.
    # Looking for something is not making it.
    r"|check|verif|look|compare|count)\w*\b"
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


def evidence_floor(
    kind: str, *, producible: frozenset[EvidenceKind] | None = None
) -> frozenset[EvidenceKind]:
    """The minimum evidence the runtime requires for a step of this kind.

    `producible` is what this workspace and this tool set can actually prove,
    and the floor is intersected with it. `None` means "do not filter", which
    is what a caller testing the floor itself wants.

    **A requirement nothing can satisfy is not a strict gate, it is a broken
    one.** Outside a git repository `git.inspect` fails on every call, so
    `GIT_DIFF_REVIEWED` becomes a gate with no key — a live run in a plain
    folder spent both repair attempts calling `git.checkpoint`, failed
    identically each time, and blocked. In a project with no tests,
    `TESTS_PASSED` is the same shape of impossibility, and four evidence kinds
    (health check, smoke test, migration, schema) have no producing tool
    anywhere yet while the planner's vocabulary still maps prose onto them.

    Each of those used to need its own patch here. Filtering against what is
    producible handles all of them, including the next one nobody has hit yet.

    Dropping an impossible requirement is an honest reading, not a discount:
    every requirement that *can* be produced still applies, so the edit must
    still happen and still register `FILE_CHANGED`. The run reports the weaker
    guarantee rather than pretending to the stronger one.
    """
    if kind != "change":
        return frozenset()
    return CHANGE_FLOOR if producible is None else CHANGE_FLOOR & producible


#: Steps that intend to take a file away. Deliberately narrow — the cost of
#: missing one is a re-plan, and the cost of matching too eagerly is handing a
#: destructive tool to every step that happens to say "clean up".
_REMOVES = re.compile(
    r"\b(delete|remove|rename|move|drop|deprecate|retire|split)\w*\b", re.IGNORECASE
)

#: Steps whose work is only finished if the project actually runs. Narrow for
#: the same reason `_REMOVES` is: `project.run` executes whatever the project
#: declares, and a step that merely edits a file has no business starting a
#: server to prove it.
_RUNS = re.compile(
    r"\b(run|runs|running|start|starts|serve|serves|boot|boots|launch"
    r"|migrat|smoke|health|deploy)\w*\b",
    re.IGNORECASE,
)


def step_may_run(title: str, criteria: Sequence[str] = ()) -> bool:
    """Whether this step's own words say it needs to run the project.

    Read from the acceptance criteria as well as the title, because "the app
    starts" is how a criterion says it and "Fix the import error" is how the
    title does — the requirement lives in the first and the action in the
    second.
    """
    return bool(_RUNS.search(" ".join([title, *criteria])))


def allowed_tools_for(
    kind: str, *, may_remove: bool = False, may_run: bool = False
) -> tuple[str, ...]:
    """The tools a step of this kind may reach.

    **`file.remove` is granted per step, not globally.** Adding it to every
    change step measurably degraded the agent: the §31.1 suite went from 5/7 to
    a steady 3/7, with call counts collapsing across the board and one task
    taking zero actions at all. Nothing about the tool is wrong — it is the
    tenth entry in a list a 7B has to discriminate among on every single turn,
    and the eleventh way to answer "what next?".

    That is the scaffold-size effect, measured rather than assumed: little-coder
    ships four tools and works; capability surface is not free, and the model
    pays for it on turns that had no use for it.

    So a step gets the destructive tool when its own title says it destroys
    something, and not otherwise. The capability is intact; the tax is not
    levied on every turn.

    `project.run` is granted on the same terms and for the same reason. It is
    the only tool that can prove a program starts — the gap that made four
    evidence kinds unproducible — and it is also the one that executes whatever
    the project declares. A step that edits a docstring should not be offered
    it, and a step whose acceptance criterion is "the app starts" cannot finish
    without it.
    """
    if kind != "change":
        return READ_ONLY_TOOLS

    tools = CHANGE_TOOLS
    if may_remove:
        tools += ("file.remove",)
    if may_run:
        tools += ("project.run",)
    return tools


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


def _exists_in_workspace(path: str, sandbox: PathSandbox | None) -> bool:
    """Whether a cited path is a real file here.

    Without a sandbox there is no workspace to ask, and the honest answer is
    then "assume it exists" — refusing a plan on the strength of a check that
    could not run would be the same false rejection in a different place.
    """
    if sandbox is None:
        return True
    try:
        return sandbox.resolve(path).exists()
    except PathEscape:
        # Outside the workspace. Already reported as a problem by the per-step
        # path check above, and definitely not something to accept here.
        return False


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
        # **Invented, not merely unread.** This check exists to stop a plan
        # being grounded in files that do not exist — the hallucinated citation
        # that makes a plan unexecutable. It used to refuse any path the
        # investigation had not opened, and that is a different and much larger
        # set: a §31.1 task was rejected three times, and blocked having run no
        # tool at all, for citing `test_payments.py` — a real file, sitting in
        # the workspace, correctly identified as relevant. The investigation
        # simply had not happened to read it.
        #
        # A file that exists is evidence the model knows the repository, not
        # evidence it is making things up. Only a path that is neither read nor
        # present is a fabrication.
        invented = [
            path
            for path in plan.grounded_in
            if path not in seen and not _exists_in_workspace(path, sandbox)
        ]
        if invented:
            problems.append(f"plan cites files that do not exist: {', '.join(sorted(invented))}")

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

    # A change step that names no file is a gate with nothing behind it. It
    # requires FILE_CHANGED, and nothing in it says which file would change, so
    # the model is left to pick — and in a live PRD build one picked `PRD.md`
    # and edited the specification it was supposed to be implementing.
    #
    # Naming files is also what makes a plan tractable. `coalesce_by_file` can
    # only merge steps whose targets match, so "implement the add
    # functionality", "implement the list functionality" and four more like them
    # stayed six separate steps against one script, each owing its own proof.
    #
    # Rejected rather than repaired: the runtime cannot invent the missing
    # filename without guessing, and a re-plan carrying this reason is exactly
    # the recovery the rejection path exists for.
    unnamed = [
        step.title
        for step in plan.steps
        if effective_kind(step, read_only=read_only) == "change" and not step.files
    ]
    if unnamed and not read_only:
        listed = "; ".join(unnamed[:4])
        problems.append(
            f"these steps would change something but name no file: {listed}. "
            "Every step that edits must list the file(s) it edits in `files`, "
            "and a step whose work is not a file change is not a step"
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

    #: Evidence the plan asked for that nothing in this workspace can produce.
    #: Dropped from the requirements and reported here, because a run that
    #: silently proves less than it was asked to prove is exactly the kind of
    #: quiet discount the evidence architecture exists to prevent — the
    #: requirement is gone, so the fact that it is gone has to be visible.
    unsatisfiable_evidence: tuple[EvidenceKind, ...] = ()

    #: Titles of steps removed by `drop_unexecutable_steps`. Reported for the
    #: same reason as `unsatisfiable_evidence`: the plan the user is shown must
    #: account for every step the model proposed, including the ones that could
    #: not be run.
    dropped_steps: tuple[str, ...] = ()

    @property
    def plan_id(self) -> PlanId:
        return self.record.plan_id


#: Suffixes that name a *specification*, not a target. Kept in step with
#: `tools/documents.EXTRACTABLE`; duplicated as a literal set rather than
#: imported so `agent/` does not depend on `tools/`.
_DOCUMENTS = frozenset({".docx", ".pdf", ".xlsx", ".pptx", ".doc", ".odt"})


def recover_named_files(plan: ImplementationPlan) -> ImplementationPlan:
    """Fill in `files` from a step's own words when it left the field empty.

    A live incremental build died here. Asked to "Create manage.py", the 7B
    proposed one step titled **"Create manage.py File"** with `files: []`, and
    `validate_plan` refused it three times — *"these steps would change
    something but name no file"* — so a one-file task blocked without running a
    single tool.

    The filename was in the title the whole time. Refusing a plan for putting a
    fact in the wrong field discards work the model actually did, and this
    repository has the scar tissue to prove how expensive that is: the §31.1
    suite lost tasks to a tool layer that rejected *correct* calls on
    technicalities.

    Strictly additive and only from the step's own text — nothing is invented,
    and a step that named files keeps exactly what it named. Recovered names are
    filtered through `editable_files` so this can never reintroduce the
    specification document that `strip_documents` just removed.
    """
    return plan.model_copy(
        update={
            "steps": tuple(
                step
                if step.files
                else step.model_copy(
                    update={"files": editable_files(filenames_in(f"{step.title} {step.intent}"))}
                )
                for step in plan.steps
            )
        }
    )


def drop_unexecutable_steps(plan: ImplementationPlan) -> tuple[ImplementationPlan, tuple[str, ...]]:
    """Remove change steps that name no file, and report which were removed.

    Run *after* `recover_named_files`, so a step whose filename was merely in
    the wrong field has already been rescued. What is left is a step that
    names no target anywhere in its own text — and a small model emits these
    constantly as procedural filler: *"Navigate to the project directory"*,
    *"Open a terminal"*, *"Install the dependencies"*.

    Such a step cannot be executed. It requires `FILE_CHANGED`, nothing says
    which file, and the runtime has no target to scope the evidence to. The
    previous behaviour — refuse the whole plan and re-plan — threw away every
    *good* step alongside it, and a fresh OpenBazaar build blocked on exactly
    that: one junk step next to a perfectly executable "create
    openbazaar/settings.py".

    This is not the guessing the rejection path was written to avoid. Nothing
    is invented; an unexecutable step is removed and named. The plan is
    returned untouched when dropping would empty it, so `validate_plan` still
    refuses a plan made entirely of filler rather than silently running
    nothing.
    """
    keep = tuple(step for step in plan.steps if step.files or effective_kind(step) != "change")
    if not keep or len(keep) == len(plan.steps):
        return plan, ()
    dropped = tuple(step.title for step in plan.steps if step not in keep)
    return plan.model_copy(update={"steps": keep}), dropped


def strip_documents(plan: ImplementationPlan) -> ImplementationPlan:
    """The plan with specification documents removed from every step's `files`."""
    return plan.model_copy(
        update={
            "steps": tuple(
                step.model_copy(update={"files": editable_files(step.files)}) for step in plan.steps
            )
        }
    )


def editable_files(files: Sequence[str]) -> tuple[str, ...]:
    """A step's `files`, minus anything that is a document rather than code.

    A live PRD build made the cost of not doing this vivid. The workspace held
    exactly one file — `OpenBazaar_Marketplace_PRD.docx` — so every step the
    model proposed named it, and two things followed. `file.patch` spent the
    run trying to edit a zip archive; and `coalesce_by_file`, seeing four steps
    with identical `files`, merged them into one step titled *"Design System
    Architecture; develop frontend; develop backend; integrate frontend and
    backend"* — the entire project as a single unit of work.

    Merging is justified by "same file, so one unit of work". A specification
    is not a unit of work, so it must not license the merge. The document stays
    available to read; it stops being something a step claims to change.
    """
    return tuple(path for path in files if not _is_document(path))


def _is_document(path: str) -> bool:
    lowered = workspace_key(path).lower()
    return any(lowered.endswith(suffix) for suffix in _DOCUMENTS)


def coalesce_by_file(
    proposals: Sequence[PlanStepProposal], *, read_only: bool = False
) -> tuple[PlanStepProposal, ...]:
    """Merge adjacent change steps that target the same file.

    A plan is a decomposition of work, and the right grain depends on how much
    the executor produces per turn. A 7B writes a *whole file* per turn, so a
    plan reading

        2. Define the TaskList class      → tasks.py
        3. Implement the add method       → tasks.py
        4. Implement the all method       → tasks.py
        5. Implement the complete method  → tasks.py

    describes four steps and one unit of work. That is not a hypothetical: in
    a live build the model wrote all three methods in step 2, correctly and
    completely — and steps 3, 4 and 5 then failed, each demanding its own
    `FILE_CHANGED` for work that was already on disk. The task reported NOT
    COMPLETE with the file finished.

    Nothing weakens here. The merged step keeps the union of what its parts
    required and every acceptance criterion they carried, so the same proof is
    owed; it is owed once, by the one step that does the work.

    **Adjacent and identical only.** Same file tuple, back to back, both
    changing it. A step that reads the file in between is a different intent
    and breaks the run, because merging across it would reorder the work.
    """
    merged: list[PlanStepProposal] = []

    #: Original 1-based position -> position in `merged`. Dependencies are
    #: written against the plan the model produced, and merging renumbers it;
    #: without this remap, "step 4 needs step 2" would silently come to mean a
    #: different step, which is worse than having no dependencies at all.
    moved: dict[int, int] = {}

    for index, proposal in enumerate(proposals, start=1):
        previous = merged[-1] if merged else None
        mergeable = (
            previous is not None
            and bool(proposal.files)
            and previous.files == proposal.files
            and effective_kind(previous, read_only=read_only) == "change"
            and effective_kind(proposal, read_only=read_only) == "change"
        )
        if not mergeable:
            merged.append(proposal)
            moved[index] = len(merged)
            continue

        assert previous is not None  # narrowed by `mergeable`
        merged[-1] = previous.model_copy(
            update={
                "title": f"{previous.title}; {proposal.title.lower()}",
                "required_evidence": tuple(
                    dict.fromkeys([*previous.required_evidence, *proposal.required_evidence])
                ),
                "acceptance_criteria": tuple(
                    dict.fromkeys([*previous.acceptance_criteria, *proposal.acceptance_criteria])
                ),
                "risk": max(previous.risk, proposal.risk, key=_RISK_ORDER.__getitem__),
            }
        )
        moved[index] = len(merged)

    # No dependency remap here any more: a proposal carries no dependencies at
    # all, because the model is not asked for them. `derive_dependencies` runs
    # over this merged list, so the positions it produces are already the final
    # ones and there is nothing to renumber.
    return tuple(merged)


def derive_dependencies(steps: Sequence[PlanStepProposal]) -> tuple[tuple[int, ...], ...]:
    """Work out which steps need which, from the files they name.

    **Derived rather than asked for, because asking does not work.** The
    planner prompt describes `steps` as an array and never describes a step's
    fields, so a model is never told `depends_on` exists and never populates
    it. Left as the model's job the graph is empty on every plan — which makes
    `skip_dependents` skip nothing and the local-failure fix inert.

    Invariant 8 applies exactly here: structural facts come from parsers, not
    from models. Two steps touching the same file are ordered by construction —
    you cannot edit `pkg/calc.py` in step 3 before step 1 creates it — and that
    is a fact about the plan, readable without asking anyone.

    Only the *nearest* earlier step sharing a file is recorded. Dependency is
    transitive and `skip_dependents` walks it, so linking to the whole history
    would add edges that say nothing new.

    **The model is not asked.** It used to carry a `depends_on` field, and a
    live PRD build showed exactly what that costs: qwen2.5-coder emitted
    `"depends_on": [0, 1, 2, ... 74` and ran out of output tokens mid-JSON, so
    the whole plan failed to parse and the run blocked before its first tool
    call. A field a small model can fill in wrongly is a field that can destroy
    the response around it — and this one is derivable, so it is derived.
    """
    derived: list[tuple[int, ...]] = []

    for index, step in enumerate(steps):
        files = {_file_key(path) for path in step.files}
        needs: set[int] = set()

        if files:
            for earlier in range(index - 1, -1, -1):
                if files & {_file_key(path) for path in steps[earlier].files}:
                    needs.add(earlier + 1)  # 1-based, as the field documents
                    break

        derived.append(tuple(sorted(needs)))

    return tuple(derived)


def _file_key(path: str) -> str:
    return workspace_key(path).lower()


#: Severity order for `Risk`, which is a `StrEnum` and so compares
#: alphabetically — "critical" < "low" is not what a merge should mean.
_RISK_ORDER: dict[str, int] = {
    Risk.LOW: 0,
    Risk.MEDIUM: 1,
    Risk.HIGH: 2,
    Risk.CRITICAL: 3,
}


def materialise(
    task_id: TaskId,
    plan: ImplementationPlan,
    *,
    version: int = 1,
    read_only: bool = False,
    producible: frozenset[EvidenceKind] | None = None,
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
    unsatisfiable: set[EvidenceKind] = set()

    # Idempotent: `Planner._persist` already stripped these before validating,
    # and `materialise` is also called directly by tests and tooling. Doing it
    # again costs nothing and means neither caller can forget.
    merged = coalesce_by_file(strip_documents(plan).steps, read_only=read_only)
    dependencies = derive_dependencies(merged)

    for ordinal, proposal in enumerate(merged):
        mapping = map_required_evidence(proposal.required_evidence)
        unmapped.extend(mapping.unrecognised)

        # What the step *is*, which is not always what the proposal called it.
        kind = effective_kind(proposal, read_only=read_only)
        if kind != proposal.kind:
            reclassified.append(proposal.title)

        # The model may raise its own bar — but only as far as something can
        # actually clear it. `_VOCABULARY` maps "verify the migration applies"
        # onto MIGRATION_APPLIED, which no tool produces, and the step then
        # cannot complete however well it is executed. The runtime told the
        # model "no available tool can produce this" and blocked anyway.
        asked = mapping.kinds
        if producible is not None:
            unsatisfiable |= asked - producible
            asked &= producible

        # Union, never replacement. A plan proposing no evidence at all still
        # gets the floor for its kind.
        required = asked | evidence_floor(kind, producible=producible)

        # An investigate step cannot patch, so a mapped requirement for
        # FILE_CHANGED is a requirement it has no tool to satisfy. Dropping it
        # keeps the model's *raised* bar meaningful while refusing to build a
        # gate with no key -- the phrase survives as an acceptance criterion.
        if kind == "investigate":
            required -= CHANGE_FLOOR

        risk = _effective_risk(proposal, kind)

        steps.append(
            PlanStepRecord(
                step_id=StepId(new_id()),
                plan_id=plan_id,
                ordinal=ordinal,
                title=proposal.title,
                inputs=proposal.files,
                outputs=(),
                constraints=(proposal.intent,) if proposal.intent else (),
                allowed_tools=allowed_tools_for(
                    kind,
                    may_remove=bool(_REMOVES.search(proposal.title)),
                    may_run=step_may_run(proposal.title, proposal.acceptance_criteria),
                ),
                acceptance_criteria=_criteria(proposal, mapping),
                required_evidence=tuple(sorted(required, key=lambda kind: kind.value)),
                depends_on=dependencies[ordinal],
                risk=risk,
                approval_required=_needs_approval(risk),
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
        unsatisfiable_evidence=tuple(sorted(unsatisfiable, key=lambda kind: kind.value)),
    )


def _criteria(proposal: PlanStepProposal, mapping: EvidenceMapping) -> tuple[str, ...]:
    """Acceptance criteria, plus any evidence phrase that mapped to nothing.

    An unmapped phrase is still a statement about what "done" means. Keeping it
    as prose loses its mechanical force but not its content, and a criterion
    the runtime cannot check is at least one a reviewer can read.
    """
    return tuple(dict.fromkeys([*proposal.acceptance_criteria, *mapping.unrecognised]))


def _effective_risk(proposal: PlanStepProposal, kind: str) -> Risk:
    """The risk a step can actually carry, which caps what it claims.

    Same reasoning as the evidence floor above: an `investigate` step holds
    `READ_ONLY_TOOLS` and has no way to write, so `high` describes a
    consequence it cannot produce. Left uncapped it is worse than cosmetic —
    high risk demands approval, and a headless run has no approver, so the
    whole task stops to authorise something that could never have happened.

    A live build died exactly there: a 7B labelled *"Check for existing
    storage.py file"* high risk, and the run stopped before writing anything.
    Models are consistently poor at this judgement, which is the argument for
    deriving the ceiling from the allowlist rather than trusting the label.

    The cap only ever lowers. A model calling a read `critical` is wrong in a
    way the runtime can prove; one calling a write `critical` may know
    something the runtime does not, so that is left alone.
    """
    declared = Risk(proposal.risk)
    if kind == "investigate" and declared in (Risk.HIGH, Risk.CRITICAL):
        return Risk.LOW
    return declared


def _needs_approval(risk: Risk) -> bool:
    """The runtime decides approval, not the plan.

    `PlanStepProposal` has no approval field on purpose: a model that could
    declare its own step pre-approved would route around the only human gate in
    the system. It takes the *effective* risk, so the cap above carries here.
    """
    return risk in (Risk.HIGH, Risk.CRITICAL)


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
        producible: frozenset[EvidenceKind] | None = None,
    ) -> None:
        self._store = store
        self._limits = limits or DEFAULT_LIMITS
        self._sandbox = sandbox
        self._read_only = read_only
        self._producible = producible

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
        # Documents are stripped *before* validation, not between validation and
        # materialisation. Otherwise the two disagree: validation sees a step
        # naming `PRD.docx`, accepts it as "names a file", and materialise then
        # removes it — so a step that in fact names no editable target sails
        # through the one check that exists to catch exactly that. Stripping
        # first means such a step is refused, and the model is told to name the
        # files it will actually create.
        # Strip before recovering, not after. A step whose only named file is
        # the specification is, once stripped, a step that names nothing --- and
        # `recover_named_files` is a no-op on a step that already named
        # something, so recovering first would look at the *unstripped* step,
        # decline to help, and leave the stripping to empty it. That is not
        # hypothetical: the first prompt of a fresh OpenBazaar build proposed
        # `files: ["OpenBazaar_Marketplace_PRD.docx"]` for a step titled
        # "... Create manage.py at the workspace root ...", and blocked without
        # running a tool. Stripping first lets recovery see the empty field and
        # take `manage.py` from the step's own title.
        plan = recover_named_files(strip_documents(plan))

        # Only now, with every filename rescued from wherever the model put it,
        # is a step with no target genuinely unexecutable. Dropping those beats
        # refusing the plan they arrived in, which discarded the good steps too.
        plan, dropped = drop_unexecutable_steps(plan)

        validation = validate_plan(
            plan,
            sandbox=self._sandbox,
            files_seen=files_seen,
            request=task.request,
            read_only=self._read_only,
        )
        if not validation.ok:
            raise PlanRejected(validation.problems)

        materialised = materialise(
            task.task_id,
            plan,
            version=version,
            read_only=self._read_only,
            producible=self._producible,
        )
        if dropped:
            materialised = replace(materialised, dropped_steps=dropped)
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

    def skip_dependents(self, step: PlanStepRecord) -> tuple[PlanStepRecord, ...]:
        """Close every step that transitively needed `step`, as SKIPPED.

        Transitive because a dependency chain is only as good as its weakest
        link: if 4 needs 3 and 3 needs 2, then 2 failing makes 4 unreachable
        just as surely as it makes 3 unreachable, and stopping at direct
        dependents would hand 4 to the executor with its precondition missing.

        Only *unfinished* steps are touched. A step that already passed keeps
        its outcome and its evidence — work that was done and proven stays
        done, whatever failed afterwards.

        Returns what it closed, so the caller can say so rather than leaving
        the user to infer it from a report full of steps that never ran.
        """
        steps = self._store.get_steps(step.plan_id)
        doomed = {step.ordinal}

        # One forward pass suffices: `depends_on` only ever points backwards
        # (`_remap` drops anything else), so a step's dependencies are always
        # resolved before it is reached.
        skipped: list[PlanStepRecord] = []
        for candidate in sorted(steps, key=lambda item: item.ordinal):
            if candidate.ordinal in doomed or candidate.outcome is not None:
                continue
            # `depends_on` is 1-based, `ordinal` is 0-based.
            if any(position - 1 in doomed for position in candidate.depends_on):
                doomed.add(candidate.ordinal)
                skipped.append(self.fail_step(candidate, StepOutcome.SKIPPED))

        return tuple(skipped)

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
        marker = _MARKERS.get(step.outcome, " ") if step.outcome else " "
        pointer = " ← current" if current is not None and step.step_id == current else ""
        lines.append(f"[{marker}] {step.ordinal + 1}. {step.title}{pointer}")
    return "\n".join(lines).strip()


#: How each closed outcome appears in the plan view.
#:
#: A blank used to mean everything that was not `PASS`, which was harmless while
#: the only alternative to passing was ending the run. Now that a failed step
#: leaves the rest of the plan running, a skipped step and a pending step would
#: render identically — so the model would be told work is still coming that
#: nothing intends to do.
_MARKERS: dict[StepOutcome, str] = {
    StepOutcome.PASS: "✓",
    StepOutcome.SKIPPED: "–",
    StepOutcome.BLOCKED: "✗",
    StepOutcome.CANCELLED: "✗",
    StepOutcome.APPROVAL_REQUIRED: "✗",
    StepOutcome.PLAN_INVALID: "✗",
    StepOutcome.REPAIRABLE: " ",
}


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
    "step_may_run",
    "asks_for_a_change",
    "effective_kind",
    "derive_dependencies",
    "editable_files",
    "recover_named_files",
    "strip_documents",
    "evidence_floor",
    "map_required_evidence",
    "materialise",
    "render_plan_summary",
    "render_step",
    "validate_plan",
]
