"""A plan made only of investigation cannot carry out a change request.

Because an `investigate` step requires no evidence, such a plan passes every
gate it meets — so the runtime reports completion with nothing done. The §31.1
evaluation caught it live: qwen2.5-coder:7b, asked to fix a failing test,
planned "Inspect the Project Structure", "Identify Dependencies", "Review
Failing Tests". All three are legitimately read-only, all three passed, and
`temperature.py` was never touched.

That is a **false success**, which the evidence architecture exists to prevent,
and it is a regression risk of reading step kinds more generously — so it is
pinned here rather than left to be rediscovered.
"""

from __future__ import annotations

import pytest

from shamsu.agent.planning import asks_for_a_change, validate_plan
from shamsu.models.contracts import ImplementationPlan, PlanStepProposal


def plan_of(*titles: str, files: tuple[str, ...] = ()) -> ImplementationPlan:
    return ImplementationPlan(
        summary="a plan",
        steps=tuple(PlanStepProposal(title=title, files=files) for title in titles),
    )


#: Verbatim from the eval run that produced the false success.
OBSERVED = ("Inspect the Project Structure", "Identify Dependencies", "Review Failing Tests")


class TestRequestClassification:
    @pytest.mark.parametrize(
        "asked",
        [
            "fix temperature.py so the tests pass",
            "Add an 'Installation' section to README.md",
            "refactor the grade function",
            "charge() must raise a ValueError when amount is negative",
            "remove the legacy module",
        ],
    )
    def test_change_requests_are_recognised(self, asked: str) -> None:
        assert asks_for_a_change(asked) is True

    @pytest.mark.parametrize(
        "asked",
        [
            "explain how the caching works",
            "which module owns routing?",
            "summarise the architecture",
            # A verb used as a symbol name is not an instruction.
            "look at add()",
            "what does update() return?",
            "trace where build() is called",
            # A copula in front makes it a predicate, not an instruction.
            "confirm add() is correct",
            "check whether the config is valid",
        ],
    )
    def test_questions_are_not_change_requests(self, asked: str) -> None:
        assert asks_for_a_change(asked) is False


class TestAnAllInvestigatePlanIsRejected:
    def test_the_observed_plan_is_refused(self) -> None:
        validation = validate_plan(
            plan_of(*OBSERVED),
            request="The tests in test_temperature.py are failing. Fix temperature.py.",
        )
        assert validation.ok is False
        assert any("only of investigation" in problem for problem in validation.problems)

    def test_one_change_step_is_enough(self) -> None:
        validation = validate_plan(
            plan_of("Review the failing test", "Fix temperature.py"),
            request="Fix temperature.py so the tests pass",
        )
        assert validation.ok is True

    def test_a_question_may_be_answered_by_investigation_alone(self) -> None:
        """Not every request wants an edit; those plans must still be allowed."""
        validation = validate_plan(
            plan_of(*OBSERVED), request="explain how temperature conversion works"
        )
        assert validation.ok is True

    def test_plan_mode_is_exempt(self) -> None:
        """In read-only mode a plan with no change step is the entire point."""
        validation = validate_plan(
            plan_of(*OBSERVED),
            request="Fix temperature.py so the tests pass",
            read_only=True,
        )
        assert validation.ok is True

    def test_the_check_is_skipped_without_a_request(self) -> None:
        """Callers that pass no request keep the previous behaviour."""
        assert validate_plan(plan_of(*OBSERVED)).ok is True


class TestItDoesNotOverreach:
    def test_a_named_file_does_not_make_an_investigation_a_change(self) -> None:
        """The verb-first rule still holds; this check is about the plan, not the step."""
        validation = validate_plan(
            plan_of("Review the failing test", files=("temperature.py",)),
            request="Fix temperature.py",
        )
        assert validation.ok is False

    def test_a_plan_whose_steps_all_change_is_fine(self) -> None:
        validation = validate_plan(
            plan_of("Fix temperature.py", "Update the docstring"),
            request="Fix temperature.py",
        )
        assert validation.ok is True
