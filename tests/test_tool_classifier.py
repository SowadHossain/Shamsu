"""Deterministic tool-category scoring, and the roster it narrows.

The scorer is a GUESS. So the tests that matter most are not the ones proving
it guesses well - they are the ones proving a bad guess cannot strand the
model: an unsure request keeps the whole roster, a narrowed roster always
carries the way back, and an explicit choice by the model outranks the guess.
"""
from __future__ import annotations

import pytest

from shamsu.agents.simple_chat import active_tool_schemas
from shamsu.agents.tool_classifier import (
    categories_for,
    classify_request,
)


# --- what it recognises -------------------------------------------------


@pytest.mark.parametrize(
    ("request_text", "expected"),
    [
        ("fix the missing brace in game.js", "write"),
        ("add a shout() function to greet.js", "write"),
        ("rename old_name.py to new_name.py", "write"),
        ("where is the login handler defined?", "search"),
        ("who calls parse_config?", "search"),
        ("run the tests", "run"),
        ("build the project", "run"),
        ("what did we decide about auth last time?", "recall"),
        ("show me src/app.py", "read"),
    ],
)
def test_it_recognises_the_shape_of_a_request(request_text, expected):
    assert classify_request(request_text).category == expected


def test_an_anti_signal_stops_a_verb_hijacking_the_category(request_text="fix the search bug"):
    """"search" appears in the words and the task is an edit. Negative weights
    are what stop the noun deciding the category."""
    assert classify_request(request_text).category == "write"


# --- when it must NOT guess ---------------------------------------------


def test_a_greeting_keeps_the_whole_roster(tmp_path):
    """"hi" is not evidence that few tools are needed - the next thing said may
    be the task. No confidence means no narrowing."""
    assert classify_request("hi").category == ""
    assert categories_for("hi") == ()


def test_a_short_message_with_a_verb_is_still_a_task():
    """"run it" is eight characters and a real instruction."""
    assert classify_request("run it").category == "run"


def test_a_long_multi_part_request_is_not_narrowed_to_one_category():
    """Narrowing a request that does four things is how the fourth finds its
    tool missing."""
    long_request = (
        "Please read through the authentication module and work out how the "
        "session cookie is signed, then update the middleware so that it "
        "rejects an expired token cleanly, add a test that covers the expiry "
        "path, run the whole suite to make sure nothing else broke, and "
        "finally write a note about what you changed so we can review it all "
        "together tomorrow morning."
    )
    assert len(long_request) > 300
    assert categories_for(long_request) == ()


def test_a_request_matching_nothing_keeps_the_whole_roster():
    assert categories_for("qwerty zxcvbn") == ()


def test_a_near_tie_is_not_a_winner():
    """Two categories level with each other is not a classification, and acting
    on it would be a coin flip that costs the model its tools.

    "review the tests" is the real case: `review` is worth 3.0 to read and
    `tests` is worth 3.0 to run, so nothing wins and everything is sent.
    """
    verdict = classify_request("review the tests")

    assert verdict.scores["read"] == verdict.scores["run"], verdict.scores
    assert not verdict.certain_enough
    assert categories_for("review the tests") == ()


def test_a_dominant_score_is_acted_on():
    """The other edge. "find and fix" reads as a search until the anti-signals
    are applied; it is an edit, and the scorer must be confident enough to say
    so or the whole exercise saves nothing."""
    verdict = classify_request("find and fix the broken import")

    assert verdict.category == "write"
    assert verdict.certain_enough, verdict.scores


# --- the roster it produces ---------------------------------------------


def _names(context_window=32768, request="", category=""):
    schemas = active_tool_schemas(
        context_window=context_window,
        category=category,
        available=frozenset({"recall", "graph", "history"}),
        request=request,
    )
    return {s["function"]["name"] for s in schemas}


def test_an_edit_request_still_carries_the_read_tools(tmp_path):
    """Every edit starts with a read. A `write` classification that dropped
    `read_file` would break the first move of the task it just identified."""
    names = _names(request="fix the missing brace in game.js")

    assert {"patch_file", "replace_symbol", "write_file"} <= names
    assert "read_file" in names


def test_a_narrowed_roster_is_smaller_than_the_whole_catalogue(tmp_path):
    """The point of the exercise. Measured at 26 schemas / 3,196 tokens on a
    32k window before this existed."""
    everything = _names()
    narrowed = _names(request="run the tests")

    assert len(narrowed) < len(everything)
    assert "write_file" not in narrowed, "a test run does not need the write tools"


def test_every_narrowed_roster_carries_the_way_back(tmp_path):
    """The escape hatch, and the reason a guess is safe to act on. smallcode's
    direct mode has no equivalent: a wrong guess there strands the model.

    It is APPENDED rather than filtered in - `select_category` is generated,
    not a member of the catalogue, so keeping its name in the allow-set
    silently dropped it.
    """
    for request in ("fix the brace in game.js", "run the tests", "where is login?"):
        assert "select_category" in _names(request=request), request


def test_the_skill_index_stays_callable_whatever_was_guessed(tmp_path):
    """The index is injected into the prompt every turn; a model that can read
    it and not act on it is being taunted."""
    assert "use_skill" in _names(request="run the tests")


def test_the_models_own_choice_outranks_the_guess(tmp_path):
    """Without this the escape hatch is decorative: the model asks for the
    write tools, the scorer re-applies, and it gets read tools back."""
    names = _names(request="show me src/app.py", category="write")

    assert {"write_file", "patch_file"} <= names


def test_no_request_means_the_old_behaviour_exactly(tmp_path):
    """Every existing caller passes no request, and must be unaffected."""
    assert "write_file" in _names(request="")
    assert "run_command" in _names(request="")
    assert "select_category" not in _names(request="")


def test_two_stage_routing_is_untouched_below_the_threshold(tmp_path):
    """The small-window path already had an answer that costs a round and
    works. This changes nothing about it."""
    assert _names(context_window=8192, request="fix the brace") == {"select_category"}


# --- the plan role: read-only on purpose ---------------------------------


def test_asking_for_a_plan_withholds_the_write_tools():
    """smallcode's `planner.md` is a strong planner for one reason visible in
    its frontmatter: `tools: [read_file, find_files, search, ...]` - no write
    tools, so it CANNOT skip to implementing. That needs no sub-agent here; it
    is a category with the write tools left out."""
    names = _names(request="plan how to add authentication")

    assert not {"write_file", "patch_file", "replace_symbol", "delete_file"} & names
    assert "contract_create" in names, "it must still be able to write the plan DOWN"
    assert "read_file" in names, "and to research it"


def test_asking_for_advice_is_planning_not_writing():
    """"how should we approach the refactor?" - `refactor` alone used to
    outweigh the question and hand the model the write tools for a turn that
    was meant to produce words."""
    assert classify_request("how should we approach the refactor?").category == "plan"


def test_being_told_to_get_on_with_it_is_not_planning():
    """The anti-signal's other edge. "now just fix the typo" is an instruction,
    however much the word "just" sounds like deliberation."""
    assert classify_request("now just fix the typo").category == "write"
    assert classify_request("refactor the session store").category == "write"


def test_every_offered_tool_is_reachable_from_some_category():
    """A tool in no category vanishes the moment two-stage routing engages -
    which is the path SMALL models take, the ones that need the roster narrowed
    most. `use_skill` was invisible there while the skill index was still being
    injected into the prompt every turn: a list of skills, and no tool to open
    one."""
    from shamsu.agents.simple_chat import SIMPLE_TOOL_SCHEMAS
    from shamsu.agents.simple_router import ALWAYS_TOOLS, TOOL_CATEGORIES

    reachable = set(ALWAYS_TOOLS)
    for body in TOOL_CATEGORIES.values():
        reachable |= set(body["tools"])
    offered = {s["function"]["name"] for s in SIMPLE_TOOL_SCHEMAS}

    assert not offered - reachable, f"unreachable under two-stage: {sorted(offered - reachable)}"


def test_a_skill_can_still_be_opened_after_a_category_is_chosen():
    from shamsu.agents.simple_chat import active_tool_schemas

    for category in ("read", "write", "run", "search", "verify", "recall", "plan"):
        names = {
            s["function"]["name"]
            for s in active_tool_schemas(context_window=8192, category=category)
        }
        assert "use_skill" in names, category


# --- the web family: built, and gated on a probe rather than on hope ------


def test_web_tools_are_withheld_when_nothing_is_listening(tmp_path):
    """They were built, tested and never offered, because offering a tool that
    may always fail contradicts "offer only the tools that have something to
    answer from". Reporting that as a permanent gap was the wrong call: the
    missing piece was a probe, and every other conditional family has one."""
    from shamsu.agents.simple_chat import available_tool_families

    assert "web" not in available_tool_families(tmp_path)


def test_web_tools_stay_withheld_without_an_explicit_opt_in(tmp_path, monkeypatch):
    """Reaching the network is a decision a local-first tool must be asked to
    make. The probe is not even attempted unless SHAMSU_WEB_ENABLED says so."""
    from shamsu.agents.simple_chat import _web_is_reachable

    monkeypatch.delenv("SHAMSU_WEB_ENABLED", raising=False)
    assert _web_is_reachable(tmp_path) is False


def test_an_unreachable_backend_is_a_normal_answer_not_a_crash(tmp_path, monkeypatch):
    from shamsu.agents.simple_chat import _web_is_reachable

    monkeypatch.setenv("SHAMSU_WEB_ENABLED", "1")
    monkeypatch.setenv("SHAMSU_SEARXNG_URL", "http://127.0.0.1:1")
    assert _web_is_reachable(tmp_path) is False


def test_asking_about_this_project_never_routes_to_the_web():
    """"search the codebase" must not become a web lookup."""
    for local in ("search this project for parse_config", "what does our code do here"):
        assert classify_request(local).category != "web", local
