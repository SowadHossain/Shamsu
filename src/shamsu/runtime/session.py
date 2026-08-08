"""The loop. One run, walked through the state machine.

This is the module the prime directive is about: *the runtime controls the
loop; the model performs one narrow decision at a time.* Every other component
in v2 is a bounded controller that answers one question and returns. Nothing
until now composed them, so nothing until now could actually run a task.

**The state machine is a dispatch table, not control flow.** Each handler
returns the next state and the loop persists the move through
`StateStore.advance_task`, which validates it against the transition table. An
illegal move raises rather than being coerced — that is the difference between
a machine you can reason about and v1's `if`-branch ordering.

**Every transition passes through `RunController.checkpoint`.** Cancellation,
pause, and the wall clock are checked between units of work rather than
sprinkled through them. v1 had no mid-run cancellation path at all; this is the
single defect that most motivates v2.

**The loop is bounded twice.** Each phase has its own budget from
`ExecutionLimits`, and the whole machine has a transition cap. Per-phase bounds
stop one phase running away; the cap stops two states ping-ponging forever, and
hitting it is a runtime bug that should be visible rather than a hang.

**Every exit is named.** `SessionResult.stopped_because` is always set. v1's
exhaustion paths were frequently indistinguishable from failure, so "it
stopped" was not a report anyone could act on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from shamsu.agent.planning import (
    Planner,
    PlanRejected,
    render_plan_summary,
    render_step,
)
from shamsu.agent.readonly import InvestigationResult, ReadOnlyAgent
from shamsu.agent.repair import RepairController, RepairScope
from shamsu.context.compiler import ContextCompiler, FrameInputs
from shamsu.interfaces.cancellation import CancellationToken, Cancelled
from shamsu.interfaces.context import ContextFrame
from shamsu.interfaces.enums import AgentState, EvidenceKind, Phase, StepOutcome, TaskKind
from shamsu.interfaces.ids import CheckpointId, PlanId, ProjectId, RunId, StepId, TaskId
from shamsu.interfaces.models import (
    ModelClient,
    ModelContractError,
    ModelMessage,
    ModelRequest,
    ModelUnavailable,
)
from shamsu.interfaces.tools import ToolPolicyViolation, ToolRequest, ToolResult
from shamsu.memory.store import MemoryStore
from shamsu.models.contracts import (
    ImplementationPlan,
    InvestigationStep,
    PlanStepProposal,
    TaskClassification,
    ToolCall,
    schema_hint,
)
from shamsu.runtime.controller import RunController
from shamsu.runtime.events import EventKind
from shamsu.runtime.limits import ExecutionLimits, LimitExceeded
from shamsu.state.records import CheckpointRecord, PlanStepRecord, TaskRecord, new_id
from shamsu.state.store import StateStore
from shamsu.state.transitions import (
    can_transition,
    is_terminal,
    next_after_classification,
    next_after_verification,
)
from shamsu.tools.gateway import ToolGateway
from shamsu.verification.completion import (
    CompletionGate,
    EvidenceReport,
    build_report,
    next_after_completion_gate,
)
from shamsu.verification.digest import TestDigest
from shamsu.verification.evidence import EvidenceRecorder

#: Hard cap on state transitions in one run. Not a work budget — the per-phase
#: limits are that. This exists so a bug that makes two states hand off to each
#: other terminates loudly instead of hanging, and it is deliberately far above
#: anything a legitimate run needs.
MAX_TRANSITIONS = 400

_EXECUTE_RULES = """\
You are executing ONE step of a plan. Work one action at a time: each response
is a single decision — call one tool, or conclude that the step is done.

You may only use the tools listed. You may only edit the files this step names.
Do not start the next step; the runtime decides what comes next.

Ground every claim in something a tool actually returned."""

_CLASSIFY_RULES = """\
Decide whether this task needs a multi-step plan.

'direct' means one focused change to one place. 'planned' means anything that
touches several files, needs investigation first, or has ordering constraints.

If you are unsure, answer 'planned'."""


@dataclass
class SessionResult:
    """What a run produced, and why it stopped.

    `stopped_because` is always populated. `report` is present whenever a plan
    existed to report on — including for a failed run, because the evidence
    report is how a user decides whether to trust what happened.
    """

    task_id: TaskId
    run_id: RunId
    final_state: AgentState
    stopped_because: str = ""
    completed: bool = False
    report: EvidenceReport | None = None
    transitions: tuple[AgentState, ...] = ()

    @property
    def blocked(self) -> bool:
        return self.final_state is AgentState.BLOCKED

    @property
    def cancelled(self) -> bool:
        return self.final_state is AgentState.STOPPED

    def render(self) -> str:
        headline = f"{self.final_state.value}: {self.stopped_because}"
        return f"{headline}\n\n{self.report.render()}" if self.report else headline


@dataclass
class AgentSession:
    """Runs one task from request to verified report.

    Assembled from components rather than containing their logic: the planner
    plans, the gate gates, the repair controller decides about repair. This
    class decides only *what happens next*, which is the one thing that has to
    live in the runtime.
    """

    store: StateStore
    runs: RunController
    model: ModelClient
    gateway: ToolGateway
    compiler: ContextCompiler
    workspace: Path
    project_id: ProjectId

    limits: ExecutionLimits | None = None
    memory: MemoryStore | None = None

    _visited: list[AgentState] = field(default_factory=list, repr=False)

    async def run(self, request: str) -> SessionResult:
        """Execute one task. Never raises for an ordinary failure.

        `Cancelled` is the single exception that escapes, because a cancelled
        run must not return something shaped like an answer.
        """
        bounds = self.limits or ExecutionLimits()
        task = self.store.create_task(
            TaskRecord(task_id=TaskId(new_id()), project_id=self.project_id, request=request)
        )
        run_id = self.runs.register(self.project_id, task.task_id, limits=bounds)
        self.runs.start(run_id)

        state = AgentState.RECEIVE_TASK
        self._visited = [state]
        context = _Context(
            task=task,
            run_id=run_id,
            limits=bounds,
            recorder=EvidenceRecorder(self.store, run_id, task.task_id),
            planner=Planner(self.store, limits=bounds),
            gate=CompletionGate(self.store, task.task_id),
            repair=RepairController(self.store, task.task_id, limits=bounds, memory=self.memory),
        )

        try:
            state, reason = await self._walk(state, context)
        except Cancelled as exc:
            self.runs.finish_cancelled(run_id)
            self.store.advance_task(self._task(context), AgentState.STOPPED, cancelling=True)
            return self._result(context, AgentState.STOPPED, f"cancelled: {exc}", completed=False)

        completed = state is AgentState.FINAL_REPORT
        if completed:
            self.runs.complete(run_id)
        else:
            self.runs.fail(run_id, reason)

        return self._result(context, state, reason, completed=completed)

    # -- the machine -------------------------------------------------------

    async def _walk(self, state: AgentState, context: _Context) -> tuple[AgentState, str]:
        """Drive states until a terminal one, returning it and the reason."""
        reason = ""

        for _ in range(MAX_TRANSITIONS):
            if is_terminal(state):
                return state, reason or f"reached {state.value}"

            await self.runs.checkpoint(context.run_id)
            self.runs.record_state(context.run_id, state)

            try:
                target, reason = await self._dispatch(state, context)
            except LimitExceeded as exc:
                # A backstop, not the normal path: handlers that can exhaust a
                # budget catch it themselves and return a *legal* successor.
                # Asserting legality here means a future state that forgets to
                # says so loudly, instead of being silently coerced into an
                # illegal move -- which is the failure the table exists to stop.
                assert can_transition(state, AgentState.BLOCKED), (
                    f"{state.value} raised {exc} but cannot reach BLOCKED; "
                    "the handler must return a legal successor"
                )
                target, reason = AgentState.BLOCKED, str(exc)

            self.store.advance_task(self._task(context), target, phase=_PHASES.get(target))
            self._visited.append(target)
            state = target

        return AgentState.BLOCKED, (
            f"the state machine made {MAX_TRANSITIONS} transitions without finishing; "
            "this is a runtime defect, not an exhausted budget"
        )

    async def _dispatch(self, state: AgentState, context: _Context) -> tuple[AgentState, str]:
        """One state's work. Returns the next state and a reason for the record."""
        if state is AgentState.RECEIVE_TASK:
            return AgentState.LOAD_PROJECT_STATE, "task received"

        if state is AgentState.LOAD_PROJECT_STATE:
            return AgentState.INSPECT_PROJECT, "project state loaded"

        if state is AgentState.INSPECT_PROJECT:
            return await self._inspect(context)

        if state is AgentState.CLASSIFY_TASK:
            return await self._classify(context)

        if state is AgentState.CREATE_PLAN:
            return await self._create_plan(context)

        if state is AgentState.VALIDATE_PLAN:
            return self._validate_plan(context)

        if state is AgentState.APPROVAL_CHECK:
            return self._approval_check(context)

        if state is AgentState.WAIT_APPROVAL:
            # No interactive approver is wired in yet. Waiting forever would be
            # worse than stopping: an unattended run would hang rather than
            # report that it needs a human.
            return AgentState.STOPPED, "a step requires approval and no approver is configured"

        if state is AgentState.EXECUTE_CURRENT_STEP:
            return await self._execute_step(context)

        if state is AgentState.VERIFY_CURRENT_STEP:
            return self._verify_step(context)

        if state is AgentState.CREATE_CHECKPOINT:
            return self._checkpoint(context)

        if state is AgentState.REPAIR:
            return self._repair(context)

        if state is AgentState.REPLAN:
            return await self._replan(context)

        if state is AgentState.CHECK_REMAINING_STEPS:
            return self._check_remaining(context)

        if state is AgentState.FINAL_VERIFICATION:
            return AgentState.COMPLETION_GATE, "all steps closed"

        if state is AgentState.COMPLETION_GATE:
            return self._completion_gate(context)

        raise AssertionError(f"no handler for {state}")  # pragma: no cover

    # -- handlers ----------------------------------------------------------

    async def _inspect(self, context: _Context) -> tuple[AgentState, str]:
        """Read-only investigation. Produces the grounding a plan is checked against."""
        agent = ReadOnlyAgent(self.model, self.gateway, self.compiler, context.limits)
        investigation = await agent.investigate(context.task.request, cancel=self._token(context))
        context.investigation_files = tuple(investigation.files_seen)
        context.investigation_summary = investigation.conclusion
        return AgentState.CLASSIFY_TASK, investigation.stopped_because

    async def _classify(self, context: _Context) -> tuple[AgentState, str]:
        """One narrow decision, defaulting to the safer branch.

        A failed or unavailable model yields PLANNED, which goes through plan
        validation and the approval check. Defaulting to DIRECT would route
        around both on the strength of a call that did not happen.
        """
        kind = TaskKind.PLANNED
        reason = "defaulted to planned"

        frame = self.compiler.compile(
            FrameInputs(
                phase=Phase.PLAN,
                task=context.task.request,
                output_contract="TaskClassification",
                latest_observation=context.investigation_summary,
                system_rules=_CLASSIFY_RULES + "\n\n" + schema_hint(TaskClassification),
            ),
            (),
        )
        try:
            answer = await self.model.generate_typed(
                _request(frame), TaskClassification, self._token(context)
            )
            kind = TaskKind.DIRECT if answer.kind == "direct" else TaskKind.PLANNED
            reason = answer.reason or f"classified as {kind.value}"
        except (ModelContractError, ModelUnavailable) as exc:
            reason = f"classification failed ({exc}); defaulted to planned"

        self.store.save_task(self._task(context).model_copy(update={"kind": kind}))

        if kind is TaskKind.DIRECT:
            # DIRECT skips the *planning model call*, not the plan record.
            # Everything executes through plan steps, so there is one execution
            # path rather than two.
            context.direct = True
            return AgentState.CREATE_PLAN, reason

        return next_after_classification(kind), reason

    async def _create_plan(self, context: _Context) -> tuple[AgentState, str]:
        plan = (
            _direct_plan(context.task.request, context.investigation_files)
            if context.direct
            else await self._ask_for_plan(context)
        )
        if plan is None:
            return AgentState.BLOCKED, "the model did not produce a usable plan"

        context.proposal = plan
        return AgentState.VALIDATE_PLAN, f"proposed {len(plan.steps)} step(s)"

    async def _ask_for_plan(self, context: _Context) -> ImplementationPlan | None:
        agent = ReadOnlyAgent(self.model, self.gateway, self.compiler, context.limits)
        if context.replan_reason:
            return await agent.replan(
                context.task.request,
                previous_summary=context.previous_summary,
                completed=context.completed_titles,
                reason=context.replan_reason,
                cancel=self._token(context),
            )

        return await agent.plan(
            context.task.request,
            InvestigationResult(
                conclusion=context.investigation_summary,
                files_seen=list(context.investigation_files),
            ),
            cancel=self._token(context),
        )

    def _validate_plan(self, context: _Context) -> tuple[AgentState, str]:
        """Persist the plan, or send it back to be regenerated."""
        assert context.proposal is not None

        try:
            if context.replan_reason:
                materialised = context.planner.replan(self._task(context), context.proposal)
                context.replan_reason = ""
            else:
                materialised = context.planner.create(
                    self._task(context),
                    context.proposal,
                    files_seen=context.investigation_files,
                )
        except PlanRejected as exc:
            context.rejections += 1
            if context.rejections > context.limits.replans_per_task:
                return AgentState.BLOCKED, f"plan rejected repeatedly: {exc}"
            return AgentState.CREATE_PLAN, f"plan rejected: {exc}"
        except LimitExceeded as exc:
            return AgentState.BLOCKED, str(exc)

        context.plan_id = materialised.plan_id
        return AgentState.APPROVAL_CHECK, f"plan v{materialised.record.version} persisted"

    def _approval_check(self, context: _Context) -> tuple[AgentState, str]:
        step = self._next_step(context)
        if step is None:
            return AgentState.EXECUTE_CURRENT_STEP, "no steps require approval"
        if step.approval_required:
            return AgentState.WAIT_APPROVAL, f"step {step.ordinal + 1} needs approval"
        return AgentState.EXECUTE_CURRENT_STEP, "no approval required"

    async def _execute_step(self, context: _Context) -> tuple[AgentState, str]:
        """The bounded inner loop (plan §11), for one step."""
        step = self._next_step(context)
        if step is None:
            return AgentState.VERIFY_CURRENT_STEP, "no step to execute"

        try:
            step = context.planner.begin_step(step)
        except LimitExceeded as exc:
            # Verify will refuse, repair will find the budget spent, and the
            # run blocks there. Going straight to BLOCKED would skip the gate
            # that decides whether the step was actually finished.
            context.current = step
            return AgentState.VERIFY_CURRENT_STEP, str(exc)

        context.current = step
        self.store.save_task(
            self._task(context).model_copy(update={"current_step_id": step.step_id})
        )

        scope = context.repair_scope
        actions = 0
        failures = 0
        latest = context.capsule_text
        attempted: list[tuple[str, str]] = []

        for _ in range(context.limits.actions_per_step):
            await self.runs.checkpoint(context.run_id)

            frame = self._step_frame(context, step, latest, attempted)
            try:
                decision = await self._decide(frame, self._token(context))
            except ModelUnavailable as exc:
                return AgentState.VERIFY_CURRENT_STEP, f"model unavailable: {exc}"
            except ModelContractError as exc:
                failures += 1
                latest = (
                    f"Your last response did not match the required JSON shape ({exc}). "
                    "Respond with valid JSON only."
                )
                if failures >= context.limits.consecutive_failed_actions:
                    return AgentState.VERIFY_CURRENT_STEP, "model produced unparseable responses"
                continue

            if decision.action == "conclude":
                # A proposal, not a decision. VERIFY reads the evidence table.
                return AgentState.VERIFY_CURRENT_STEP, decision.conclusion[:200]

            assert decision.tool is not None
            latest, ok, executed = await self._invoke(context, step, decision.tool, scope)

            # A refusal did no work, so it does not spend the work budget. It
            # is still a failure and `consecutive_failed_actions` still bounds
            # it — but charging it twice meant one mistyped argument could cost
            # a step the call that would have finished it. A live run spent its
            # whole budget on search, a refused patch, the real patch, and a
            # diff review, and never reached `test.run`.
            if executed:
                actions += 1
            failures = 0 if ok else failures + 1
            attempted.append((_describe_attempt(decision.tool), _outcome_line(latest, ok)))

            if failures >= context.limits.consecutive_failed_actions:
                return AgentState.VERIFY_CURRENT_STEP, f"{failures} consecutive failed actions"

        return AgentState.VERIFY_CURRENT_STEP, f"action budget spent ({actions})"

    def _verify_step(self, context: _Context) -> tuple[AgentState, str]:
        """Apply the step gate. The model's opinion is not consulted."""
        step = context.current
        if step is None:
            return AgentState.CHECK_REMAINING_STEPS, "no step under verification"

        closed, result = context.planner.close_step(step, context.recorder.verified(step.step_id))
        context.current = closed

        if result.satisfied:
            context.capsule_text = ""
            context.repair_scope = None
            return next_after_verification(StepOutcome.PASS), result.explain()

        outcome = StepOutcome.REPAIRABLE if context.last_digest is not None else StepOutcome.BLOCKED
        if outcome is StepOutcome.BLOCKED:
            context.planner.fail_step(step, StepOutcome.BLOCKED)
            return next_after_verification(outcome), result.explain()

        return next_after_verification(outcome), result.explain()

    def _checkpoint(self, context: _Context) -> tuple[AgentState, str]:
        """A verified step boundary — the only resume point v2 promises."""
        step = context.current
        self.store.create_checkpoint(
            _checkpoint_record(self._task(context), step.step_id if step else None)
        )
        self.runs.note(context.run_id, EventKind.CHECKPOINT_CREATED, "verified step boundary")
        return AgentState.CHECK_REMAINING_STEPS, "checkpointed"

    def _repair(self, context: _Context) -> tuple[AgentState, str]:
        """Ask the repair controller whether another attempt is warranted."""
        step = context.current
        digest = context.last_digest
        if step is None or digest is None:
            return AgentState.BLOCKED, "nothing to repair"

        decision = context.repair.consider(
            digest,
            step_id=step.step_id,
            related_files=step.inputs,
            changed_files=context.changed_files,
        )
        if not decision.proceed:
            context.planner.fail_step(step, decision.outcome or StepOutcome.BLOCKED)
            return AgentState.BLOCKED, decision.reason

        context.capsule_text = decision.capsule.render() if decision.capsule else ""
        context.repair_scope = decision.scope
        self.store.save_task(
            self._task(context).model_copy(
                update={"repair_count": self._task(context).repair_count + 1}
            )
        )
        return AgentState.EXECUTE_CURRENT_STEP, decision.reason

    async def _replan(self, context: _Context) -> tuple[AgentState, str]:
        task = self._task(context)
        try:
            context.limits.check_replans(task.replan_count)
        except LimitExceeded as exc:
            return AgentState.BLOCKED, str(exc)

        if context.plan_id is not None:
            record = self.store.get_plan(context.plan_id)
            context.previous_summary = record.summary if record else ""
            context.completed_titles = context.planner.completed_titles(context.plan_id)

        context.replan_reason = context.replan_reason or "the plan did not complete"
        return AgentState.CREATE_PLAN, "re-planning"

    def _check_remaining(self, context: _Context) -> tuple[AgentState, str]:
        if self._next_step(context) is not None:
            return AgentState.EXECUTE_CURRENT_STEP, "more steps remain"
        return AgentState.FINAL_VERIFICATION, "no steps remain"

    def _completion_gate(self, context: _Context) -> tuple[AgentState, str]:
        if context.plan_id is None:
            return AgentState.BLOCKED, "no plan to verify"

        verdict = context.gate.check_task(context.plan_id)
        task = self._task(context)
        can_replan = task.replan_count < context.limits.replans_per_task

        target = next_after_completion_gate(verdict, can_replan=can_replan)
        if target is AgentState.REPLAN:
            context.replan_reason = verdict.reason
        return target, verdict.reason

    # -- helpers -----------------------------------------------------------

    async def _invoke(
        self,
        context: _Context,
        step: PlanStepRecord,
        call: ToolCall,
        scope: RepairScope | None,
    ) -> tuple[str, bool, bool]:
        """Run one tool call under the step's allowlist and the active scope."""
        if step.allowed_tools and call.tool not in step.allowed_tools:
            return (
                f"Refused: {call.tool} is not available to this step. "
                f"Allowed: {', '.join(step.allowed_tools)}.",
                False,
                False,
            )

        phase = _PHASES.get(AgentState.EXECUTE_CURRENT_STEP, Phase.AUTHOR)
        if call.tool in ("test.run", "git.inspect"):
            # Verification tools declare VERIFY/REPAIR, not AUTHOR. The step is
            # still executing; the phase describes what the *call* is doing.
            phase = Phase.REPAIR if scope is not None else Phase.VERIFY

        request = ToolRequest(tool=call.tool, arguments=call.arguments)
        try:
            with self.gateway.decision():
                if scope is not None:
                    with self.gateway.restricted_to(scope):
                        result = await self.gateway.invoke(request, phase, self._token(context))
                else:
                    result = await self.gateway.invoke(request, phase, self._token(context))
        except ToolPolicyViolation as exc:
            self.runs.note(context.run_id, EventKind.TOOL_INVOKED, f"!{call.tool}  refused")
            return f"Refused: {exc}", False, False

        # The interface only ever saw phase transitions, because this event kind
        # was declared and never emitted. Without it a run shows "author" for a
        # minute with no indication of what it is doing -- which reads as a hang.
        self.runs.note(context.run_id, EventKind.TOOL_INVOKED, _describe_call(request, result))

        context.recorder.record(request, result, phase, step_id=step.step_id)

        if call.tool == "file.patch" and result.ok:
            path = call.arguments.get("path")
            if isinstance(path, str) and path not in context.changed_files:
                context.changed_files = (*context.changed_files, path)

        if call.tool == "test.run":
            tool = self.gateway.get("test.run")
            context.last_digest = getattr(tool, "last_digest", None)

        body = result.output if result.ok else (result.error or "failed")
        return body[:2000], result.ok, True

    def _step_frame(
        self,
        context: _Context,
        step: PlanStepRecord,
        latest: str,
        attempted: Sequence[tuple[str, str]] = (),
    ) -> ContextFrame:
        record = self.store.get_plan(context.plan_id) if context.plan_id else None
        steps = self.store.get_steps(context.plan_id) if context.plan_id else ()

        return self.compiler.compile(
            FrameInputs(
                phase=Phase.REPAIR if context.repair_scope else Phase.AUTHOR,
                task=context.task.request,
                output_contract="InvestigationStep",
                acceptance_criteria=step.acceptance_criteria,
                current_step=render_step(step),
                plan_summary=(
                    render_plan_summary(record, steps, current=step.step_id) if record else ""
                ),
                project_facts=self.memory.recall() if self.memory else "",
                latest_observation=latest,
                attempted=tuple(attempted),
                system_rules=(
                    _EXECUTE_RULES
                    + "\n\n"
                    + _evidence_rule(step, self.gateway, context.recorder.verified(step.step_id))
                    + "\n\n"
                    + schema_hint(InvestigationStep)
                ),
            ),
            [
                contract
                for contract in self.gateway.available(Phase.AUTHOR)
                if not step.allowed_tools or contract.name in step.allowed_tools
            ],
        )

    async def _decide(self, frame: ContextFrame, token: CancellationToken) -> InvestigationStep:
        decision = await self.model.generate_typed(_request(frame), InvestigationStep, token)
        try:
            return decision.validated()
        except ValueError as exc:
            raise ModelContractError(str(exc), raw_text=decision.model_dump_json()) from exc

    def _next_step(self, context: _Context) -> PlanStepRecord | None:
        return context.planner.next_step(context.plan_id) if context.plan_id else None

    def _task(self, context: _Context) -> TaskRecord:
        """The task as it currently is on disk.

        Re-read rather than cached: handlers persist counter changes, and a
        stale copy would overwrite them on the next `advance_task`.
        """
        current = self.store.get_task(context.task.task_id)
        assert current is not None
        return current

    def _token(self, context: _Context) -> CancellationToken:
        return self.runs.token(context.run_id)

    def _result(
        self, context: _Context, state: AgentState, reason: str, *, completed: bool
    ) -> SessionResult:
        report = None
        if context.plan_id is not None:
            report = build_report(self.store, context.task.task_id, context.plan_id)

        self.store.save_task(self._task(context).model_copy(update={"final_result": reason[:2000]}))
        return SessionResult(
            task_id=context.task.task_id,
            run_id=context.run_id,
            final_state=state,
            stopped_because=reason,
            completed=completed,
            report=report,
            transitions=tuple(self._visited),
        )


@dataclass
class _Context:
    """Everything one run carries between states.

    A single object rather than attributes on the session, so a session can run
    more than one task and so nothing leaks between them.
    """

    task: TaskRecord
    run_id: RunId
    limits: ExecutionLimits
    recorder: EvidenceRecorder
    planner: Planner
    gate: CompletionGate
    repair: RepairController

    direct: bool = False
    plan_id: PlanId | None = None
    proposal: ImplementationPlan | None = None
    current: PlanStepRecord | None = None

    investigation_files: tuple[str, ...] = ()
    investigation_summary: str = ""
    changed_files: tuple[str, ...] = ()

    last_digest: TestDigest | None = None
    capsule_text: str = ""
    repair_scope: RepairScope | None = None

    replan_reason: str = ""
    previous_summary: str = ""
    completed_titles: tuple[str, ...] = ()
    rejections: int = 0


#: The phase each state runs in. The gateway enforces the tool allowlist from
#: this, so a state's phase is a policy decision rather than a label.
_PHASES: dict[AgentState, Phase] = {
    AgentState.INSPECT_PROJECT: Phase.INSPECT,
    AgentState.CLASSIFY_TASK: Phase.PLAN,
    AgentState.CREATE_PLAN: Phase.PLAN,
    AgentState.VALIDATE_PLAN: Phase.PLAN,
    AgentState.APPROVAL_CHECK: Phase.PLAN,
    AgentState.EXECUTE_CURRENT_STEP: Phase.AUTHOR,
    AgentState.VERIFY_CURRENT_STEP: Phase.VERIFY,
    AgentState.REPAIR: Phase.REPAIR,
    AgentState.REPLAN: Phase.PLAN,
    AgentState.FINAL_VERIFICATION: Phase.VERIFY,
    AgentState.COMPLETION_GATE: Phase.COMPLETE,
    AgentState.FINAL_REPORT: Phase.COMPLETE,
}


def _evidence_rule(
    step: PlanStepRecord,
    gateway: ToolGateway,
    verified: frozenset[EvidenceKind] = frozenset(),
) -> str:
    """Tell the step what would make concluding it legitimate.

    Without this the model concludes as soon as it believes the work is done,
    which the gate then refuses — correctly, and uselessly, because nothing
    told the model what "done" is measured by. A live run searched for
    `def add(`, saw the signature line, and concluded the function was fine
    without ever reading the body that held the bug.

    The tool for each kind is **derived from the tool contracts**, not listed
    here. `produces_evidence` already declares it, and a second copy would be
    one that could drift — a renamed tool would leave this telling the model to
    call something that no longer exists.

    This does not lower the bar. The gate still reads the evidence table and a
    model claim still proves nothing. It states the bar.
    """
    if not step.required_evidence:
        return (
            "This step requires no evidence, so concluding is enough once you "
            "believe the work is done."
        )

    producers: dict[EvidenceKind, list[str]] = {}
    for phase in (Phase.AUTHOR, Phase.VERIFY):
        for contract in gateway.available(phase):
            for kind in contract.produces_evidence:
                producers.setdefault(kind, []).append(contract.name)

    done: list[str] = []
    todo: list[str] = []
    for kind in step.required_evidence:
        if kind in verified:
            done.append(f"- {kind.value}: already done, do not repeat it")
            continue

        tools = sorted(set(producers.get(kind, ())))
        if tools:
            todo.append(f"- {kind.value}: call {' or '.join(tools)}")
        else:
            # Seven of eleven EvidenceKinds have no tool that can produce them
            # (HANDOFF section 3). Saying so beats demanding the impossible in
            # silence and letting the step fail without explanation.
            todo.append(f"- {kind.value}: no available tool can produce this")

    if not todo:
        return (
            "Every kind of evidence this step requires has been produced. "
            "Conclude now; there is nothing further to do."
        )

    # Naming what is already satisfied is what stops the model redoing it. A
    # live run patched the file, then spent the rest of its budget trying to
    # patch it again, because the rule listed what was *required* and never
    # said which parts were already met.
    sections = [
        "STILL NEEDED - the step is not finished until each of these happens:",
        "\n".join(todo),
    ]
    if done:
        sections.append("ALREADY SATISFIED:\n" + "\n".join(done))
    sections.append(
        "Do the next thing on the STILL NEEDED list. Concluding before it is "
        "empty will be rejected and the step reopened."
    )
    return "\n\n".join(sections)


#: Argument that identifies what a tool acted on, per tool. A generic "first
#: string argument" rule picks up `command` for `test.run` and the find-text for
#: `file.patch`, so the interesting one is named rather than guessed.
_SUBJECT_ARGUMENT: dict[str, str] = {
    "file.read": "path",
    "file.patch": "path",
    "file.write": "path",
    "code.search": "query",
    "test.run": "command",
    "git.inspect": "what",
    "project.inspect": "path",
}


def _describe_attempt(call: ToolCall) -> str:
    """A tool call as it appears in the "already tried" list.

    Arguments are included because they are what makes two calls different:
    `file.read` on two files is progress; `file.read` on the same file twice is
    the loop the list exists to stop.
    """
    arguments = ", ".join(f"{name}={value!r}" for name, value in sorted(call.arguments.items()))
    return f"{call.tool}({arguments})" if arguments else f"{call.tool}()"


def _outcome_line(body: str, ok: bool) -> str:
    """One line on how a call went, for the "already tried" list."""
    first = body.strip().splitlines()
    summary = (first[0][:90] if first else "") or "no output"
    return summary if ok else f"failed: {summary}"


def _describe_call(request: ToolRequest, result: ToolResult) -> str:
    """One line for the activity pane: what ran, on what, and how it went.

    A leading `!` marks failure -- `RunView.apply` reads that to pick the glyph
    and the colour. Keeping the convention here means the view needs no new
    vocabulary to show a failed call.
    """
    subject = request.arguments.get(_SUBJECT_ARGUMENT.get(request.tool, ""), "")
    parts = [request.tool]
    if isinstance(subject, str) and subject:
        parts.append(subject if len(subject) <= 48 else subject[:45] + "…")

    if not result.ok:
        parts.append(f"— {(result.error or 'failed').splitlines()[0][:60]}")
    elif result.evidence:
        # Evidence is the only thing a tool produces that the gate will accept,
        # so it earns space on the line ahead of any output preview.
        parts.append("✓ " + ", ".join(sorted(kind.value for kind in result.evidence)))

    if result.duration_seconds >= 0.05:
        parts.append(f"{result.duration_seconds:.1f}s")

    line = "  ".join(parts)
    return line if result.ok else f"!{line}"


def _direct_plan(request: str, files: Sequence[str]) -> ImplementationPlan:
    """A one-step plan for a DIRECT task, built without a model call.

    The step still carries the change floor and the standard tool allowlist, so
    a direct task is gated exactly as strictly as a planned one. What DIRECT
    buys is one fewer model call, not one fewer check.
    """
    return ImplementationPlan(
        summary=f"Direct change: {request}",
        steps=(
            PlanStepProposal(
                title=request[:200],
                intent=request[:600],
                files=tuple(files[:5]),
                acceptance_criteria=("The requested change is made and the tests pass.",),
                required_evidence=("targeted tests pass",),
            ),
        ),
        grounded_in=tuple(files),
    )


def _checkpoint_record(task: TaskRecord, step_id: StepId | None) -> CheckpointRecord:
    return CheckpointRecord(
        checkpoint_id=CheckpointId(new_id()),
        task_id=task.task_id,
        step_id=step_id,
        label="verified step",
        state_snapshot_json=task.model_dump_json(),
    )


def _request(frame: ContextFrame) -> ModelRequest:
    """One frame, one message, one call. No conversation replay (plan §8.4)."""
    return ModelRequest(
        messages=(ModelMessage(role="user", content=frame.render()),),
        max_output_tokens=frame.budget.output_reserve,
    )


__all__ = ["MAX_TRANSITIONS", "AgentSession", "SessionResult"]
