"""Letting the model decide change-or-question, without depending on it.

A pattern list stops scaling at the change/question boundary: "the login is
broken, sort it out" names no verb any vocabulary would enumerate. A model
reads it at once — but the classifier must not become a thing that breaks when
the model is down, and the cases that are already certain must not start
costing a round trip.
"""

from __future__ import annotations

import asyncio

import pytest

from shamsu.agent.triage import Intent, classify, triage
from shamsu.interfaces.cancellation import CancellationToken, NullCancellationToken
from shamsu.interfaces.models import (
    ModelContractError,
    ModelRequest,
    ModelResponse,
    ModelTimeout,
    ModelUnavailable,
)
from shamsu.models.contracts import RequestIntent


class _Classifier:
    """Answers with a fixed intent and records what it was asked."""

    context_tokens = 8192

    def __init__(self, intent: str = "question") -> None:
        self._intent = intent
        self.calls = 0
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "stub"

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def generate(self, request: ModelRequest, cancel: CancellationToken) -> ModelResponse:
        raise AssertionError("classification must use the typed contract")

    async def generate_typed(
        self, request: ModelRequest, contract: object, cancel: CancellationToken
    ) -> object:
        self.calls += 1
        self.prompts.append(request.messages[0].content)
        return RequestIntent(intent=self._intent, reason="stub")  # type: ignore[arg-type]


class _Broken(_Classifier):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def generate_typed(
        self, request: ModelRequest, contract: object, cancel: CancellationToken
    ) -> object:
        self.calls += 1
        raise self._error


def decide(request: str, model: object | None) -> Intent:
    return asyncio.run(classify(request, model, cancel=NullCancellationToken()))  # type: ignore[arg-type]


class TestTheSettledCasesCostNothing:
    """A greeting must not become the slowest interaction in the tool."""

    @pytest.mark.parametrize(
        ("request_text", "expected"),
        [
            ("hi", Intent.CHAT),
            ("thanks", Intent.CHAT),
            ("", Intent.EMPTY),
            ("what can you do?", Intent.CAPABILITIES),
            ("what tools do you have", Intent.CAPABILITIES),
        ],
    )
    def test_they_never_reach_the_model(self, request_text: str, expected: Intent) -> None:
        model = _Classifier("change")
        assert decide(request_text, model) is expected
        assert model.calls == 0, "a settled case paid for a round trip"


class TestTheModelDecidesTheHardBoundary:
    def test_it_can_route_to_work(self) -> None:
        model = _Classifier("change")
        assert decide("the login is broken, sort it out", model) is Intent.TASK

    def test_it_can_route_to_an_answer(self) -> None:
        model = _Classifier("question")
        assert decide("fix the login bug", model) is Intent.QUESTION

    def test_it_overrides_the_pattern_rule(self) -> None:
        """The point of asking: the vocabulary has no verb for this phrasing."""
        phrasing = "the login is broken, sort it out"
        assert triage(phrasing) is Intent.QUESTION
        assert decide(phrasing, _Classifier("change")) is Intent.TASK

    def test_the_prompt_carries_the_request(self) -> None:
        model = _Classifier()
        decide("why is the parser slow", model)
        assert "why is the parser slow" in model.prompts[0]

    def test_it_is_told_to_prefer_answering_when_unsure(self) -> None:
        """Answering is cheap and reversible; an unwanted change is not."""
        model = _Classifier()
        decide("something about the parser", model)
        assert "not certain" in model.prompts[0]
        assert "'question'" in model.prompts[0]


class TestItFallsBackRatherThanFailing:
    @pytest.mark.parametrize(
        "error",
        [
            ModelUnavailable("ollama is down"),
            ModelTimeout("took too long"),
            ModelContractError("not json", raw_text="{"),
        ],
    )
    def test_a_model_that_cannot_answer_uses_the_pattern_rule(self, error: Exception) -> None:
        model = _Broken(error)
        assert decide("fix the login bug", model) is Intent.TASK
        assert model.calls == 1

    def test_no_model_at_all_still_triages(self) -> None:
        """The deterministic rule is a serviceable second opinion, not a stub."""
        assert decide("fix the login bug", None) is Intent.TASK
        assert decide("what is this project", None) is Intent.QUESTION

    def test_a_greeting_still_works_with_a_dead_model(self) -> None:
        assert decide("hi", _Broken(ModelUnavailable("down"))) is Intent.CHAT


class TestTriageStaysPure:
    def test_it_needs_no_model_and_no_event_loop(self) -> None:
        """`triage` is the fallback and the thing every other test asserts on.

        It has to stay a pure function of a string — a classifier that could
        only be exercised with a server is one nothing else can rely on.
        """
        assert triage("fix the login bug") is Intent.TASK
        assert triage("what is this project") is Intent.QUESTION
