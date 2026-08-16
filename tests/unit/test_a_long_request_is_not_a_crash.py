"""A detailed request must not take the run down before it starts.

`_direct_plan` builds a one-step plan without a model call, and set
``summary=f"Direct change: {request}"`` from whatever the user typed.
`ImplementationPlan.summary` is capped at 1000 characters, so a long-but-
entirely-ordinary request raised `pydantic_core.ValidationError` out of the
planner, through `AgentSession.run`, and out of `main` as a traceback:

    pydantic_core._pydantic_core.ValidationError: 1 validation error for
    ImplementationPlan
    summary
      String should have at most 1000 characters

The prompt that did it was a 1108-character description of a Django model —
the kind of detail the planner most wants. `title` and `intent` in the same
constructor were already truncated; `summary` was simply missed.
"""

from __future__ import annotations

from shamsu.models.contracts import ImplementationPlan
from shamsu.runtime.session import _direct_plan

#: Longer than the 1000-character cap, in the shape that caused it: one
#: request enumerating the fields of a model.
LONG_REQUEST = (
    "Add a third model, Item, to the existing marketplace/models.py. "
    + "Give it a plain field assigned directly in the body of class Item. " * 20
)


class TestTheRequestIsTruncatedNotRejected:
    def test_a_request_over_the_cap_still_produces_a_plan(self) -> None:
        assert len(LONG_REQUEST) > 1000, "the fixture has to exceed the cap to test it"
        plan = _direct_plan(LONG_REQUEST, ["marketplace/models.py"])
        assert isinstance(plan, ImplementationPlan)

    def test_the_summary_fits_the_contract(self) -> None:
        plan = _direct_plan(LONG_REQUEST, ["marketplace/models.py"])
        assert len(plan.summary) <= 1000

    def test_the_step_still_names_its_file(self) -> None:
        """Truncating the summary must not cost the plan its target."""
        plan = _direct_plan(LONG_REQUEST, ["marketplace/models.py"])
        assert plan.steps[0].files == ("marketplace/models.py",)

    def test_a_short_request_is_unchanged(self) -> None:
        plan = _direct_plan("Fix the adder", ["calc.py"])
        assert plan.summary == "Direct change: Fix the adder"

    def test_the_summary_still_starts_with_the_request(self) -> None:
        """Truncation from the end keeps the part that identifies the task."""
        plan = _direct_plan(LONG_REQUEST, ["marketplace/models.py"])
        assert plan.summary.startswith("Direct change: Add a third model, Item")
