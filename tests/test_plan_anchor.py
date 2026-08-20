"""A plan that is written down once and shown again every turn.

The contract has held ordered, checkable, persisted items for weeks. Two things
were missing and both are about VISIBILITY rather than data: nothing ever asked
the model to write a plan, and nothing ever showed it the plan again.
"""
from __future__ import annotations

import pytest

from shamsu.agents.plan_anchor import (
    MAX_ANCHOR_CHARS,
    anchor,
    ask_for_a_plan,
    should_plan,
)


# --- when a job has parts -----------------------------------------------


@pytest.mark.parametrize(
    "request_text",
    [
        "Read the auth module, then add a refresh handler, then update the middleware",
        "refactor the session store",
        "1. read config\n2. add the flag\n3. run tests",
        "Please implement the login page. It needs a form and validation. Then wire "
        "it to the API and make sure the tests still pass afterwards.",
    ],
)
def test_a_job_with_parts_is_worth_writing_down(request_text):
    assert should_plan(request_text)


@pytest.mark.parametrize(
    "request_text",
    [
        "fix the typo in game.js",
        "add a shout() function",
        "what does this function do?",
        "why is the test failing?",
        "explain the auth flow",
    ],
)
def test_one_thing_is_not_a_plan(request_text):
    """A false positive costs a round AND anchors the model to a plan it wrote
    badly. A false negative costs nothing - it can still call contract_create
    itself. So the bar is evidence of sequence, not of difficulty."""
    assert not should_plan(request_text)


def test_a_question_is_never_planned_however_long_it_is():
    """Planning a question would be planning to plan."""
    long_question = (
        "What does the session manager actually do when two windows attach to "
        "the same thread at once, and why does the second one win, and how does "
        "the heartbeat interact with all of that over a long turn?"
    )
    assert len(long_question) > 150
    assert not should_plan(long_question)


def test_the_ask_names_the_call_rather_than_stating_a_rule():
    """"Plan before you start" has been in this project's prompts before and did
    not survive contact with a 3B."""
    asked = ask_for_a_plan("refactor the session store, then update the tests")

    assert "contract_create" in asked
    assert "contract_assert_pass" in asked


def test_nothing_is_asked_of_a_single_step_request():
    assert ask_for_a_plan("fix the typo") == ""


def test_the_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("SHAMSU_PLAN", "0")
    assert not should_plan("refactor everything, then run the tests, then tidy up")


# --- what the model is shown ---------------------------------------------


def test_the_anchor_says_the_model_wrote_it():
    """Ownership matters: a list the model believes it authored is one it keeps
    working through, where an instruction from the harness is one more rule."""
    shown = anchor("Contract: ship login\n  a01  [.....]  form renders")

    assert "you wrote this" in shown.lower()
    assert "form renders" in shown


def test_an_empty_contract_shows_nothing():
    assert anchor("") == ""
    assert anchor("   ") == ""


def test_a_long_plan_is_capped_and_says_where_the_rest_is():
    """The anchor is a tax on every turn of a long task. Past a dozen steps the
    model is being handed a document rather than an anchor."""
    huge = "Contract: big\n" + "\n".join(f"  a{i:02d}  [.....]  step {i}" for i in range(200))

    shown = anchor(huge)

    assert len(shown) < MAX_ANCHOR_CHARS + 200
    assert "contract_status" in shown


@pytest.mark.parametrize(
    "request_text",
    [
        "remember: the port is 8080",
        "the port is already in use",
        "change the port to 3000",
        "check which port the server binds",
    ],
)
def test_a_tcp_port_is_not_a_porting_job(request_text):
    """Caught by the suite, not by review: `port` was in the hint list for
    "port this to X", and matched a twenty-six character note about 8080. In
    this domain the noun is everywhere and the verb is rare."""
    assert not should_plan(request_text)
