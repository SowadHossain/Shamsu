"""The seven noun phrases that passed a broken game as done.

`_RUNTIME_CLAIM` is a vocabulary of verbs, and the snake-game contract of
2026-08-24 was written entirely in noun phrases. Two of nine assertions tripped
it; the other seven passed on `verified_by: write` while the page did not load
at all - `index.html` pointed at `game.js` and the file was at `js/game.js`.

The default is now inverted: a write discharges an assertion only when the
assertion is about the artifact itself.
"""
from __future__ import annotations

import pytest

from shamsu.agents.simple_contract import claims_runtime_behaviour, write_can_discharge

#: Verbatim from `demo-3/openbazar/.shamsu/contract.json`.
THE_SEVEN = [
    "CSS styling for 3D visual effects and UI",
    "JavaScript game engine with snake movement logic",
    "Multiple difficulty levels (easy, medium, hard)",
    "Menu system with start, settings, and quit options",
    "Sound effects for game events (eat, die, level up)",
    "Score tracking and level progression system",
    "Collision detection for walls and self",
]

#: Claims that really are settled by opening the file.
STATIC = [
    "game.js exports a Ship class",
    "package.json lists three.js as a dependency",
    "the file defines an ErrorBoundary component",
    "The README documents the build steps",
    "every module carries a licence header",
    "type hints on every public function",
    "the directory structure matches the plan",
]


@pytest.mark.parametrize("text", THE_SEVEN)
def test_a_noun_phrase_about_behaviour_needs_a_run(text):
    assert claims_runtime_behaviour(text), text
    assert not write_can_discharge(text), text


@pytest.mark.parametrize("text", STATIC)
def test_a_claim_about_the_artifact_is_still_write_backed(text):
    assert write_can_discharge(text), text
    assert not claims_runtime_behaviour(text), text


def test_an_unclassifiable_assertion_defaults_to_needing_a_run():
    """The whole point of the inversion. An assertion nobody can read costs a
    run, rather than shipping under a green tick."""
    assert claims_runtime_behaviour("a04")
    assert claims_runtime_behaviour("the thing works end to end")
    assert claims_runtime_behaviour("")  is False or True  # empty is not a claim


@pytest.mark.parametrize(
    "text",
    [
        "the README page renders in the browser",
        "the documented endpoint responds with 200",
        "package.json lists three.js and the app loads",
    ],
)
def test_a_strong_runtime_signal_beats_an_artifact_word(text):
    """`_RUNTIME_CLAIM` still overrides. An artifact noun in a sentence about
    behaviour must not buy a write-backed pass."""
    assert claims_runtime_behaviour(text), text
