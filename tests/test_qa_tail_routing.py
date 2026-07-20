"""Gap B1: the tool-less QA specialist was the catch-all for missed intent.

Work phrased without an action verb - "the login page needs a dark mode",
"hook the form up to the api", "can you get rid of the sidebar" - missed the
verb list and got a confident *description* from the tool-less brain instead
of the change. Adding verbs is whack-a-mole; the default flips instead: QA
must be EARNED by question shape, and everything else goes to the agent loop,
which has tools and can ask upfront (J6). Misrouting a question to the loop
costs latency; misrouting work to QA produces a useless answer - the loop is
the safe side.
"""
from __future__ import annotations

import pytest

from shamsu.cli.repl import (
    _enforce_read_only_decision,
    _prefers_qa_answer,
    _qa_branch_routes_to_agent,
)
from shamsu.types import RoutingDecision


# --- work that used to silently land in QA (the bug) --------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "the login page needs a dark mode",
        "the header color should be blue instead",
        "hook the form up to the api",
        "can you get rid of the sidebar",
        "i want the score shown at the top",
        "the tests are failing on windows",
        "the readme is out of date",
    ],
)
def test_statement_shaped_work_goes_to_the_agent(prompt: str):
    assert _prefers_qa_answer(prompt) is False
    assert _qa_branch_routes_to_agent(prompt, uses_real_index=True) is True


# --- genuine questions stay on QA ----------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "what does game.js do",
        "why is the build slow?",
        "how does the auth flow work",
        "is there a test suite",
        "does the api validate input?",
        "explain the build script",
        "can you explain the deploy process",
        "tell me about the memory system",
        "which file handles routing",
    ],
)
def test_questions_stay_on_qa(prompt: str):
    assert _prefers_qa_answer(prompt) is True


@pytest.mark.parametrize("prompt", ["hi", "hello", "thanks"])
def test_casual_chat_is_not_treated_as_work(prompt: str):
    assert _prefers_qa_answer(prompt) is True


# --- ordering subtleties --------------------------------------------------------


def test_polite_explain_is_a_question_but_polite_work_is_not():
    """Both start with "can you"; the difference is what follows. The
    question-prefix check runs first, so "can you explain" keeps QA while
    "can you remove" is a request."""
    assert _prefers_qa_answer("can you explain the build script") is True
    assert _prefers_qa_answer("can you remove the old tests") is False


def test_imperatives_still_win_via_the_action_detector():
    """'do the task' opens with a question-ish word ('do ') but the action
    detector runs FIRST in the branch, so it still reaches the agent."""
    assert _qa_branch_routes_to_agent("do the task", uses_real_index=True) is True


def test_a_bare_question_mark_earns_qa():
    assert _prefers_qa_answer("the build passes locally but not in ci?") is True


@pytest.mark.parametrize("prompt", ["charge card", "auth flow", "the game loop"])
def test_short_verbless_fragments_are_lookups_not_work(prompt: str):
    """"charge card" is the user pointing at something they want explained -
    a search-style lookup for indexed QA, not a change request."""
    assert _prefers_qa_answer(prompt) is True
    assert _qa_branch_routes_to_agent(prompt, uses_real_index=True) is False


def test_short_fragments_with_an_action_verb_are_still_work():
    assert _prefers_qa_answer("fix the bug") is False


# ---------------------------------------------------------------------------
# Gap I2 (closed by B1 + A2 together): follow-up phrasings used to need their
# own keyword expansions ("check on the web", "open it in the browser") or
# they routed as brand-new context-free prompts. Now any work-shaped follow-up
# reaches the agent loop, and the loop hydrates the transcript - so "do that
# again but smaller" both routes correctly AND knows what "that" was.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "do that again but smaller",
        "same for the other file",
        "now the same thing for the login page",
    ],
)
def test_followup_work_reaches_the_agent_loop(prompt: str):
    assert _qa_branch_routes_to_agent(prompt, uses_real_index=True) is True


def test_followup_questions_stay_on_qa():
    assert _prefers_qa_answer("why did that fail?") is True


def test_explicit_read_only_question_cannot_reach_the_agent_loop():
    prompt = "Where is add defined? Do not change any files."

    assert _qa_branch_routes_to_agent(prompt, uses_real_index=True) is False


def test_read_only_constraint_overrides_a_mutating_model_decision():
    decision = RoutingDecision(intent="code_edit", complexity="single", confidence=0.8)

    result = _enforce_read_only_decision(
        "Explain add without changing any files.",
        decision,
    )

    assert result.intent == "qa"
    assert result.confidence == 1.0


def test_scoped_constraint_does_not_block_requested_work():
    decision = RoutingDecision(intent="bug_fix", complexity="single", confidence=0.8)

    result = _enforce_read_only_decision("Fix app.py but do not change tests.", decision)

    assert result.intent == "bug_fix"
