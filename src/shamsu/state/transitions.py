"""The state machine, as data.

v1's control flow was implicit: it emerged from the order of `if` branches in a
long loop body, which meant nobody could answer "can it go from here to there?"
without simulating it. Here the graph is a table. It can be printed, diffed,
tested for reachability, and asserted on.

The runtime consults this before every transition. A move that is not in the
table is a bug in the runtime, not a recoverable condition -- so it raises.
"""

from __future__ import annotations

from collections.abc import Mapping

from shamsu.interfaces.enums import AgentState, StepOutcome, TaskKind

# Terminal states. Nothing leaves these.
TERMINAL: frozenset[AgentState] = frozenset(
    {
        AgentState.FINAL_REPORT,
        AgentState.STOPPED,
        AgentState.BLOCKED,
    }
)

# The graph from plan section 10.
TRANSITIONS: Mapping[AgentState, frozenset[AgentState]] = {
    AgentState.RECEIVE_TASK: frozenset({AgentState.LOAD_PROJECT_STATE}),
    AgentState.LOAD_PROJECT_STATE: frozenset({AgentState.INSPECT_PROJECT}),
    # BLOCKED is reachable from the three states below because each depends on
    # a model call that can simply not produce a usable answer. Leaving those
    # edges out described a machine that cannot fail at planning time, and the
    # first runtime to actually drive this table hit all three.
    AgentState.INSPECT_PROJECT: frozenset({AgentState.CLASSIFY_TASK, AgentState.BLOCKED}),
    # DIRECT tasks skip planning; PLANNED tasks go through it.
    AgentState.CLASSIFY_TASK: frozenset({AgentState.CREATE_PLAN, AgentState.EXECUTE_CURRENT_STEP}),
    AgentState.CREATE_PLAN: frozenset({AgentState.VALIDATE_PLAN, AgentState.BLOCKED}),
    # A plan that fails validation is regenerated rather than executed -- until
    # regenerating stops helping, which is what the BLOCKED edge is for.
    AgentState.VALIDATE_PLAN: frozenset(
        {AgentState.APPROVAL_CHECK, AgentState.CREATE_PLAN, AgentState.BLOCKED}
    ),
    AgentState.APPROVAL_CHECK: frozenset(
        {AgentState.EXECUTE_CURRENT_STEP, AgentState.WAIT_APPROVAL}
    ),
    # Denial ends the run. It does not fall through to execution.
    AgentState.WAIT_APPROVAL: frozenset({AgentState.EXECUTE_CURRENT_STEP, AgentState.STOPPED}),
    AgentState.EXECUTE_CURRENT_STEP: frozenset({AgentState.VERIFY_CURRENT_STEP}),
    AgentState.VERIFY_CURRENT_STEP: frozenset(
        {
            AgentState.CREATE_CHECKPOINT,
            AgentState.REPAIR,
            AgentState.REPLAN,
            AgentState.WAIT_APPROVAL,
            AgentState.STOPPED,
            AgentState.BLOCKED,
        }
    ),
    AgentState.CREATE_CHECKPOINT: frozenset({AgentState.CHECK_REMAINING_STEPS}),
    # Repair retries the step, or gives up. It never declares success itself.
    AgentState.REPAIR: frozenset({AgentState.EXECUTE_CURRENT_STEP, AgentState.BLOCKED}),
    AgentState.REPLAN: frozenset({AgentState.CREATE_PLAN, AgentState.BLOCKED}),
    AgentState.CHECK_REMAINING_STEPS: frozenset(
        {AgentState.EXECUTE_CURRENT_STEP, AgentState.FINAL_VERIFICATION}
    ),
    AgentState.FINAL_VERIFICATION: frozenset({AgentState.COMPLETION_GATE}),
    # The gate can refuse. Reaching it is not the same as passing it.
    AgentState.COMPLETION_GATE: frozenset(
        {AgentState.FINAL_REPORT, AgentState.REPLAN, AgentState.BLOCKED}
    ),
    AgentState.FINAL_REPORT: frozenset(),
    AgentState.STOPPED: frozenset(),
    AgentState.BLOCKED: frozenset(),
}


class InvalidTransition(Exception):
    """An illegal state move was attempted.

    Always a runtime bug. Never caught and worked around -- silently coercing
    an illegal transition into a legal one is how v1's control flow became
    impossible to reason about.
    """

    def __init__(self, source: AgentState, target: AgentState) -> None:
        allowed = sorted(state.value for state in TRANSITIONS.get(source, frozenset()))
        super().__init__(
            f"illegal transition {source.value} -> {target.value}; "
            f"allowed from {source.value}: {allowed or '(terminal)'}"
        )
        self.source = source
        self.target = target


def is_terminal(state: AgentState) -> bool:
    """Whether the machine has finished."""
    return state in TERMINAL


def allowed_from(state: AgentState) -> frozenset[AgentState]:
    """Legal successors of `state`, excluding cancellation."""
    return TRANSITIONS.get(state, frozenset())


def can_transition(source: AgentState, target: AgentState, *, cancelling: bool = False) -> bool:
    """Whether `source -> target` is legal.

    Cancellation is deliberately a separate parameter rather than an edge from
    every node to STOPPED. Modelling it as ordinary edges would make STOPPED
    reachable from everywhere in the table and hide the real shape of the
    graph; making it a flag keeps the table readable and keeps "was this a
    cancellation?" an explicit question at the call site.
    """
    if cancelling:
        # A cancelled run stops from anywhere it has not already finished.
        return target is AgentState.STOPPED and not is_terminal(source)
    return target in TRANSITIONS.get(source, frozenset())


def assert_transition(source: AgentState, target: AgentState, *, cancelling: bool = False) -> None:
    """Raise `InvalidTransition` unless the move is legal."""
    if not can_transition(source, target, cancelling=cancelling):
        raise InvalidTransition(source, target)


def next_after_classification(kind: TaskKind) -> AgentState:
    """Where CLASSIFY_TASK goes, given the classifier's answer."""
    return AgentState.EXECUTE_CURRENT_STEP if kind is TaskKind.DIRECT else AgentState.CREATE_PLAN


def next_after_verification(outcome: StepOutcome) -> AgentState:
    """Where VERIFY_CURRENT_STEP goes, given the step outcome.

    Total over `StepOutcome`: adding a member without handling it here is a
    mypy error rather than a silent fall-through to some default branch.
    """
    mapping: Mapping[StepOutcome, AgentState] = {
        StepOutcome.PASS: AgentState.CREATE_CHECKPOINT,
        StepOutcome.REPAIRABLE: AgentState.REPAIR,
        StepOutcome.PLAN_INVALID: AgentState.REPLAN,
        StepOutcome.APPROVAL_REQUIRED: AgentState.WAIT_APPROVAL,
        StepOutcome.CANCELLED: AgentState.STOPPED,
        StepOutcome.BLOCKED: AgentState.BLOCKED,
    }
    return mapping[outcome]


def reachable_from(start: AgentState = AgentState.RECEIVE_TASK) -> frozenset[AgentState]:
    """Every state reachable from `start`, following the table.

    Used by tests to prove there are no orphaned states -- a node nothing can
    reach is either dead code or a missing edge, and both are worth failing on.
    """
    seen: set[AgentState] = set()
    frontier = [start]
    while frontier:
        state = frontier.pop()
        if state in seen:
            continue
        seen.add(state)
        frontier.extend(TRANSITIONS.get(state, frozenset()))
    return frozenset(seen)


__all__ = [
    "TERMINAL",
    "TRANSITIONS",
    "InvalidTransition",
    "allowed_from",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "next_after_classification",
    "next_after_verification",
    "reachable_from",
]
