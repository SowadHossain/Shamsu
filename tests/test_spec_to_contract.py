"""The Definition of Done, taken from the document instead of from memory.

A contract only means anything if it says what the USER asked for. Until now it
said whatever the model remembered of what the user asked for, and a small model
remembers badly: live 2026-08-31 it was handed eight requirements and wrote a
contract with ONE assertion, whose text was the eight of them printed as a Python
list into a single field. Nothing later could have noticed - a contract with one
pending assertion looks exactly like a contract with one pending assertion.

So the extraction is deterministic. Reading a document and listing what it asks
for is the job a 3B does worst and a regex does well enough: the document already
says "must", already numbers its features, already puts them under a heading
called Requirements.

And the outcome now reads it. `evidence_outcome` decided from mutations and
syntax alone, so a run that wrote files and parsed them was `success` however
much of its own contract was untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shamsu.agents.simple_spec import (
    MAX_REQUIREMENTS,
    extract_requirements,
    read_spec,
)

SPEC = """
# Marketplace

Some background prose that is not a requirement at all.

## Functional Requirements

- Sellers can list an item with at least three photographs
- Buyers can place a bid above the current highest bid
- The listing page must load in under two seconds

## Notes

TBD

## Implementation

The system should send an email when an auction closes.

Frontend
Phase 2
"""


# -- what counts as a requirement --------------------------------------------


def test_bullets_under_a_requirements_heading_are_found():
    found = extract_requirements(SPEC)
    texts = [item.text for item in found]
    assert "Sellers can list an item with at least three photographs" in texts
    assert any("place a bid" in t for t in texts)


def test_the_heading_is_recorded_as_the_reason():
    found = {item.text: item.source for item in extract_requirements(SPEC)}
    assert found["Sellers can list an item with at least three photographs"] == (
        "listed under a requirements heading"
    )


def test_an_obligation_in_prose_is_found_without_a_bullet():
    found = {item.text: item.source for item in extract_requirements(SPEC)}
    obligation = next(t for t in found if "email when an auction closes" in t)
    assert found[obligation] == "states an obligation"


def test_prose_and_structure_are_not_requirements():
    texts = [item.text for item in extract_requirements(SPEC)]
    assert not any("background prose" in t for t in texts)
    # A label has no verb and cannot be true or false.
    assert "Frontend" not in texts
    assert "Phase 2" not in texts
    assert "TBD" not in texts


def test_a_line_is_taken_once():
    doubled = SPEC + "\n- Buyers can place a bid above the current highest bid\n"
    texts = [item.text for item in extract_requirements(doubled)]
    assert len(texts) == len(set(texts))


def test_the_count_is_bounded():
    """A spec with two hundred bullets is a document to read, not a contract to
    satisfy in one turn."""
    huge = "## Requirements\n" + "\n".join(
        f"- The system must do useful thing number {n}" for n in range(200)
    )
    assert len(extract_requirements(huge)) == MAX_REQUIREMENTS


def test_document_order_is_kept():
    lines = [item.line for item in extract_requirements(SPEC)]
    assert lines == sorted(lines)


def test_prose_with_no_requirements_yields_none():
    assert extract_requirements("Just some writing about a project. It is nice.") == []


# -- reading the file ---------------------------------------------------------


def test_a_markdown_spec_is_read(tmp_path):
    (tmp_path / "SPEC.md").write_text(SPEC, encoding="utf-8")
    text, error = read_spec(tmp_path, "SPEC.md")
    assert not error
    assert "Functional Requirements" in text


def test_a_missing_file_is_a_message_not_an_exception(tmp_path):
    text, error = read_spec(tmp_path, "nope.md")
    assert not text
    assert "not a file" in error


def test_a_path_outside_the_workspace_is_refused(tmp_path):
    _text, error = read_spec(tmp_path, "../../secrets.md")
    assert "outside this workspace" in error


# -- the tool ----------------------------------------------------------------


def _loop(workspace: Path):
    from shamsu.agents.simple_chat import SimpleChatLoop

    loop = SimpleChatLoop.__new__(SimpleChatLoop)
    loop.workspace = workspace
    loop._activity = lambda *_a, **_k: None
    return loop


def test_the_tool_makes_the_documents_requirements_the_contract(tmp_path):
    from shamsu.agents.simple_contract import load_contract

    (tmp_path / "SPEC.md").write_text(SPEC, encoding="utf-8")
    result = _loop(tmp_path)._contract_from_spec({"filepath": "SPEC.md"})
    assert result.ok
    contract = load_contract(tmp_path)
    assert contract is not None
    assert len(contract.assertions) == result.data["count"] >= 4
    assert all(item.state == "pending" for item in contract.assertions)
    # Each assertion is one requirement, not all of them in one field - which is
    # the exact defect this replaces.
    assert all(len(item.text) < 250 for item in contract.assertions)


def test_a_document_with_no_requirements_says_so(tmp_path):
    (tmp_path / "prose.md").write_text("A nice essay about nothing.", encoding="utf-8")
    result = _loop(tmp_path)._contract_from_spec({"filepath": "prose.md"})
    assert not result.ok
    assert "No requirements found" in result.message
    # And it says what to do instead, like every other message here.
    assert "contract_create" in result.message


def test_an_open_contract_is_not_silently_replaced(tmp_path):
    from shamsu.agents.simple_contract import new_contract, save_contract

    (tmp_path / "SPEC.md").write_text(SPEC, encoding="utf-8")
    save_contract(tmp_path, new_contract("Existing", "b", ["something"]))
    result = _loop(tmp_path)._contract_from_spec({"filepath": "SPEC.md"})
    assert not result.ok
    assert "already an open contract" in result.message


def test_the_tool_is_offered_to_the_model():
    from shamsu.agents.simple_chat import SIMPLE_TOOLS, SIMPLE_TOOL_SCHEMAS

    assert "contract_from_spec" in SIMPLE_TOOLS
    schema = next(
        s for s in SIMPLE_TOOL_SCHEMAS if s["function"]["name"] == "contract_from_spec"
    )
    assert "filepath" in schema["function"]["parameters"]["properties"]


# -- and the outcome finally reads it ----------------------------------------


def test_a_run_with_an_unresolved_contract_is_not_success(tmp_path):
    """It wrote files and they parsed, so it was `success` - while the model's
    own Definition of Done said nothing had been established."""
    from shamsu.action_ledger.ledger import ActionLedger

    ledger = ActionLedger(tmp_path)
    ledger.start("build the thing")
    ledger.log_event("patch_apply_succeeded", path="a.py")
    ledger.log_event("verification_passed", verifier_id="syntax:a.py", path="a.py")
    assert ledger.evidence_outcome() == "success"

    ledger.log_event("contract_unresolved", total=8, pending=8, unproven=0)
    assert ledger.evidence_outcome() == "success_unverified"


def test_an_unresolved_contract_is_a_demotion_not_a_failure(tmp_path):
    """Unchecked is not broken; it is unknown, and this ledger already has a
    word for that."""
    from shamsu.action_ledger.ledger import ActionLedger

    ledger = ActionLedger(tmp_path)
    ledger.start("build the thing")
    ledger.log_event("patch_apply_succeeded", path="a.py")
    ledger.log_event("contract_unresolved", total=3, pending=1, unproven=2)
    assert ledger.evidence_outcome() == "success_unverified"


def test_a_finished_contract_leaves_the_outcome_alone(tmp_path):
    from shamsu.action_ledger.ledger import ActionLedger

    ledger = ActionLedger(tmp_path)
    ledger.start("build the thing")
    ledger.log_event("patch_apply_succeeded", path="a.py")
    ledger.log_event("verification_passed", verifier_id="syntax:a.py", path="a.py")
    assert ledger.evidence_outcome() == "success"


def test_the_loop_records_an_unresolved_contract(tmp_path):
    from shamsu.action_ledger.ledger import ActionLedger
    from shamsu.agents.simple_contract import new_contract, save_contract

    save_contract(tmp_path, new_contract("T", "b", ["one", "two"]))
    loop = _loop(tmp_path)
    loop.action_ledger = ActionLedger(tmp_path)
    loop.action_ledger.start("build the thing")
    loop._record_contract_state()
    events = [
        json.loads(line)
        for line in loop.action_ledger.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    recorded = next(e for e in events if e.get("type") == "contract_unresolved")
    assert recorded["pending"] == 2


def test_no_contract_records_nothing(tmp_path):
    from shamsu.action_ledger.ledger import ActionLedger

    loop = _loop(tmp_path)
    loop.action_ledger = ActionLedger(tmp_path)
    loop.action_ledger.start("just a question")
    loop._record_contract_state()
    body = loop.action_ledger.events_path.read_text(encoding="utf-8")
    assert "contract_unresolved" not in body


# -- an ask that was ignored ---------------------------------------------------


def _noticing_loop(workspace: Path):
    loop = _loop(workspace)
    said: list[str] = []
    loop._notice = said.append
    loop.action_ledger = None
    return loop, said


def test_a_plan_asked_for_and_never_written_is_reported(tmp_path):
    """`ask_for_a_plan` is a nudge and stopped there. Live 2026-08-31 a build of
    a whole game ran two turns and finished with no contract at all, so nothing
    checked the work against what was asked - and every verdict was computed
    from files and syntax alone."""
    loop, said = _noticing_loop(tmp_path)
    loop._asked_for_a_plan = True
    loop._note_a_plan_that_was_never_written()
    assert said and "no contract was written" in said[0]


def test_a_plan_that_WAS_written_says_nothing(tmp_path):
    from shamsu.agents.simple_contract import new_contract, save_contract

    save_contract(tmp_path, new_contract("T", "b", ["one"]))
    loop, said = _noticing_loop(tmp_path)
    loop._asked_for_a_plan = True
    loop._note_a_plan_that_was_never_written()
    assert said == []


def test_a_turn_that_was_never_asked_says_nothing(tmp_path):
    """A one-line question is not a job with parts, and must not be nagged."""
    loop, said = _noticing_loop(tmp_path)
    loop._asked_for_a_plan = False
    loop._note_a_plan_that_was_never_written()
    assert said == []


def test_the_missing_contract_reaches_the_run_record(tmp_path):
    from shamsu.action_ledger.ledger import ActionLedger

    loop = _loop(tmp_path)
    loop._notice = lambda _m: None
    loop._asked_for_a_plan = True
    loop.action_ledger = ActionLedger(tmp_path)
    loop.action_ledger.start("build a game")
    loop._note_a_plan_that_was_never_written()
    body = loop.action_ledger.events_path.read_text(encoding="utf-8")
    assert "contract_never_written" in body


# -- a look before the rounds run out ------------------------------------------


def _turn(workspace: Path, *, rounds: int = 24, checks: int = 0):
    loop = _loop(workspace)
    loop.max_rounds = rounds
    loop._checks_run = checks
    loop._asked_for_a_look = False
    loop._corrections_this_turn = 0
    steered: list[str] = []
    loop._steer = lambda m: steered.append(m)
    return loop, steered


def test_a_turn_that_only_wrote_is_asked_to_look(tmp_path):
    """Live 2026-08-31: 24 rounds and both turns spent writing, nothing run, and
    a game in which clicking START moved the canvas 1.22% -> 1.23%."""
    loop, steered = _turn(tmp_path)
    loop._ask_for_a_look_before_the_ceiling(21, ["game.js"])
    assert steered and "nothing you have written has been run" in steered[0]
    # It names the calls, like every other message here.
    for tool in ("run_tests", "run_command", "check_page"):
        assert tool in steered[0]


def test_a_turn_that_already_checked_is_left_alone(tmp_path):
    loop, steered = _turn(tmp_path, checks=1)
    loop._ask_for_a_look_before_the_ceiling(21, ["game.js"])
    assert steered == []


def test_a_turn_that_wrote_nothing_has_nothing_to_check(tmp_path):
    loop, steered = _turn(tmp_path)
    loop._ask_for_a_look_before_the_ceiling(21, [])
    assert steered == []


def test_it_does_not_fire_early_in_a_turn(tmp_path):
    """Round 5 of 24 is not running out of budget; it is doing the work."""
    loop, steered = _turn(tmp_path)
    loop._ask_for_a_look_before_the_ceiling(5, ["game.js"])
    assert steered == []


def test_it_fires_once_not_every_remaining_round(tmp_path):
    loop, steered = _turn(tmp_path)
    for round_index in (21, 22, 23):
        loop._ask_for_a_look_before_the_ceiling(round_index, ["game.js"])
    assert len(steered) == 1


def test_the_checking_tools_are_the_ones_that_exercise_code():
    from shamsu.agents.simple_chat import _CHECKING_TOOLS

    assert {"check_page", "run_tests", "run_command"} <= _CHECKING_TOOLS
    # Writing is not checking - that distinction is the whole point.
    assert not _CHECKING_TOOLS & {"write_file", "append_file", "patch_file"}


# -- the designer skill -------------------------------------------------------


def _designer_body() -> str:
    from shamsu.skills.loader import discover_skills

    return {
        s.name: s for s in discover_skills(Path.cwd()).sorted_skills()
    }["ui-designer"].instructions


def test_the_designer_skill_is_a_method_not_a_list_of_opinions():
    body = _designer_body()
    # It has to name the check, or it is advice with no way to act on it.
    assert "check_page" in body
    # And the two failures that cost a real session.
    assert "0x0" in body
    assert "script src" in body.lower()


def test_the_designer_skill_bans_emoji_and_names_real_icon_sets():
    body = _designer_body()
    assert "emoji" in body.lower()
    # Naming the alternative is the whole point - a prohibition with no
    # replacement is the shape this prompt file refuses everywhere else.
    assert "Lucide" in body
    assert "aria-label" in body


@pytest.mark.parametrize(
    "framework", ["React", "Vue", "Svelte", "Angular", "Tailwind", "HTML"]
)
def test_the_designer_skill_covers_every_front_end(framework):
    """In the BODY, deliberately, and not as triggers: `react` as a trigger made
    "the ReAct tool loop" match React the framework, which
    tests/test_skills.py has a case against."""
    assert framework in _designer_body()


def test_a_framework_name_is_not_a_designer_trigger():
    from shamsu.skills.loader import discover_skills

    skill = {
        s.name: s for s in discover_skills(Path.cwd()).sorted_skills()
    }["ui-designer"]
    assert not {"react", "vue", "angular", "svelte"} & set(skill.triggers)


def test_the_designer_skill_fits_the_window_it_is_given():
    """It was cut off before its last section at a 4% share, which is how the
    line that turns it into evidence went missing."""
    from shamsu.agents.simple_skills import SKILL_BUDGET_RATIO, render_skill
    from shamsu.context.budget import count_tokens

    rendered = render_skill(
        Path.cwd(), "build a responsive dashboard layout", int(32768 * SKILL_BUDGET_RATIO)
    )
    assert rendered
    assert "call use_skill" not in rendered.rstrip().splitlines()[-1]
    assert count_tokens(rendered) <= int(32768 * SKILL_BUDGET_RATIO)


def test_a_small_window_degrades_rather_than_dropping_the_skill():
    from shamsu.agents.simple_skills import SKILL_BUDGET_RATIO, render_skill

    rendered = render_skill(
        Path.cwd(), "build a responsive dashboard layout", int(8192 * SKILL_BUDGET_RATIO)
    )
    assert rendered
    assert "call use_skill" in rendered  # says where the rest is


@pytest.mark.parametrize(
    "request_text",
    [
        "build a responsive dashboard layout",
        "fix the css on the mobile screen",
        "make the frontend look better",
        "improve the page design",
    ],
)
def test_a_front_end_request_reaches_the_designer(request_text):
    from shamsu.agents.simple_skills import best_skill
    from shamsu.skills.loader import discover_skills

    skills = list(discover_skills(Path.cwd()).sorted_skills())
    chosen = best_skill(skills, request_text)
    assert chosen is not None and chosen.name == "ui-designer"
