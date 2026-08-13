"""The approval gate: every path that must not become a yes.

`WAIT_APPROVAL` used to return STOPPED with "no approver is configured", so a
HIGH-risk step could not proceed at all. Now it can — which makes the ways it
must *not* proceed the interesting tests.
"""

from __future__ import annotations

import asyncio
import io

import pytest

from shamsu.interfaces.cancellation import (
    CancellationToken,
    Cancelled,
    NullCancellationToken,
)
from shamsu.interfaces.enums import ApprovalDecision, Risk
from shamsu.interfaces.ids import PlanId, StepId
from shamsu.state.records import PlanStepRecord
from shamsu.ui.approval import (
    AlwaysApprover,
    ConsoleApprover,
    DenyingApprover,
    ScreenApprover,
    approver_for,
    describe,
    parse_answer,
)


def a_step() -> PlanStepRecord:
    return PlanStepRecord(
        step_id=StepId("s1"),
        plan_id=PlanId("p1"),
        ordinal=0,
        title="Delete the legacy migrations",
        inputs=("db/migrations/0001_initial.py",),
        acceptance_criteria=("the suite still passes",),
        risk=Risk.HIGH,
        approval_required=True,
    )


class _Cancelled(NullCancellationToken):
    """A token that is already cancelled."""

    @property
    def cancelled(self) -> bool:
        return True

    @property
    def reason(self) -> str:
        return "stopped by the user"

    def raise_if_cancelled(self) -> None:
        raise Cancelled(self.reason)

    async def wait_cancelled(self) -> None:
        return None


def decide(approver: object, token: CancellationToken | None = None) -> ApprovalDecision:
    return asyncio.run(
        approver.decide(  # type: ignore[attr-defined]
            a_step(),
            reason="step 1 is high risk",
            cancel=token or NullCancellationToken(),
        )
    )


class TestOnlyAnExplicitYesApproves:
    @pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", " yes "])
    def test_yes_approves(self, answer: str) -> None:
        assert parse_answer(answer) is ApprovalDecision.APPROVED

    @pytest.mark.parametrize(
        "answer",
        ["", " ", "n", "no", "N", "nope", "maybe", "ok", "sure", "yy", "yep", "1", "true"],
    )
    def test_everything_else_denies(self, answer: str) -> None:
        """Including 'ok' and 'sure' — the vocabulary is deliberately closed."""
        assert parse_answer(answer) is ApprovalDecision.DENIED

    def test_leaning_on_return_is_safe(self) -> None:
        assert parse_answer("\n") is ApprovalDecision.DENIED


class TestTheQuestionSaysWhatWillHappen:
    def test_it_names_the_files(self) -> None:
        rendered = "\n".join(describe(a_step(), "step 1 is high risk"))
        assert "db/migrations/0001_initial.py" in rendered

    def test_it_states_the_default(self) -> None:
        rendered = "\n".join(describe(a_step(), "step 1 is high risk"))
        assert "denies" in rendered


class TestNonInteractiveCannotApprove:
    def test_a_denying_approver_denies(self) -> None:
        assert decide(DenyingApprover()) is ApprovalDecision.DENIED

    def test_a_pipe_gets_the_denying_approver(self) -> None:
        """A CI run must not authorise a destructive step because nobody looked."""
        assert isinstance(approver_for(stream=io.StringIO()), DenyingApprover)

    def test_explicit_yes_is_the_only_way_to_preapprove(self) -> None:
        assert isinstance(approver_for(assume_yes=True, stream=io.StringIO()), AlwaysApprover)

    def test_always_approver_approves(self) -> None:
        assert decide(AlwaysApprover()) is ApprovalDecision.APPROVED

    def test_even_preapproval_observes_cancellation(self) -> None:
        with pytest.raises(Cancelled):
            decide(AlwaysApprover(), _Cancelled())


class TestConsoleApprover:
    def test_a_typed_yes_approves(self) -> None:
        approver = ConsoleApprover(read_line=lambda _: "y", stream=io.StringIO())
        assert decide(approver) is ApprovalDecision.APPROVED

    def test_a_typed_no_denies(self) -> None:
        approver = ConsoleApprover(read_line=lambda _: "n", stream=io.StringIO())
        assert decide(approver) is ApprovalDecision.DENIED

    def test_a_closed_stdin_times_out_rather_than_approving(self) -> None:
        """EOF is not a person saying yes."""

        def closed(_: str) -> str:
            raise EOFError

        approver = ConsoleApprover(read_line=closed, stream=io.StringIO())
        assert decide(approver) is ApprovalDecision.TIMED_OUT

    def test_silence_times_out(self) -> None:
        """The property the whole class exists for: waiting is not consenting."""

        def never(_: str) -> str:
            import time

            time.sleep(30)
            return "y"

        approver = ConsoleApprover(read_line=never, stream=io.StringIO(), timeout=0.05)
        assert decide(approver) is ApprovalDecision.TIMED_OUT

    def test_the_question_is_shown_before_it_is_asked(self) -> None:
        stream = io.StringIO()
        decide(ConsoleApprover(read_line=lambda _: "y", stream=stream))
        assert "approval required" in stream.getvalue()


class TestScreenApprover:
    def test_a_y_keypress_approves(self) -> None:
        approver = ScreenApprover()

        async def scenario() -> ApprovalDecision:
            work = asyncio.ensure_future(
                approver.decide(a_step(), reason="r", cancel=NullCancellationToken())
            )
            while not approver.waiting:
                await asyncio.sleep(0)
            approver.answer_key("y")
            return await work

        assert asyncio.run(scenario()) is ApprovalDecision.APPROVED

    def test_any_other_keypress_denies(self) -> None:
        approver = ScreenApprover()

        async def scenario() -> ApprovalDecision:
            work = asyncio.ensure_future(
                approver.decide(a_step(), reason="r", cancel=NullCancellationToken())
            )
            while not approver.waiting:
                await asyncio.sleep(0)
            approver.answer_key("\x03")  # Ctrl-C denies rather than cancelling
            return await work

        assert asyncio.run(scenario()) is ApprovalDecision.DENIED

    def test_an_unanswered_question_times_out(self) -> None:
        approver = ScreenApprover(timeout=0.05)
        assert decide(approver) is ApprovalDecision.TIMED_OUT

    def test_it_stops_waiting_once_answered(self) -> None:
        approver = ScreenApprover()

        async def scenario() -> bool:
            work = asyncio.ensure_future(
                approver.decide(a_step(), reason="r", cancel=NullCancellationToken())
            )
            while not approver.waiting:
                await asyncio.sleep(0)
            approver.answer_key("y")
            await work
            return approver.waiting

        assert asyncio.run(scenario()) is False

    def test_answering_when_nothing_is_open_is_harmless(self) -> None:
        assert ScreenApprover().answer_key("y") is False

    def test_an_empty_key_is_not_an_answer(self) -> None:
        """A frame with no keypress must not be read as a denial."""
        approver = ScreenApprover()

        async def scenario() -> bool:
            work = asyncio.ensure_future(
                approver.decide(a_step(), reason="r", cancel=NullCancellationToken())
            )
            while not approver.waiting:
                await asyncio.sleep(0)
            answered = approver.answer_key("")
            approver.answer_key("n")
            await work
            return answered

        assert asyncio.run(scenario()) is False
