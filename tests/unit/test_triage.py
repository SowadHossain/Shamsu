"""Triage: deciding whether an input is a task before a run exists.

The asymmetry these tests protect is the whole point. A false CHAT answers a
real request with pleasantries and does no work; a false TASK spends a plan on
a greeting. So `test_real_work_is_never_chat` is the class that must never
regress, and it is deliberately the longer list.
"""

from __future__ import annotations

import asyncio

import pytest

from shamsu.agent.triage import FALLBACK_REPLY, Intent, respond, triage
from shamsu.interfaces.cancellation import NullCancellationToken
from shamsu.interfaces.models import (
    ModelRequest,
    ModelResponse,
    ModelTimeout,
    ModelUnavailable,
)


class TestConversationIsRecognised:
    @pytest.mark.parametrize(
        "message",
        [
            "hi",
            "Hi!",
            "hello",
            "Hello there",
            "hey",
            "hey there",
            "yo",
            "howdy",
            "good morning",
            "Good evening!",
            "how are you",
            "hey how are you",
            "hi, how are you?",
            "how's it going",
            "what's up",
            "whats up",
            "who are you",
            "are you there",
            "thanks",
            "thank you",
            "thanks!",
            "ok",
            "cool",
            "nice",
            "got it",
            "makes sense",
            "bye",
            "good night",
            "ping",
            "test",
        ],
    )
    def test_small_talk_is_chat(self, message: str) -> None:
        assert triage(message) is Intent.CHAT

    @pytest.mark.parametrize("message", ["", "   ", "\n\t ", "...", "???", "!!"])
    def test_nothing_typed_is_empty(self, message: str) -> None:
        assert triage(message) is Intent.EMPTY


class TestRealWorkIsNeverChat:
    """The expensive error: answering a request for work with pleasantries."""

    @pytest.mark.parametrize(
        "message",
        [
            "fix the login bug",
            "hey, fix the login bug",
            "hi, can you add a test for the parser",
            "add a health check endpoint",
            "thanks, now update the README",
            "good morning, please refactor the planner",
        ],
    )
    def test_work_reaches_the_state_machine(self, message: str) -> None:
        assert triage(message) is Intent.TASK

    @pytest.mark.parametrize(
        "message",
        [
            "explain the caching",
            "how does auth work",
            "how do I run the tests",
            "what is this project",
            "what are the entry points",
            "who calls parse_response",
            "is the build passing",
            "why does the gate refuse",
            "summarise the architecture",
            "hello, what does this project do",
        ],
    )
    def test_questions_are_answered_not_planned(self, message: str) -> None:
        """These used to be tasks, and a task can only change and prove.

        "what can you do?" went plan → author → verify → repair → BLOCKED on
        "the failure implicates no editable file". A question has nothing to
        change and nothing to prove.
        """
        assert triage(message) is Intent.QUESTION

    def test_a_question_is_still_never_chat(self) -> None:
        """The original property these cases were written to protect."""
        for message in ("explain the caching", "how does auth work", "what is this project"):
            assert triage(message) is not Intent.CHAT


class TestQuestionsAboutShamsuItself:
    @pytest.mark.parametrize(
        "message",
        [
            "what can you do?",
            "what can you do",
            "what are your capabilities",
            "what tools do you have",
            "what commands do you support",
            "list your tools",
            "show me the available commands",
            "help",
            "hey, what tools do you have?",
        ],
    )
    def test_they_are_recognised(self, message: str) -> None:
        assert triage(message) is Intent.CAPABILITIES

    def test_a_question_about_the_code_is_not_one_about_shamsu(self) -> None:
        """`file.list` is a tool; "what files are here" is about the repository."""
        assert triage("what files are in src") is Intent.QUESTION


class TestAnythingUnrecognisedIsAnswered:
    """The property that replaced a growing list of question shapes.

    Triage asks one question — "does this ask for a change?" — and answers
    everything else. None of these were ever enumerated, and none need to be.
    """

    @pytest.mark.parametrize(
        "message",
        [
            "wtf does this thing do",
            "any idea why this breaks",
            "i dont understand the gate",
            "whats going on with the tests",
            "the tests are failing",
            "no clue what this module is for",
            "something is wrong with the parser",
            "walk me through the flow",
            "do you support typescript",
            "does the parser handle unicode",
            "is there a config file",
        ],
    )
    def test_they_are_answered_not_planned(self, message: str) -> None:
        assert triage(message) is Intent.QUESTION

    def test_a_long_rambling_message_is_still_answered(self) -> None:
        """The word ceiling bounds CHAT, not QUESTION."""
        rambling = (
            "so I was looking at this thing earlier and I really cannot work out "
            "what on earth it is supposed to be doing at all"
        )
        assert triage(rambling) is Intent.QUESTION

    def test_a_long_message_is_never_chat(self) -> None:
        """The word ceiling is the backstop under CHAT specifically."""
        padded = "thanks " * 20
        assert triage(padded) is not Intent.CHAT

    def test_a_greeting_prefix_does_not_launder_a_request(self) -> None:
        assert triage("hey there, delete the old migrations") is Intent.TASK


class TestCapabilitiesAreReadOffTheContracts:
    def test_it_lists_the_real_tools(self) -> None:
        """Not a hand-written blurb: a model asked to describe its own tools
        confidently names ones it does not have, and so does a stale docstring."""
        from pathlib import Path

        from shamsu.agent.triage import describe_capabilities
        from shamsu.tools import authoring_tools

        contracts = [tool.contract for tool in authoring_tools(Path("."))]
        rendered = describe_capabilities(contracts)

        for contract in contracts:
            assert contract.name in rendered

    def test_it_separates_reading_from_changing(self) -> None:
        from pathlib import Path

        from shamsu.agent.triage import describe_capabilities
        from shamsu.tools import authoring_tools

        rendered = describe_capabilities([t.contract for t in authoring_tools(Path("."))])
        assert rendered.index("file.read") < rendered.index("To change it:")
        assert rendered.index("To change it:") < rendered.index("file.patch")

    def test_it_needs_no_model(self) -> None:
        """The answer is a fact about the registry, so nothing can be wrong about it."""
        from pathlib import Path

        from shamsu.agent.triage import describe_capabilities
        from shamsu.tools import authoring_tools

        assert describe_capabilities([t.contract for t in authoring_tools(Path("."))])


class TestGreetingStripping:
    def test_exactly_one_greeting_is_stripped(self) -> None:
        # "hi" strips to "hello", which is itself conversational.
        assert triage("hi hello") is Intent.CHAT

    def test_a_greeting_tail_is_still_a_greeting(self) -> None:
        assert triage("hello everyone") is Intent.CHAT

    def test_a_tail_alone_is_not_small_talk(self) -> None:
        assert triage("there") is not Intent.CHAT


class _Model:
    """The narrowest client `respond` needs. Records what it was asked."""

    name = "stub"
    context_tokens = 4096

    def __init__(self, text: str = "Hello! What would you like changed?") -> None:
        self.text = text
        self.prompts: list[str] = []

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    async def generate(self, request: ModelRequest, cancel: object) -> ModelResponse:
        self.prompts.append(request.messages[0].content)
        return ModelResponse(text=self.text)

    async def generate_typed(self, request: object, contract: object, cancel: object) -> object:
        raise AssertionError("small talk must not use a structured contract")


class _Unavailable(_Model):
    async def generate(self, request: ModelRequest, cancel: object) -> ModelResponse:
        raise ModelUnavailable("ollama is not running")


class _Slow(_Model):
    async def generate(self, request: ModelRequest, cancel: object) -> ModelResponse:
        raise ModelTimeout("took too long")


def _reply(model: _Model, message: str = "hi") -> str:
    return asyncio.run(respond(model, message, NullCancellationToken()))


class TestReplying:
    def test_a_greeting_gets_the_models_reply(self) -> None:
        assert _reply(_Model()) == "Hello! What would you like changed?"

    def test_the_prompt_is_only_the_rules_and_the_message(self) -> None:
        """v1 passed the agent context in and the model narrated *about* it.

        The fix is not a sterner instruction, it is having nothing to narrate —
        so what this pins is that the prompt carries no compiled frame: no
        project facts, no file listing, no plan, no prior turns. Asserting on a
        size ceiling rather than on absent keywords keeps the test honest when
        the wording of the rules changes.
        """
        model = _Model()
        _reply(model, "hey there")

        prompt = model.prompts[0]
        assert "hey there" in prompt
        assert len(prompt) < 600, "small talk must not carry a compiled context frame"
        for marker in ("Task:", "Plan:", "Step ", "Files:", "Recent", "Observation"):
            assert marker not in prompt

    def test_a_leaked_reasoning_span_is_stripped(self) -> None:
        assert _reply(_Model("<think>the user said hi, be brief</think>Hello!")) == "Hello!"

    def test_a_role_label_is_stripped(self) -> None:
        assert _reply(_Model("Reply: Hey — what next?")) == "Hey — what next?"

    def test_an_unreachable_model_still_answers(self) -> None:
        """A greeting is not worth ending a session over."""
        assert _reply(_Unavailable()) == FALLBACK_REPLY

    def test_a_timeout_still_answers(self) -> None:
        assert _reply(_Slow()) == FALLBACK_REPLY

    def test_an_empty_reply_falls_back(self) -> None:
        assert _reply(_Model("   ")) == FALLBACK_REPLY

    @pytest.mark.parametrize(
        "raw",
        ['{"action": "conclude"}', "{}", "[]", '[{"tool": "file.read"}]'],
    )
    def test_structured_data_is_not_a_reply(self, raw: str) -> None:
        """A greeting answered with JSON is not an answer."""
        assert _reply(_Model(raw)) == FALLBACK_REPLY

    def test_prose_containing_braces_still_replies(self) -> None:
        """The guard must not eat a legitimate sentence that mentions syntax."""
        model = _Model("Use {} for an empty dict — what would you like changed?")
        assert _reply(model) != FALLBACK_REPLY
