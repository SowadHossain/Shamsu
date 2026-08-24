"""The detectors simple mode did not have, tested without a loop.

That is the point of the module: eight guards already existed and not one of
them could be exercised without standing up a whole chat loop, a fake client and
a scripted turn. These are objects, so their edges are cheap to state.
"""
from __future__ import annotations

import pytest

from shamsu.agents import loop_guards
from shamsu.agents.loop_guards import (
    READS_BEFORE_INSISTING,
    READS_BEFORE_NUDGE,
    ReadLoopDetector,
    TrustDecay,
    closest_tool_names,
    greeting_regression,
)


# --- reading instead of answering ---------------------------------------


def test_five_reads_with_nothing_produced_earns_a_nudge():
    """"review X" has no terminal state, so a model can gather context forever
    and never be wrong to. Five is a model reading; it gets a gentle word."""
    detector = ReadLoopDetector()

    signals = [
        detector.record(["read_file"], produced_something=False)
        for _ in range(READS_BEFORE_NUDGE)
    ]

    assert signals[-1] is not None
    assert signals[-1].reason == "read_loop_warning"
    assert all(s is None for s in signals[:-1])


def test_eight_reads_is_insisted_on_rather_than_nudged_again():
    detector = ReadLoopDetector()
    fired = [
        detector.record(["read_file"], produced_something=False)
        for _ in range(READS_BEFORE_INSISTING)
    ]

    reasons = [s.reason for s in fired if s]
    assert reasons == ["read_loop_warning", "read_loop"]
    hard = [s for s in fired if s and s.reason == "read_loop"][0]
    assert "Stop reading" in hard.correction


def test_producing_anything_resets_the_streak():
    """An answer is production too, and so is running a command. The guard is
    about a turn with nothing to show, not about writes specifically."""
    detector = ReadLoopDetector()
    for _ in range(4):
        detector.record(["read_file"], produced_something=False)

    detector.record(["read_file"], produced_something=True)
    after = [
        detector.record(["read_file"], produced_something=False) for _ in range(3)
    ]

    assert all(s is None for s in after), "the streak restarted from zero"


def test_a_non_reading_call_also_resets_it():
    detector = ReadLoopDetector()
    for _ in range(4):
        detector.record(["read_file"], produced_something=False)

    detector.record(["run_command"], produced_something=False)

    assert detector.streak == 0


def test_each_level_fires_once_not_every_round_after():
    """A nudge repeated every round is not a nudge, it is a stuck loop of its
    own - and this project's rule is that every guard needs an exit."""
    detector = ReadLoopDetector()
    fired = [
        detector.record(["read_file"], produced_something=False) for _ in range(30)
    ]
    signals = [s for s in fired if s]

    # Each WORD is said once. What follows is not another sentence.
    reasons = [s.reason for s in signals]
    assert reasons.count("read_loop_warning") == 1
    assert reasons.count("read_loop") == 1
    assert reasons[:2] == ["read_loop_warning", "read_loop"]


def test_past_the_second_nudge_the_detector_does_not_go_silent():
    """It used to. Both flags were one-shot and nothing counted past them, so
    after eight reads there was no ceiling at all - which is how one turn read
    the same file fifteen times over 21m52s and still reported success.

    Six reads is not enough to reach it: the escalation is a ceiling, not a
    third nudge, and it must not fire on a model that is merely thorough."""
    from shamsu.agents.loop_guards import READ_LOOP_EXHAUSTED

    detector = ReadLoopDetector()
    early = [detector.record(["read_file"], produced_something=False) for _ in range(6)]
    assert not any(s and s.reason == READ_LOOP_EXHAUSTED for s in early)

    later = [detector.record(["read_file"], produced_something=False) for _ in range(20)]
    assert any(s and s.reason == READ_LOOP_EXHAUSTED for s in later)


def test_producing_something_clears_the_escalation_too():
    """A model that reads a lot and then WRITES has not lost the thread, and
    must not carry a ceiling into the rest of the turn."""
    from shamsu.agents.loop_guards import READ_LOOP_EXHAUSTED

    detector = ReadLoopDetector()
    for _ in range(12):
        detector.record(["read_file"], produced_something=False)
    detector.record(["write_file"], produced_something=True)

    after = [detector.record(["read_file"], produced_something=False) for _ in range(7)]
    assert not any(s and s.reason == READ_LOOP_EXHAUSTED for s in after)


# --- greeting mid-task ---------------------------------------------------


def test_a_greeting_after_tool_calls_is_lost_context():
    signal = greeting_regression("How can I help you today?", work_happened=True)

    assert signal is not None
    assert signal.reason == "greeting_regression"
    assert "carry on" in signal.correction


def test_a_greeting_on_the_first_turn_is_just_a_greeting():
    """The right answer to "hi" must not be corrected."""
    assert greeting_regression("Hi! How can I help?", work_happened=False) is None


def test_a_friendly_opening_to_a_real_answer_is_not_a_regression():
    """"Hi - I've added the handler" is a normal reply. A guard that fires on
    politeness is a guard that punishes good behaviour."""
    signal = greeting_regression(
        "Hi! I've added the refresh handler to auth.py and the tests pass.",
        work_happened=True,
    )

    assert signal is None


# --- a hallucinated tool name --------------------------------------------


@pytest.mark.parametrize(
    ("invented", "expected"),
    [
        ("read_files", "read_file"),
        ("write_files", "write_file"),
        ("search_file", "search_files"),
        ("run_tets", "run_tests"),
    ],
)
def test_a_near_miss_is_answered_with_the_name_it_meant(invented, expected):
    known = ["read_file", "write_file", "search_files", "run_tests", "patch_file"]

    assert expected in closest_tool_names(invented, known)


def test_a_claude_shaped_name_still_finds_something():
    """A model reaching for `functions.read_file` or `Edit` lands nowhere near a
    SHAMSU name by edit distance, but usually shares a word with one."""
    known = ["read_file", "write_file", "patch_file"]

    assert closest_tool_names("functions.read_file", known) == ["read_file"]


def test_a_name_resembling_nothing_suggests_nothing():
    """Better to fall back to the full list than to invent a confident wrong
    answer."""
    assert closest_tool_names("zzzzzzz", ["read_file", "write_file"]) == []


# --- trust decay ---------------------------------------------------------


def test_a_tool_that_keeps_failing_is_eventually_withheld():
    trust = TrustDecay()
    for _ in range(5):
        trust.record("search_files", ok=False)

    assert trust.level("search_files") == "drop"
    assert "search_files" in trust.dropped()


def test_a_writing_tool_is_never_withheld_however_often_it_fails():
    """THE difference from smallcode. Dropping `patch_file` leaves a model that
    cannot edit anything, which is worse than the loop it prevents."""
    trust = TrustDecay(protected=frozenset({"patch_file"}))
    for _ in range(20):
        trust.record("patch_file", ok=False)

    assert trust.level("patch_file") == "ok"
    assert trust.dropped() == frozenset()


def test_one_success_restores_a_failing_tool():
    """The question is whether the tool is working HERE, now - not whether it
    has ever failed."""
    trust = TrustDecay()
    for _ in range(4):
        trust.record("graph_search", ok=False)
    assert trust.level("graph_search") == "warn"

    trust.record("graph_search", ok=True)

    assert trust.level("graph_search") == "ok"


# --- adaptive retry temperature -----------------------------------------


def test_the_first_retry_is_colder_and_the_second_warmer():
    """At one fixed temperature a retry produces the same strategy and the same
    mistake - which is how one payload went out nine times byte-for-byte. The
    first retry has an exact error to work from and wants determinism; only
    when THAT fails is exploration worth paying for."""
    from shamsu.agents.loop_guards import adapted_temperature

    base = 0.4
    assert adapted_temperature(base, 0) == base, "a first attempt is untouched"
    assert adapted_temperature(base, 1) < base, "retry 1 goes colder"
    assert adapted_temperature(base, 2) > base, "retry 2 explores"
    assert adapted_temperature(base, 3) == base, "then back to the anchor"


def test_the_temperature_never_leaves_the_legal_range():
    from shamsu.agents.loop_guards import adapted_temperature

    for base in (0.0, 0.05, 0.95, 1.0):
        for streak in range(6):
            assert 0.0 <= adapted_temperature(base, streak) <= 1.0


# --- a reply that is nothing but a tool call ----------------------------


def test_a_reply_that_is_only_a_tool_call_is_recognised():
    """`parse_model_turn` salvages a leaked call only on an EXACT name match,
    and that gate is right for prose. But when the whole reply is the object
    there is no prose for it to be an example in - live 2026-08-20 a turn
    answered `{"name": "run_file", ...}` as its finished answer."""
    from shamsu.agents.loop_guards import leaked_tool_call

    assert leaked_tool_call('{"name": "run_file", "arguments": {"filepath": "a.py"}}') == "run_file"
    assert leaked_tool_call('```json\n{"name": "read_files", "arguments": {}}\n```') == "read_files"


def test_a_tool_call_MENTIONED_in_prose_is_left_alone():
    """The reason the parser's gate exists. Executing an example would be worse
    than ignoring it."""
    from shamsu.agents.loop_guards import leaked_tool_call

    assert leaked_tool_call('I would call {"name": "read_file", "arguments": {}} to see it.') == ""
    assert leaked_tool_call("Done! I created hello.py.") == ""
    assert leaked_tool_call("") == ""


# -- reading a project is not reading in circles -----------------------------
#
# Live 2026-08-24, `demo-3/asteroid`. The defect spanned seven source files:
# `initGame()` was never called in main.js, two more modules had no default
# export against a `{ default: X }` import, and four ended with a `let scene;`
# that ES modules cannot share. This guard interrupted at five reads - 17 times
# across the session - with "You probably have enough ... do not keep reading."
#
# It did not have enough, and it obeyed: every fix it shipped that session was
# scoped to whichever file it had read by the time it was told to stop.
#
# Raising the ceiling would have been the wrong correction. Nine different files
# read to no purpose is exactly the open-ended "review X" this detector exists
# for, and no count separates that from the eight above. What was wrong was the
# INSTRUCTION.


def _read(detector, name):
    return detector.record(["read_file"], False, targets=[f"read_file({name})"])


def test_opening_new_files_is_not_told_it_already_has_enough():
    detector = loop_guards.ReadLoopDetector()

    signal = None
    for name in ["main.js", "player.js", "projectile.js", "asteroid.js", "collision.js"]:
        signal = _read(detector, name) or signal

    assert signal is not None, "the guard still fires - the cadence has not moved"
    assert "probably have enough" not in signal.correction
    assert "WHAT you are looking for" in signal.correction
    assert "make the change now" in signal.correction


def test_circling_one_file_is_still_told_to_stop_reading():
    """The fault this detector was built for keeps its original wording."""
    detector = loop_guards.ReadLoopDetector()

    signal = None
    for _ in range(5):
        signal = _read(detector, "PlayerShip.js") or signal

    assert signal is not None
    assert "probably have enough" in signal.correction
    assert "do not keep reading" in signal.correction
    assert "re-reading what you already have" in signal.correction


def test_the_firm_word_also_stops_claiming_it_has_enough():
    detector = loop_guards.ReadLoopDetector()

    signals = [_read(detector, f"f{n}.js") for n in range(8)]
    firm = [s for s in signals if s and s.reason == "read_loop"]

    assert firm, "eight reads must still escalate"
    assert "have enough to go on" not in firm[0].correction
    assert "what is still missing" in firm[0].correction


def test_the_ceiling_that_ends_a_turn_is_untouched():
    """Breadth buys better wording, never a way past the ceiling."""
    detector = loop_guards.ReadLoopDetector()

    reasons = [
        signal.reason
        for signal in (_read(detector, f"f{n}.js") for n in range(40))
        if signal
    ]

    assert loop_guards.READ_LOOP_EXHAUSTED in reasons


def test_producing_something_forgets_what_was_read():
    detector = loop_guards.ReadLoopDetector()
    for name in ["a.js", "b.js", "c.js"]:
        _read(detector, name)

    detector.record(["write_file"], True, targets=[])

    assert not detector.seen
    assert detector.streak == 0


def test_a_caller_that_sends_no_targets_behaves_exactly_as_before():
    """The parameter is optional, and it reads as re-reading when absent -
    which is the conservative half."""
    detector = loop_guards.ReadLoopDetector()

    signals = [detector.record(["read_file"], False) for _ in range(5)]

    assert signals[-1] is not None
    assert signals[-1].reason == "read_loop_warning"
    assert "probably have enough" in signals[-1].correction
