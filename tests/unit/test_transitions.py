"""The state machine graph.

v1's control flow was implicit in the order of `if` branches, so "can it go
from here to there?" could only be answered by simulation. These tests exist
because the answer is now a fact about a table.
"""

from __future__ import annotations

import pytest

from shamsu.interfaces.enums import AgentState, StepOutcome, TaskKind
from shamsu.state.transitions import (
    TERMINAL,
    TRANSITIONS,
    InvalidTransition,
    allowed_from,
    assert_transition,
    can_transition,
    is_terminal,
    next_after_classification,
    next_after_verification,
    reachable_from,
)


class TestGraphIntegrity:
    def test_every_state_has_an_entry(self) -> None:
        """A missing entry silently becomes a dead end at runtime."""
        assert set(TRANSITIONS) == set(AgentState)

    def test_every_state_is_reachable_from_the_start(self) -> None:
        """An unreachable node is either dead code or a missing edge."""
        assert reachable_from(AgentState.RECEIVE_TASK) == frozenset(AgentState)

    def test_only_terminal_states_are_dead_ends(self) -> None:
        dead_ends = {state for state, targets in TRANSITIONS.items() if not targets}
        assert dead_ends == set(TERMINAL)

    def test_terminal_states_go_nowhere(self) -> None:
        for state in TERMINAL:
            assert allowed_from(state) == frozenset()
            assert is_terminal(state)

    def test_every_target_is_a_real_state(self) -> None:
        for targets in TRANSITIONS.values():
            assert targets <= set(AgentState)


class TestHappyPath:
    def test_the_planned_route_walks_end_to_end(self) -> None:
        """Plan section 10's main line must be traversable one edge at a time."""
        route = [
            AgentState.RECEIVE_TASK,
            AgentState.LOAD_PROJECT_STATE,
            AgentState.INSPECT_PROJECT,
            AgentState.CLASSIFY_TASK,
            AgentState.CREATE_PLAN,
            AgentState.VALIDATE_PLAN,
            AgentState.APPROVAL_CHECK,
            AgentState.EXECUTE_CURRENT_STEP,
            AgentState.VERIFY_CURRENT_STEP,
            AgentState.CREATE_CHECKPOINT,
            AgentState.CHECK_REMAINING_STEPS,
            AgentState.FINAL_VERIFICATION,
            AgentState.COMPLETION_GATE,
            AgentState.FINAL_REPORT,
        ]
        for source, target in zip(route, route[1:], strict=False):
            assert_transition(source, target)

    def test_a_direct_task_skips_planning(self) -> None:
        assert next_after_classification(TaskKind.DIRECT) is AgentState.EXECUTE_CURRENT_STEP
        assert_transition(AgentState.CLASSIFY_TASK, AgentState.EXECUTE_CURRENT_STEP)

    def test_a_planned_task_goes_through_planning(self) -> None:
        assert next_after_classification(TaskKind.PLANNED) is AgentState.CREATE_PLAN

    def test_multiple_steps_loop_back_to_execution(self) -> None:
        assert_transition(AgentState.CHECK_REMAINING_STEPS, AgentState.EXECUTE_CURRENT_STEP)


class TestIllegalMoves:
    @pytest.mark.parametrize(
        ("source", "target"),
        [
            # Execution must not bypass verification.
            (AgentState.EXECUTE_CURRENT_STEP, AgentState.CREATE_CHECKPOINT),
            (AgentState.EXECUTE_CURRENT_STEP, AgentState.FINAL_REPORT),
            # The completion gate must not be skipped.
            (AgentState.FINAL_VERIFICATION, AgentState.FINAL_REPORT),
            (AgentState.CHECK_REMAINING_STEPS, AgentState.FINAL_REPORT),
            # Planning must be validated before it is executed.
            (AgentState.CREATE_PLAN, AgentState.EXECUTE_CURRENT_STEP),
            # A pending approval must not fall through to execution.
            (AgentState.APPROVAL_CHECK, AgentState.VERIFY_CURRENT_STEP),
            # Nothing restarts from a terminal state.
            (AgentState.FINAL_REPORT, AgentState.EXECUTE_CURRENT_STEP),
            (AgentState.STOPPED, AgentState.EXECUTE_CURRENT_STEP),
            (AgentState.BLOCKED, AgentState.REPAIR),
        ],
    )
    def test_rejected(self, source: AgentState, target: AgentState) -> None:
        assert can_transition(source, target) is False
        with pytest.raises(InvalidTransition):
            assert_transition(source, target)

    def test_the_error_names_what_was_allowed(self) -> None:
        """A transition bug should be diagnosable from the message alone."""
        with pytest.raises(InvalidTransition) as excinfo:
            assert_transition(AgentState.EXECUTE_CURRENT_STEP, AgentState.FINAL_REPORT)
        message = str(excinfo.value)
        assert "execute_current_step -> final_report" in message
        assert "verify_current_step" in message

    def test_repair_cannot_declare_success(self) -> None:
        """Repair retries or gives up; it never marks a step verified itself."""
        assert can_transition(AgentState.REPAIR, AgentState.CREATE_CHECKPOINT) is False
        assert can_transition(AgentState.REPAIR, AgentState.EXECUTE_CURRENT_STEP) is True
        assert can_transition(AgentState.REPAIR, AgentState.BLOCKED) is True


class TestCancellation:
    @pytest.mark.parametrize("source", [state for state in AgentState if state not in TERMINAL])
    def test_any_live_state_can_be_cancelled(self, source: AgentState) -> None:
        """Every live run must be cancellable -- the defect v2 exists to fix."""
        assert can_transition(source, AgentState.STOPPED, cancelling=True) is True

    @pytest.mark.parametrize("source", sorted(TERMINAL))
    def test_a_finished_run_cannot_be_cancelled(self, source: AgentState) -> None:
        assert can_transition(source, AgentState.STOPPED, cancelling=True) is False

    def test_cancelling_does_not_unlock_other_moves(self) -> None:
        """The cancel flag permits STOPPED only, not arbitrary transitions."""
        assert (
            can_transition(
                AgentState.EXECUTE_CURRENT_STEP, AgentState.FINAL_REPORT, cancelling=True
            )
            is False
        )


class TestVerificationOutcomes:
    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (StepOutcome.PASS, AgentState.CREATE_CHECKPOINT),
            (StepOutcome.REPAIRABLE, AgentState.REPAIR),
            (StepOutcome.PLAN_INVALID, AgentState.REPLAN),
            (StepOutcome.APPROVAL_REQUIRED, AgentState.WAIT_APPROVAL),
            (StepOutcome.CANCELLED, AgentState.STOPPED),
            (StepOutcome.BLOCKED, AgentState.BLOCKED),
        ],
    )
    def test_routing(self, outcome: StepOutcome, expected: AgentState) -> None:
        assert next_after_verification(outcome) is expected

    def test_every_outcome_is_handled(self) -> None:
        """Adding a StepOutcome without routing it must not fall through."""
        for outcome in StepOutcome:
            assert next_after_verification(outcome) in AgentState

    def test_every_routed_target_is_a_legal_edge(self) -> None:
        """The routing helper must not propose a move the table forbids."""
        for outcome in StepOutcome:
            target = next_after_verification(outcome)
            assert can_transition(AgentState.VERIFY_CURRENT_STEP, target), outcome
