"""The read-only agent: investigate a repository, produce a grounded plan.

Milestone 4's deliverable, and the first place the whole stack runs end to end
— gateway, sandbox, compiler, contracts, limits, cancellation.

The loop is bounded and the runtime owns it:

    compile a frame -> ask for ONE decision -> validate it -> execute one tool
    -> compress the observation -> repeat, until the model concludes or a
    bound is reached

The model never decides how long to keep going. It answers "what next?" and the
runtime decides whether there is a next.

**Nothing here can modify the repository.** The phase is INSPECT throughout, and
the gateway only exposes tools that declare INSPECT — so read-only is enforced
by the same mechanism as every other policy, not by this class remembering to
behave.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from shamsu.context.compiler import ContextCompiler, FrameInputs
from shamsu.interfaces.cancellation import CancellationToken, NullCancellationToken
from shamsu.interfaces.context import ContextFrame
from shamsu.interfaces.enums import Phase
from shamsu.interfaces.models import (
    ModelClient,
    ModelContractError,
    ModelMessage,
    ModelRequest,
    ModelUnavailable,
)
from shamsu.interfaces.tools import ToolPolicyViolation, ToolRequest, ToolResult
from shamsu.models.contracts import (
    ImplementationPlan,
    InvestigationStep,
    ToolCall,
    schema_hint,
)
from shamsu.runtime.limits import DEFAULT_LIMITS, ExecutionLimits
from shamsu.tools.gateway import ToolGateway

_SYSTEM_RULES = """\
You are inspecting a repository. You cannot modify anything.

Work one step at a time. Each response is a single decision: either call one
tool, or conclude. Do not plan several tool calls in one response.

Ground every claim in something a tool actually returned. If you have not read
it, you do not know it."""


@dataclass
class Observation:
    """One tool call and what came back, kept for the run record."""

    tool: str
    ok: bool
    summary: str
    reason: str = ""


@dataclass
class InvestigationResult:
    """What an investigation produced.

    `stopped_because` is always set, so "why did it stop?" never requires
    inferring from the shape of the other fields. v1's exhaustion paths were
    frequently indistinguishable from failure; this makes them distinct.
    """

    conclusion: str = ""
    observations: list[Observation] = field(default_factory=list)
    files_seen: list[str] = field(default_factory=list)
    stopped_because: str = ""
    ok: bool = False

    #: The model could not be reached at all. Distinct from any other stop:
    #: nothing downstream can succeed either, so a caller should report the
    #: environment problem rather than continue and blame what follows.
    unavailable: bool = False

    @property
    def actions_taken(self) -> int:
        return len(self.observations)


class ReadOnlyAgent:
    """Investigates a repository without modifying it."""

    #: How much of a tool result is carried forward as the "latest
    #: observation". The full result was already capped by the gateway; this is
    #: a second, tighter budget because an observation competes with the task
    #: and the step for hot context.
    OBSERVATION_CHARS = 2_000

    def __init__(
        self,
        model: ModelClient,
        gateway: ToolGateway,
        compiler: ContextCompiler,
        limits: ExecutionLimits | None = None,
    ) -> None:
        self._model = model
        self._gateway = gateway
        self._compiler = compiler
        self._limits = limits or DEFAULT_LIMITS

    async def investigate(
        self,
        task: str,
        *,
        project_facts: str = "",
        cancel: CancellationToken | None = None,
        max_actions: int | None = None,
        require_lookup: bool = False,
        must_read: Sequence[str] = (),
    ) -> InvestigationResult:
        """Run the bounded investigation loop.

        `require_lookup` refuses a conclusion reached without calling anything.
        Off by default and on when the conclusion is the *deliverable* —
        answering a user's question. During INSPECT the conclusion is only
        input to planning, and the plan is separately checked against the files
        actually read (`is_grounded`), so a quick conclusion there is cheap
        rather than dishonest.

        `must_read` names files the answer cannot be given without. Pointing at
        a file is the most explicit instruction a user can give, and it was
        being ignored: asked to review `@OpenBazaar_Marketplace_PRD.docx`, a
        live run called `project.inspect` once and described the *repository* —
        never opening the document it was handed. Both nudges are bounded by
        `consecutive_failed_actions`, so a model that will not read still
        leaves.

        Raises:
            Cancelled: the token fired. Propagated rather than swallowed — a
                cancelled run must not return a result that looks like an
                answer.
        """
        token = cancel or NullCancellationToken()
        budget = max_actions or self._limits.actions_per_step
        result = InvestigationResult()

        latest = ""
        consecutive_failures = 0
        #: Conclusions refused for want of any look at the repository. Bounded
        #: by the same counter as any other unproductive response, so a model
        #: that will not look still leaves.
        sent_back = 0
        attempted: list[tuple[str, str]] = []

        for _ in range(budget):
            token.raise_if_cancelled()

            frame = self._compiler.compile(
                FrameInputs(
                    phase=Phase.INSPECT,
                    task=task,
                    output_contract="InvestigationStep",
                    project_facts=project_facts,
                    latest_observation=latest,
                    attempted=tuple(attempted),
                    system_rules=_SYSTEM_RULES + "\n\n" + schema_hint(InvestigationStep),
                ),
                self._gateway.available(Phase.INSPECT),
            )

            try:
                decision = await self._decide(frame.render(), token)
            except ModelUnavailable as exc:
                result.stopped_because = f"model unavailable: {exc}"
                result.unavailable = True
                return result
            except ModelContractError as exc:
                # Recorded, not repaired. The runtime decides what a malformed
                # response means; the client does not get to guess.
                consecutive_failures += 1
                latest = (
                    "Your last response did not match the required JSON shape "
                    f"({exc}). Respond with valid JSON only."
                )
                if consecutive_failures >= self._limits.consecutive_failed_actions:
                    result.stopped_because = (
                        f"model produced {consecutive_failures} unparseable responses"
                    )
                    return result
                continue

            if decision.action == "conclude":
                # Concluding before looking at anything is not an investigation,
                # it is a guess with a citation-shaped surface. A live run
                # described a Node project with `package.json` and Jest in a
                # directory holding one Python file, having called no tool at
                # all — and nothing downstream can catch that, because a
                # question has no evidence gate to fail.
                #
                # Bounded rather than once: a 7B often answers the first
                # refusal with a *statement of intent* — "to understand this I
                # would need to identify the entry points" — which is another
                # conclusion, not a tool call. A second and third nudge are
                # cheap, and the bound is the same one that ends any other
                # unproductive exchange.
                unread = [path for path in must_read if path not in result.files_seen]
                blind = require_lookup and not result.observations

                if (blind or unread) and sent_back < self._limits.consecutive_failed_actions:
                    sent_back += 1
                    if unread:
                        listed = ", ".join(unread)
                        latest = (
                            f"You have not read {listed} yet, and the user asked about "
                            f"it specifically. Call file.read with path='{unread[0]}' "
                            "before concluding."
                        )
                    else:
                        latest = (
                            "You have not looked at anything in this repository yet, so "
                            "that is a guess rather than an answer. Do not describe what "
                            "you would do — call a tool now. `project.inspect` takes no "
                            "arguments and is the usual place to start."
                        )
                    continue

                result.conclusion = decision.conclusion
                result.stopped_because = "the agent concluded"
                result.ok = True
                return result

            assert decision.tool is not None  # guaranteed by validated()
            observation = await self._execute(decision.tool, token)
            result.observations.append(observation)
            attempted.append((_describe_attempt(decision.tool), _outcome(observation)))

            if observation.ok:
                consecutive_failures = 0
                self._record_file(result, decision.tool.arguments)
            else:
                consecutive_failures += 1
                if consecutive_failures >= self._limits.consecutive_failed_actions:
                    result.stopped_because = f"{consecutive_failures} consecutive failed actions"
                    return result

            latest = observation.summary

        result.stopped_because = f"action budget exhausted ({budget} actions)"
        return result

    async def plan(
        self,
        task: str,
        investigation: InvestigationResult,
        *,
        cancel: CancellationToken | None = None,
    ) -> ImplementationPlan | None:
        """Turn a completed investigation into a grounded plan.

        Returns None when the model cannot produce a valid plan. A missing plan
        is an honest outcome; a fabricated one is not.
        """
        token = cancel or NullCancellationToken()
        token.raise_if_cancelled()

        evidence = "\n\n".join(f"[{obs.tool}] {obs.summary}" for obs in investigation.observations)

        frame = self._compiler.compile(
            FrameInputs(
                phase=Phase.PLAN,
                task=task,
                output_contract="ImplementationPlan",
                latest_observation=evidence,
                previous_step_summary=investigation.conclusion,
                system_rules=(
                    "Produce an implementation plan from the evidence below. "
                    "You have not modified anything and must not claim to have. "
                    "List only files you actually inspected in `grounded_in`.\n\n"
                    + schema_hint(ImplementationPlan)
                ),
            ),
            # No tools: planning is a pure reasoning step over gathered
            # evidence. Offering tools here would invite another round of
            # investigation inside what is supposed to be a decision.
            (),
        )

        return await self._request_plan(frame, token)

    async def replan(
        self,
        task: str,
        *,
        previous_summary: str,
        completed: Sequence[str] = (),
        reason: str,
        cancel: CancellationToken | None = None,
    ) -> ImplementationPlan | None:
        """Produce a replacement plan after the current one stopped working.

        `completed` is what the previous plan already proved done. It is stated
        as a prohibition rather than left implicit: a model shown only the
        failure will cheerfully re-propose the steps that already succeeded,
        and re-running a verified change is how a working repository gets
        broken by a repair.

        Returns None when no valid plan comes back — the runtime then has a
        bounded re-plan that failed, which is a reportable outcome rather than
        a plan it has to second-guess.
        """
        token = cancel or NullCancellationToken()
        token.raise_if_cancelled()

        done = (
            "Already done — do NOT repeat these:\n" + "\n".join(f"- {title}" for title in completed)
            if completed
            else "Nothing has been completed yet."
        )

        frame = self._compiler.compile(
            FrameInputs(
                phase=Phase.PLAN,
                task=task,
                output_contract="ImplementationPlan",
                plan_summary=previous_summary,
                latest_observation=f"Why the previous plan was abandoned:\n{reason}",
                previous_step_summary=done,
                system_rules=(
                    "The previous plan did not work. Produce a replacement that "
                    "takes a different approach to the part that failed. Do not "
                    "restate the previous plan with different wording.\n\n"
                    + schema_hint(ImplementationPlan)
                ),
            ),
            (),
        )

        return await self._request_plan(frame, token)

    # -- internals ---------------------------------------------------------

    async def _request_plan(
        self, frame: ContextFrame, token: CancellationToken
    ) -> ImplementationPlan | None:
        request = ModelRequest(
            messages=(ModelMessage(role="user", content=frame.render()),),
            max_output_tokens=frame.budget.output_reserve,
        )
        try:
            return await self._model.generate_typed(request, ImplementationPlan, token)
        except ModelContractError:
            # The model answered and the answer was unusable. `None` is the
            # honest report of that, and the runtime turns it into a bounded
            # retry.
            return None
        # `ModelUnavailable` is deliberately *not* caught. A server that is down
        # or a model that was never pulled is an environment problem, and
        # folding it into "no usable plan" blames the model for the absence of
        # one — the report a user then acts on sends them to their prompt
        # instead of to `ollama serve` or `ollama pull`.

    async def _decide(self, prompt: str, token: CancellationToken) -> InvestigationStep:
        request = ModelRequest(
            messages=(ModelMessage(role="user", content=prompt),),
            max_output_tokens=self._compiler.budget.output_reserve,
        )
        decision = await self._model.generate_typed(request, InvestigationStep, token)
        try:
            return decision.validated()
        except ValueError as exc:
            raise ModelContractError(str(exc), raw_text=decision.model_dump_json()) from exc

    async def _execute(self, call: object, token: CancellationToken) -> Observation:
        from shamsu.models.contracts import ToolCall

        assert isinstance(call, ToolCall)
        request = ToolRequest(tool=call.tool, arguments=call.arguments)

        try:
            with self._gateway.decision():
                result: ToolResult = await self._gateway.invoke(request, Phase.INSPECT, token)
        except ToolPolicyViolation as exc:
            # A refusal is information the model can act on, not a crash. It is
            # reported back verbatim so the next decision can be better.
            return Observation(
                tool=call.tool, ok=False, summary=f"Refused: {exc}", reason=call.reason
            )

        body = result.output if result.ok else (result.error or "failed")
        return Observation(
            tool=call.tool,
            ok=result.ok,
            summary=self._compress(body),
            reason=call.reason,
        )

    def _compress(self, text: str) -> str:
        if len(text) <= self.OBSERVATION_CHARS:
            return text
        kept = text[: self.OBSERVATION_CHARS]
        return (
            f"{kept}\n[observation trimmed at {self.OBSERVATION_CHARS} characters; "
            "narrow the query to see more]"
        )

    @staticmethod
    def _record_file(result: InvestigationResult, arguments: object) -> None:
        """Track which files were actually read, so grounding can be checked."""
        if not isinstance(arguments, dict):
            return
        path = arguments.get("path")
        if isinstance(path, str) and path and path not in result.files_seen:
            result.files_seen.append(path)


def _describe_attempt(call: ToolCall) -> str:
    """A tool call as it appears in the "already tried" list.

    Arguments are included because they are what makes two calls different:
    `file.read` on two files is progress, and `file.read` on the same file
    twice is the loop this list exists to stop.
    """
    arguments = ", ".join(f"{name}={value!r}" for name, value in sorted(call.arguments.items()))
    return f"{call.tool}({arguments})" if arguments else f"{call.tool}()"


def _outcome(observation: Observation) -> str:
    """One line on how a call went, for the "already tried" list."""
    if not observation.ok:
        return f"failed: {observation.reason or observation.summary}".splitlines()[0][:90]
    first = observation.summary.strip().splitlines()
    return (first[0][:90] if first else "no output") or "no output"


def is_grounded(plan: ImplementationPlan, investigation: InvestigationResult) -> tuple[bool, str]:
    """Whether a plan only cites files the investigation actually opened.

    The check that makes "grounded" mean something. A plan naming files the
    agent never read is exactly the confident fabrication v2 exists to prevent,
    and it is cheap to detect because the runtime knows what was read.
    """
    seen = set(investigation.files_seen)
    invented = [path for path in plan.grounded_in if path not in seen]
    if invented:
        return False, f"plan cites files that were never read: {', '.join(sorted(invented))}"
    return True, ""


__all__ = [
    "InvestigationResult",
    "Observation",
    "ReadOnlyAgent",
    "is_grounded",
]
