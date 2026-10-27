"""The roster the model is offered, and what happens when it ignores it.

Live 2026-08-31 in `F:/Work/voice-demo`, with `chat_max_ctx` set to exactly
16384, two consecutive runs went out with `tool_count: 1` on all 47 model
calls, burned their whole round ceiling and failed. The runs on either side of
that setting carried 26 and 29 schemas and succeeded.

Two separate faults, both here:

* `routing_mode` compared `<=`, so the single most likely window in the field -
  the one `/context window` offers and `OFFERED_CTX_WINDOWS` snaps to - fell
  one point inside two-stage.
* Nothing reconciled a tool call against the roster it was offered, so the
  model called tools it had never been shown, they all ran, `_tool_category`
  stayed "" and stage 2 never happened.
"""
from __future__ import annotations

import pytest

from shamsu.agents.simple_chat import SimpleChatLoop, active_tool_schemas
from shamsu.agents.simple_router import (
    TWO_STAGE_CTX_THRESHOLD,
    category_for_tool,
    routing_mode,
)


def _names(schemas):
    return {(s.get("function") or {}).get("name") for s in schemas}


# -- the threshold ----------------------------------------------------------


def test_the_offered_window_is_not_inside_two_stage():
    """16384 is a value the UI hands out; it must get the real roster."""
    assert routing_mode(TWO_STAGE_CTX_THRESHOLD) == "direct"


def test_below_the_threshold_still_narrows():
    assert routing_mode(TWO_STAGE_CTX_THRESHOLD - 1) == "two_stage"
    assert routing_mode(8192) == "two_stage"


def test_every_offered_window_gets_more_than_the_selector():
    """The regression in one line: no window a user can pick may send 1 tool
    and then never send another."""
    from shamsu.context.budget import OFFERED_CTX_WINDOWS

    for window in OFFERED_CTX_WINDOWS:
        if routing_mode(window) == "direct":
            assert len(active_tool_schemas(window, "", None, "fix a bug")) > 1


# -- the reverse index ------------------------------------------------------


@pytest.mark.parametrize(
    "tool,category",
    [
        ("read_file", "read"),
        ("patch_file", "write"),
        ("search_files", "search"),
        ("run_command", "run"),
        ("check_page", "run"),
    ],
)
def test_a_tool_names_its_category(tool, category):
    assert category_for_tool(tool) == category


def test_an_unknown_tool_names_none():
    assert category_for_tool("nonsense") == ""
    assert category_for_tool("") == ""


# -- adoption ---------------------------------------------------------------


def _loop(ceiling: int) -> SimpleChatLoop:
    loop = SimpleChatLoop.__new__(SimpleChatLoop)
    loop.model_name = "qwen3:8b"
    loop._tool_category = ""
    loop._available_families = None
    loop._request = "fix the bullet direction in game.js"
    loop._ceiling = lambda: ceiling
    loop._read_withdrawn = set()
    loop.action_ledger = None
    loop._activity = lambda *a, **k: None
    from shamsu.agents.loop_guards import TrustDecay
    from shamsu.agents.simple_chat import SessionStalls

    loop._stalls = SessionStalls()
    loop._stalls.trust = TrustDecay()
    return loop


def test_a_call_for_an_unoffered_tool_chooses_its_category():
    """The fault, exactly: stage 1 offers one tool, the model calls another."""
    loop = _loop(8192)
    assert _names(loop._sent_schemas()) == {"select_category"}
    loop._adopt_category_from_call("patch_file")
    assert loop._tool_category == "write"
    assert "patch_file" in _names(loop._sent_schemas())


def test_the_adopted_roster_carries_real_schemas_not_just_names():
    """Why it matters. A model that has never seen a schema guesses argument
    names - nine `read_file{"name": ...}` calls in one live turn."""
    loop = _loop(8192)
    loop._adopt_category_from_call("read_file")
    read = next(
        s
        for s in loop._sent_schemas()
        if (s.get("function") or {}).get("name") == "read_file"
    )
    assert "filepath" in read["function"]["parameters"]["properties"]


def test_a_tool_it_was_offered_changes_nothing():
    loop = _loop(8192)
    loop._tool_category = "write"
    loop._adopt_category_from_call("patch_file")
    assert loop._tool_category == "write"


def test_the_selector_itself_is_never_adopted():
    loop = _loop(8192)
    loop._adopt_category_from_call("select_category")
    assert loop._tool_category == ""


def test_direct_mode_never_adopts():
    """Above the threshold the model has everything; nothing to reconcile."""
    loop = _loop(32768)
    loop._adopt_category_from_call("patch_file")
    assert loop._tool_category == ""


def test_reaching_outside_the_chosen_category_moves_it():
    """A model that picked `read` and now wants to write is not stranded."""
    loop = _loop(8192)
    loop._tool_category = "read"
    loop._adopt_category_from_call("patch_file")
    assert loop._tool_category == "write"


# -- what counts as "looking" -----------------------------------------------


def test_a_rejected_read_is_not_a_look():
    """Live 2026-08-31: nine `read_file{"name": ...}` rejections were counted
    as nine reads, ended the turn for reading too much, and reported
    "Everything I read is still in the conversation" of a turn that had read
    nothing. The guard counted tool NAMES and never asked whether the call
    worked."""
    from shamsu.agents.loop_guards import READS_BEFORE_INSISTING, ReadLoopDetector

    detector = ReadLoopDetector()
    for _ in range(READS_BEFORE_INSISTING + 4):
        # What the loop now passes: the failures are filtered out first, so a
        # round of nothing-but-rejections hands the detector an empty list.
        assert detector.record([], produced_something=False, targets=[]) is None
    assert detector.streak == 0


def test_successful_reads_still_trip_it():
    """The guard must keep working for the fault it was built for."""
    from shamsu.agents.loop_guards import READS_BEFORE_INSISTING, ReadLoopDetector

    detector = ReadLoopDetector()
    signal = None
    for index in range(READS_BEFORE_INSISTING + 2):
        signal = signal or detector.record(
            ["read_file"], produced_something=False, targets=[f"f{index}.py"]
        )
    assert signal is not None
