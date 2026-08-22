"""A contract assertion may not be signed off on a paragraph.

Live 2026-08-22, `F:\\Work\\demo2\\test`. SHAMSU built a Three.js game, marked
all seven of its own contract assertions passed, and reported:

    ✅ All requirements have been successfully implemented

The game drew neither the ship nor a single asteroid. Loaded in a real browser:
zero JavaScript errors, `playerShip` at the camera's own eye height, and
`asteroids: []` because the spawn threshold worked out to 64,000 frames.

The evidence recorded for a02 was:

    "Positioned at bottom of screen using camera position calculations"

- an accurate description of the exact line that put the ship outside the
frustum. Every assertion was like that: fluent, specific, and never checked.

`contract_assert_pass` required its `evidence` argument to be a non-empty
string. That was the whole test. Its own refusal message asks the right
question - "what did you run, and what did it say?" - and then accepted prose
that answered neither.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from shamsu.agents import simple_contract as contracts


def _contract() -> contracts.Contract:
    return contracts.new_contract(
        "3D Asteroids",
        "a game",
        ["ship is drawn at the bottom", "asteroids fall from the top"],
    )


# -- the tool ---------------------------------------------------------------


def _loop_with_contract(tmp_path: Path, turns):
    from tests.test_simple_chat import _loop

    contracts.save_contract(tmp_path, _contract())
    return _loop(tmp_path, turns)


def _tool_replies(loop) -> list[str]:
    return [m.content for m in loop.state.all_messages if m.role == "tool"]


def test_a_paragraph_alone_no_longer_passes_an_assertion(tmp_path: Path):
    """The reported failure. Nothing run, nothing written - just prose."""
    from tests.test_simple_chat import _text, _tool

    loop = _loop_with_contract(
        tmp_path,
        [
            _tool(
                "contract_assert_pass",
                assertion_id="a01",
                evidence="Player ship implemented as 3D cone (lines 170-191): "
                "createPlayerShip() positions it at the bottom of the screen.",
            ),
            _text("done"),
        ],
    )
    asyncio.run(loop.run("build it"))

    said = _tool_replies(loop)[0]
    assert "cannot be marked passed" in said
    assert "run_tests" in said or "run_command" in said
    assert "contract_assert_skip" in said, "it must name the honest way out"

    saved = contracts.load_contract(tmp_path)
    assert saved.find("a01").state == contracts.PENDING


def test_a_command_that_exited_zero_backs_it(tmp_path: Path):
    from tests.test_simple_chat import _text, _tool

    (tmp_path / "check.py").write_text("print('ok')\n", encoding="utf-8")
    loop = _loop_with_contract(
        tmp_path,
        [
            _tool("run_command", command="python check.py"),
            _tool("contract_assert_pass", assertion_id="a01", evidence="it printed ok"),
            _text("done"),
        ],
    )
    asyncio.run(loop.run("build it"))

    saved = contracts.load_contract(tmp_path)
    item = saved.find("a01")
    assert item.state == contracts.PASSED
    assert item.verified_by == contracts.BY_RUN
    assert item.is_verified
    assert "exited 0" in item.observation


def test_a_write_backs_it_but_is_recorded_as_weaker(tmp_path: Path):
    """A write proves the text reached the disk. It does not prove the text is
    right, and the contract has to be able to tell those apart."""
    from tests.test_simple_chat import _text, _tool

    loop = _loop_with_contract(
        tmp_path,
        [
            _tool("write_file", filepath="game.js", content="// the ship\n"),
            _tool("contract_assert_pass", assertion_id="a01", evidence="the ship is drawn"),
            _text("done"),
        ],
    )
    asyncio.run(loop.run("build it"))

    item = contracts.load_contract(tmp_path).find("a01")
    assert item.state == contracts.PASSED
    assert item.verified_by == contracts.BY_WRITE
    assert not item.is_verified
    assert "not run" in item.observation


def test_the_models_own_words_are_kept_but_labelled(tmp_path: Path):
    """Keeping them matters - they are how a person reads the contract back.
    Presenting them as the evidence is what went wrong."""
    from tests.test_simple_chat import _text, _tool

    loop = _loop_with_contract(
        tmp_path,
        [
            _tool("write_file", filepath="game.js", content="x\n"),
            _tool("contract_assert_pass", assertion_id="a01", evidence="my paragraph"),
            _text("done"),
        ],
    )
    asyncio.run(loop.run("go"))

    rendered = contracts.load_contract(tmp_path).render()
    assert "you said: my paragraph" in rendered
    assert "backed by:" in rendered


# -- the done claim ---------------------------------------------------------


def test_a_done_claim_is_corrected_when_every_pass_is_only_a_write():
    """`done_guard` returned "" the moment every assertion was resolved, so a
    contract signed off entirely on file writes waved through "All requirements
    have been successfully implemented"."""
    contract = _contract()
    for item in contract.assertions:
        item.state = contracts.PASSED
        item.verified_by = contracts.BY_WRITE
        item.observation = "wrote index.html (not run)"

    correction = done_correction = contracts.done_guard(
        contract, "All requirements have been successfully implemented!"
    )

    assert correction
    assert "a01" in correction and "a02" in correction
    assert "not evidence that the code runs" in correction
    assert done_correction  # named twice on purpose; the guard must fire


def test_a_done_claim_is_left_alone_when_something_actually_ran():
    contract = _contract()
    for item in contract.assertions:
        item.state = contracts.PASSED
        item.verified_by = contracts.BY_RUN
        item.observation = "run_command(node -c game.js) exited 0"

    assert contracts.done_guard(contract, "All done!") == ""


def test_an_unresolved_contract_is_still_corrected_the_old_way():
    """The original guard must survive the new one."""
    contract = _contract()
    correction = contracts.done_guard(contract, "All done!")

    assert "nobody has checked" in correction


def test_a_sentence_that_is_not_a_done_claim_is_never_touched():
    contract = _contract()
    for item in contract.assertions:
        item.state = contracts.PASSED
        item.verified_by = contracts.BY_WRITE

    assert contracts.done_guard(contract, "Next I will add the asteroids.") == ""


# -- the render -------------------------------------------------------------


def test_the_render_says_when_a_resolved_contract_proves_nothing():
    contract = _contract()
    for item in contract.assertions:
        item.state = contracts.PASSED
        item.verified_by = contracts.BY_WRITE
        item.observation = "wrote index.html (not run)"

    rendered = contract.render()

    assert contract.done
    assert len(contract.unproven) == 2
    assert "nothing has been run" in rendered
    assert "You can report the task finished." not in rendered


def test_a_fully_verified_contract_still_says_so():
    contract = _contract()
    for item in contract.assertions:
        item.state = contracts.PASSED
        item.verified_by = contracts.BY_RUN
        item.observation = "run_tests() exited 0"

    assert contract.unproven == []
    assert "You can report the task finished." in contract.render()


def test_provenance_survives_a_round_trip_to_disk(tmp_path: Path):
    contract = _contract()
    contract.assertions[0].state = contracts.PASSED
    contract.assertions[0].verified_by = contracts.BY_RUN
    contract.assertions[0].observation = "run_tests() exited 0"
    contracts.save_contract(tmp_path, contract)

    item = contracts.load_contract(tmp_path).find("a01")
    assert item.verified_by == contracts.BY_RUN
    assert item.observation == "run_tests() exited 0"


def test_an_old_contract_without_provenance_still_loads(tmp_path: Path):
    """Contracts written before this exist on disk, including the one in the
    workspace that found the bug."""
    import json

    raw = {
        "title": "old",
        "brief": "",
        "created": 0.0,
        "assertions": [{"id": "a01", "text": "x", "state": "passed", "evidence": "a paragraph"}],
    }
    path = tmp_path / ".shamsu"
    path.mkdir(parents=True, exist_ok=True)
    (path / "contract.json").write_text(json.dumps(raw), encoding="utf-8")

    item = contracts.load_contract(tmp_path).find("a01")
    assert item.state == contracts.PASSED
    assert item.verified_by == contracts.BY_NOTHING
    assert not item.is_verified, "an unprovenanced pass must not read as verified"
