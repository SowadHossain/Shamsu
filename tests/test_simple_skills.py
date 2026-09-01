"""Choosing a skill from what the turn is DOING, not just what it was asked.

The measurement behind this file, from the live asteroid session of
2026-08-31 (`F:\\Work\\voice-demo`, run_2026-08-31_00-07-32_fa88, status
failed after 24 steps):

  * the request was "Let's build an asteroid game with multiple levels and
    sound effects", which scores `developer` at 2.0 against a floor of 3.0 and
    every other skill at 0.0 - so nothing was injected, correctly, by the
    request-only matcher
  * the skill INDEX was in all 24 prompts, ~147 tokens each
  * `use_skill` was called zero times, and no skill was ever auto-injected
  * meanwhile the turn made EIGHT consecutive `append_file` calls against
    `asteroid/game.js`, taking it 76 -> 581 lines, and called
    `contract_assert_pass` three times getting the same "needs evidence"
    refusal each time

`large-file-surgery` and `testing` were both in the roster the whole time.
The sequence below is that run's, so the fixture is evidence rather than an
invention - but it is written out here rather than read from that workspace,
which is a demo and not a test fixture.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.simple_skills import (
    FAILURES_BEFORE_HELP,
    WRITES_BEFORE_SURGERY,
    Situation,
    best_skill,
    render_skill,
    situation_skill_name,
    skill_for_turn,
)

#: The asteroid run's writes, in order, as `_observed_writes` recorded them.
ASTEROID_WRITES = (
    "asteroid/PLAN.md",
    "asteroid/index.html",
    "asteroid/game.js",
    "asteroid/game.js",
    "asteroid/game.js",
    "asteroid/game.js",
    "asteroid/game.js",
    "asteroid/game.js",
    "asteroid/game.js",
    "asteroid/game.js",
    "asteroid/game.js",
)

#: Its failures, as `_turn_failures` recorded them. Verbatim.
CONTRACT_REFUSAL = (
    "contract_assert_pass",
    "a01 needs evidence: what did you run, and what did it say?",
)

ASTEROID_REQUEST = "Let's build an asteroid game with multiple levels and sound effects."


class FakeSkill:
    def __init__(self, name: str, instructions: str = "do the thing", **meta: object) -> None:
        self.name = name
        self.instructions = instructions
        self.description = f"{name} description"
        self.triggers: tuple[str, ...] = tuple(meta.get("triggers", ()))  # type: ignore[arg-type]
        self.tags: tuple[str, ...] = tuple(meta.get("tags", ()))  # type: ignore[arg-type]
        self.context_budget_tokens = int(meta.get("context_budget_tokens", 0))  # type: ignore[call-overload]


# -- the situation ---------------------------------------------------------


def test_a_file_written_over_and_over_asks_for_surgery() -> None:
    assert situation_skill_name(Situation(writes=ASTEROID_WRITES)) == "large-file-surgery"


def test_one_write_each_to_several_files_is_not_surgery() -> None:
    """Building a project is not the same as building one FILE in pieces."""
    situation = Situation(writes=("a.py", "b.py", "c.py", "d.py", "e.py"))

    assert situation_skill_name(situation) == ""


def test_the_threshold_is_where_it_says_it_is() -> None:
    one_short = Situation(writes=("game.js",) * (WRITES_BEFORE_SURGERY - 1))
    exactly = Situation(writes=("game.js",) * WRITES_BEFORE_SURGERY)

    assert situation_skill_name(one_short) == ""
    assert situation_skill_name(exactly) == "large-file-surgery"


def test_an_assertion_refused_for_evidence_asks_for_qa() -> None:
    """A failing assertion is not a bug. It is a claim the model has not
    backed up, and qa-tester is the skill that says what backing it up means."""
    situation = Situation(failures=(CONTRACT_REFUSAL,) * FAILURES_BEFORE_HELP)

    assert situation_skill_name(situation) == "qa-tester"


def test_any_other_tool_failing_the_same_way_asks_for_the_debugger() -> None:
    """Twice is a pattern, and trying again harder is not a plan."""
    stuck = ("patch_file", "old_string not found in app/routes.py")
    situation = Situation(failures=(stuck,) * FAILURES_BEFORE_HELP)

    assert situation_skill_name(situation) == "debugger"


def test_two_different_failures_are_not_a_pattern() -> None:
    """A turn that hits two unrelated errors is having a bad time, not doing
    one thing wrong repeatedly."""
    situation = Situation(
        failures=(
            ("run_tests", "2 failed"),
            ("patch_file", "old_string not found"),
            ("read_file", "no such file"),
        )
    )

    assert situation_skill_name(situation) == ""


def test_a_quiet_turn_asks_for_nothing() -> None:
    assert situation_skill_name(Situation()) == ""
    assert situation_skill_name(None) == ""


def test_the_file_being_built_outranks_the_thing_going_wrong() -> None:
    """Both fire at once in the asteroid run. Surgery is a statement about the
    whole rest of the turn; a repeated failure is about one assertion."""
    both = Situation(writes=ASTEROID_WRITES, failures=(CONTRACT_REFUSAL,) * 3)

    assert situation_skill_name(both) == "large-file-surgery"


# -- choosing, against the real bundled catalogue --------------------------


def test_the_asteroid_request_alone_matches_nothing(tmp_path: Path) -> None:
    """The premise of the whole file. `developer` scores 2.0 on "build" and the
    floor is 3.0, so the request-only matcher correctly injects nothing - and
    that is why the run got no skill at all."""
    assert render_skill(tmp_path, ASTEROID_REQUEST, 2000) == ""


def test_the_append_chain_gets_large_file_surgery(tmp_path: Path) -> None:
    """What should have happened at step 8 of that run."""
    name, text = skill_for_turn(
        tmp_path,
        ASTEROID_REQUEST,
        2000,
        situation=Situation(writes=ASTEROID_WRITES),
    )

    assert name == "large-file-surgery"
    assert text.startswith("How this project does large-file-surgery")
    # The actual instruction the run needed and never saw.
    assert "one symbol at a time" in text or "outline" in text.lower()


def test_the_repeated_refusal_gets_qa_tester(tmp_path: Path) -> None:
    """What should have happened at the SECOND "needs evidence" refusal, four
    steps and a 27-second command before the third one."""
    name, text = skill_for_turn(
        tmp_path,
        ASTEROID_REQUEST,
        2000,
        situation=Situation(failures=(CONTRACT_REFUSAL,) * 3),
    )

    assert name == "qa-tester"
    assert "evidence" in text.lower()


def test_a_skill_is_injected_once_and_not_every_round(tmp_path: Path) -> None:
    """It arrives, and then it stops arriving. Re-injecting every round would
    spend the budget re-saying what the model has already been told."""
    situation = Situation(writes=ASTEROID_WRITES)
    first, _ = skill_for_turn(tmp_path, ASTEROID_REQUEST, 2000, situation=situation)
    second, text = skill_for_turn(
        tmp_path, ASTEROID_REQUEST, 2000, situation=situation, already_used=(first,)
    )

    assert first == "large-file-surgery"
    assert (second, text) == ("", "")


def test_no_budget_means_no_skill(tmp_path: Path) -> None:
    assert skill_for_turn(
        tmp_path, ASTEROID_REQUEST, 0, situation=Situation(writes=ASTEROID_WRITES)
    ) == ("", "")


def test_the_situation_is_asked_before_the_request(tmp_path: Path) -> None:
    """A request that DOES match still loses to what the turn is actually
    doing - the request is a guess made before starting."""
    name, _text = skill_for_turn(
        tmp_path,
        "add tests and verify the acceptance criteria",
        2000,
        situation=Situation(writes=ASTEROID_WRITES),
    )

    assert name == "large-file-surgery"


def test_without_a_situation_the_request_still_decides(tmp_path: Path) -> None:
    """The old path, unbroken: no situation, so the request chooses."""
    name, text = skill_for_turn(tmp_path, "add tests and verify acceptance", 2000)

    assert name == "testing"
    assert text


# -- scoring ---------------------------------------------------------------


def test_a_phrase_outweighs_a_bare_verb() -> None:
    """Two words that matched say more than one, and the floor is set so a
    lone generic verb never wins."""
    general = FakeSkill("developer", triggers=("build", "fix"))
    specific = FakeSkill("surgery", triggers=("large file", "part by part"))

    assert best_skill([general, specific], "fix the large file") is specific
    assert best_skill([general], "build it") is None


def test_a_trigger_matches_whole_words_only() -> None:
    """`ui` inside "b**ui**ld" scored `ui-designer` at 3.0 on the live
    asteroid request and would have injected a page-layout skill into a game."""
    designer = FakeSkill("ui-designer", triggers=("ui", "design"))

    assert best_skill([designer], ASTEROID_REQUEST.lower()) is None


@pytest.mark.parametrize("request_text", ["", "   ", "\n"])
def test_an_empty_request_matches_nothing(tmp_path: Path, request_text: str) -> None:
    assert render_skill(tmp_path, request_text, 2000) == ""


# -- what a small model can actually hold -----------------------------------

#: 0.04 of an 8k window, which is what a 3B runs in. Any skill longer than this
#: is cut on the machines these skills exist to help - and the tail that gets
#: cut is where the "do not do X" rules live.
#:
#: Raised 327 -> 480 on 2026-08-31, for `ui-designer` and with the original
#: reasoning answered rather than waived. That skill has to carry two things a
#: narrower one does not: an icon policy (emoji are a different picture on every
#: platform and cannot be recoloured, so they must not be interface icons) and
#: every front-end framework, because the harness does not know which one the
#: project uses until it looks. At 327 one of those was always cut.
#:
#: The tail argument is handled by ORDER instead of by length: its rules are
#: written most-prohibitive-first, so at an 8k window the emoji rule, the icon
#: sets, the `<script src>` failure and the framework notes all still arrive,
#: and what truncation reaches is the spacing scale. Truncation is line-wise and
#: ends in "call use_skill for the rest", so nothing is cut mid-instruction.
#:
#: Ordering, not length, is what decides whether a truncated skill still works -
#: and this ceiling should stay tight enough that a skill has to be ordered.
#:
#: Set at 480 rather than crept up to: the number was shaved against four times
#: in one afternoon, each time costing content that was in the skill for a
#: reason. Every other bundled skill is under 330 and none is near this; if a
#: second one arrives at 470, that is the signal to split it, not to raise this
#: again.
SMALL_MODEL_BUDGET = 480


def test_every_bundled_skill_fits_a_small_model_window() -> None:
    """The measurement that started this: `large-file-surgery` was 722 tokens
    against its own 700 budget, so the one skill the append-chain situation
    injects was the one guaranteed to arrive truncated."""
    from shamsu.context.budget import count_tokens
    from shamsu.skills.loader import discover_skills

    oversized = {
        skill.name: count_tokens(skill.instructions)
        for skill in discover_skills(None).sorted_skills()
        if count_tokens(skill.instructions) > SMALL_MODEL_BUDGET
    }

    assert oversized == {}


def test_every_bundled_skill_can_actually_be_matched() -> None:
    """A skill with no triggers scores 0.0 on every request that does not name
    it outright, so it can only ever be reached by `use_skill` - which across
    every logged session was called zero times."""
    from shamsu.skills.loader import discover_skills

    unreachable = [
        skill.name
        for skill in discover_skills(None).sorted_skills()
        if not skill.triggers
    ]

    assert unreachable == []


def test_the_situation_skills_all_exist() -> None:
    """`situation_skill_name` returns names as strings. A typo, or a skill
    renamed, would silently mean nothing is ever injected for that situation."""
    from shamsu.agents.simple_skills import SITUATION_SKILLS
    from shamsu.skills.loader import discover_skills

    have = {skill.name for skill in discover_skills(None).sorted_skills()}
    wanted = {name for _situation, name in SITUATION_SKILLS}

    assert wanted <= have, f"situation points at skills that do not exist: {wanted - have}"
