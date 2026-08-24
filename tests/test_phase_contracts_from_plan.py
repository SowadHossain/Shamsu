r"""One contract per phase, derived from the plan document.

`contract_create` asks the model to write a list. In `demo-3/asteroid` the
model wrote PLAN.md with eight named phases, then wrote contracts that matched
nothing in it - five of them across the session, each overwriting the last - and
"phase 2" came to mean whichever decomposition the rolling summary had invented.

The obvious fix, one contract with one assertion per phase, was measured and
rejected: the eight phase HEADINGS produce 0 assertions that trip the runtime
gate, because a heading is a unit of WORK and not a checkable claim. Every one
of them would pass on a file write - the failure this whole mechanism exists to
stop, rebuilt out of its own fix.

A phase's own items are closer to claims and there are 3-5 of them, so each
phase renders in ~500 characters instead of 2,019. What actually holds the line
is `requires_run`: derived from a plan, provenance decides rather than wording.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents import simple_contract as contracts
from shamsu.agents.plan_anchor import (
    ask_for_a_plan,
    contract_from_phase,
    phase_progress,
    plan_phases,
)
from shamsu.agents.simple_chat import SimpleChatLoop

PLAN = """# Asteroids - Development Plan

## Project Overview
A game.

## Step-by-Step Approach

### Phase 1: Project Setup & Scaffolding
1. **Initialize Vite project** - Create base HTML entry point
2. **Configure build tools** - Set up ES6 module bundling with Vite

### Phase 2: Core Game Loop & Scene Setup (main.js)
1. Initialize Three.js scene, camera, renderer
2. Create HTML UI overlays (score counter, game over screen)
3. Implement requestAnimationFrame game loop with delta time tracking

### Phase 3: Player Ship Module (player.js)
1. Define ship geometry
2. Implement A/D movement controls

## Exact File Structure
main.js, player.js
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "PLAN.md").write_text(PLAN, encoding="utf-8")
    return tmp_path


def _loop(workspace: Path) -> SimpleChatLoop:
    loop = SimpleChatLoop.__new__(SimpleChatLoop)
    loop.workspace = workspace
    loop._activity = lambda _message: None
    loop._observed_runs = []
    loop._observed_writes = []
    return loop


# -- reading the phases out of the document ---------------------------------


def test_each_phase_carries_its_own_items(workspace: Path):
    phases = plan_phases(workspace)

    assert [p.slug for p in phases] == ["phase-01", "phase-02", "phase-03"]
    assert len(phases[1].steps) == 3
    assert phases[1].steps[0] == "Initialize Three.js scene, camera, renderer"


def test_a_following_section_does_not_become_part_of_the_last_phase(workspace: Path):
    """`## Exact File Structure` is not Phase 3."""
    assert not any("main.js, player.js" in step for step in plan_phases(workspace)[2].steps)


@pytest.mark.parametrize("named", ["2", "phase 2", "Phase 2", "core game loop"])
def test_a_phase_can_be_named_the_way_a_person_names_it(workspace: Path, named: str):
    matched = [p for p in plan_phases(workspace) if p.matches(named)]

    assert len(matched) == 1
    assert matched[0].slug == "phase-02"


# -- what the derived contract is -------------------------------------------


def test_the_items_go_in_verbatim(workspace: Path):
    """Rewording them would be the model re-describing its own plan, which is
    the drift being removed."""
    contract = contract_from_phase(plan_phases(workspace)[1])

    assert [a.text for a in contract.assertions] == list(plan_phases(workspace)[1].steps)
    assert contract.slug == "phase-02"
    assert "PLAN.md" in contract.source


def test_a_plan_derived_contract_cannot_be_passed_on_a_write(workspace: Path):
    """The whole reason this is safe. A phase heading trips no wording rule -
    provenance is what gates it."""
    loop = _loop(workspace)
    loop._contract_tool("contract_from_plan", {"phase": "2"})
    loop._observed_writes = ["src/main.js"]

    result = loop._contract_tool(
        "contract_assert_pass",
        {"assertion_id": "a01", "evidence": "I wrote the scene setup"},
    )

    assert not result.ok
    assert result.data.get("refused") == "plan_phase_needs_a_run"
    assert contracts.load_contract(workspace).find("a01").state == contracts.PENDING


def test_the_same_assertion_passes_once_something_has_run(workspace: Path):
    loop = _loop(workspace)
    loop._contract_tool("contract_from_plan", {"phase": "2"})
    loop._observed_runs = ["run_command(npm run build) exited 0"]

    result = loop._contract_tool(
        "contract_assert_pass", {"assertion_id": "a01", "evidence": "build is clean"}
    )

    item = contracts.load_contract(workspace).find("a01")
    assert result.ok
    assert item.state == contracts.PASSED
    assert item.verified_by == contracts.BY_RUN


def test_a_derived_phase_fits_the_anchor_budget(workspace: Path):
    """The rejected design rendered at 2,019 characters against a 900 cap."""
    for phase in plan_phases(workspace):
        assert len(contract_from_phase(phase).render()) < 900


# -- moving between phases --------------------------------------------------


def test_starting_a_new_phase_does_not_erase_the_last_one(workspace: Path):
    """Five contracts were created in the demo-3 session and one survived,
    because `contract.json` is overwritten by design."""
    loop = _loop(workspace)
    loop._contract_tool("contract_from_plan", {"phase": "1"})
    contract = contracts.load_contract(workspace)
    for item in contract.assertions:
        item.state = contracts.PASSED
        item.verified_by = contracts.BY_RUN
        item.observation = "run_command(npm install) exited 0"
    contracts.save_contract(workspace, contract)

    loop._contract_tool("contract_from_plan", {"phase": "2"})

    kept = contracts.load_phase_contract(workspace, "phase-01")
    assert kept is not None and kept.done, "the phase 1 evidence must survive"
    assert contracts.load_contract(workspace).slug == "phase-02"


def test_an_unfinished_phase_is_not_silently_abandoned(workspace: Path):
    loop = _loop(workspace)
    loop._contract_tool("contract_from_plan", {"phase": "2"})

    result = loop._contract_tool("contract_from_plan", {"phase": "3"})

    assert not result.ok
    assert result.data.get("blocked_by") == "phase-02"
    assert contracts.load_contract(workspace).slug == "phase-02", "phase 2 stays active"


def test_returning_to_a_phase_resumes_it_rather_than_re_deriving(workspace: Path):
    """Re-deriving would wipe the evidence already recorded for that phase."""
    loop = _loop(workspace)
    loop._contract_tool("contract_from_plan", {"phase": "1"})
    contract = contracts.load_contract(workspace)
    contract.find("a01").state = contracts.SKIPPED
    contract.find("a01").evidence = "no bundler needed yet"
    contracts.save_contract(workspace, contract)

    result = loop._contract_tool("contract_from_plan", {"phase": "1"})

    assert result.data.get("resumed") is True
    resumed = contracts.load_contract(workspace)
    assert resumed.find("a01").state == contracts.SKIPPED
    assert resumed.find("a01").evidence == "no bundler needed yet"


def test_going_back_to_an_earlier_phase_still_names_what_is_open(workspace: Path):
    """Blocked the same way as going forward - abandoning phase 2 halfway is
    abandoning it whichever direction you move."""
    loop = _loop(workspace)
    loop._contract_tool("contract_from_plan", {"phase": "2"})

    result = loop._contract_tool("contract_from_plan", {"phase": "1"})

    assert not result.ok
    assert result.data.get("blocked_by") == "phase-02"


def test_an_unknown_phase_says_what_the_phases_are(workspace: Path):
    result = _loop(workspace)._contract_tool("contract_from_plan", {"phase": "9"})

    assert not result.ok
    assert "Phase 2: Core Game Loop" in result.message


# -- what the model is told -------------------------------------------------


def test_the_progress_line_reports_each_phase(workspace: Path):
    loop = _loop(workspace)
    loop._contract_tool("contract_from_plan", {"phase": "2"})

    progress = phase_progress(workspace)

    assert "phase-01  [not started]" in progress
    assert "phase-02  [in progress - 3 of 3 left]" in progress


def test_a_workspace_with_a_plan_is_pointed_at_the_phases_not_a_new_list(workspace: Path):
    """Asking for `contract_create` beside an existing PLAN.md is how the two
    decompositions came apart."""
    request = "Build the game, then add the asteroids, then wire up collisions."

    asked = ask_for_a_plan(request, workspace)

    assert "contract_from_plan" in asked
    assert "contract_create" not in asked


def test_without_a_plan_the_original_ask_is_unchanged(tmp_path: Path):
    request = "Build the game, then add the asteroids, then wire up collisions."

    assert "contract_create" in ask_for_a_plan(request, tmp_path)
    assert "contract_create" in ask_for_a_plan(request)


def test_the_anchor_shows_the_phases_and_the_open_contract(workspace: Path):
    loop = _loop(workspace)
    loop._contract_tool("contract_from_plan", {"phase": "2"})

    standing = loop._standing_plan()

    assert "PHASES IN PLAN.md" in standing
    assert "Phase 2: Core Game Loop & Scene Setup (main.js)" in standing
    assert "Implement requestAnimationFrame game loop" in standing


def test_an_open_hand_made_contract_is_not_overwritten(workspace: Path):
    """A `contract_create` contract has no archive file, so overwriting it does
    not lose its place in a sequence - it loses the contract."""
    loop = _loop(workspace)
    contracts.save_contract(
        workspace, contracts.new_contract("Ad-hoc fix", "", ["the crash is gone"])
    )

    result = loop._contract_tool("contract_from_plan", {"phase": "1"})

    assert not result.ok
    assert contracts.load_contract(workspace).title == "Ad-hoc fix"


def test_a_finished_contract_never_blocks_the_next_phase(workspace: Path):
    loop = _loop(workspace)
    done = contracts.new_contract("Ad-hoc fix", "", ["the crash is gone"])
    done.assertions[0].state = contracts.PASSED
    done.assertions[0].verified_by = contracts.BY_RUN
    contracts.save_contract(workspace, done)

    assert loop._contract_tool("contract_from_plan", {"phase": "1"}).ok
