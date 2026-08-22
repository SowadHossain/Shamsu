"""What a turn CLAIMS about itself, and what it is allowed to claim.

Live 2026-08-22, session 20260822-090221-f144. One turn: 21m52s, fifteen reads
of `js/PlayerShip.js`, four failed tool calls, no file changed, and a final
answer that ended with SHAMSU's own words -

    This answer was cut off. That answer hit my per-reply limit of 15,071
    tokens - one reply cannot be longer than that.

- under this badge:

    ✓ SUCCESS  done in 21m52s

`status="done"` meant "the loop returned without raising", which is a claim
about the PROCESS. Every surface above it was reading it as a claim about the
OUTCOME, and the one line the user actually reads carried none of the four
numbers that would have contradicted it.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from shamsu.agents.simple_chat import _turn_verdict


# -- the line -------------------------------------------------------------


def test_a_plain_answer_still_reads_plainly():
    """A question answered in nine seconds must not grow a rap sheet. "No files
    changed" is not news on a turn nobody asked to change a file."""
    assert _turn_verdict(9.0, (), stopped=False) == "done in 9s"


def test_the_line_carries_what_went_wrong():
    line = _turn_verdict(1312.0, (), stopped=False, failures=4, truncated=True)

    assert "21m52s" in line
    assert "no files changed" in line
    assert "4 tool calls failed" in line
    assert "answer cut off" in line


def test_one_failure_is_singular():
    assert "1 tool call failed" in _turn_verdict(5.0, (), stopped=False, failures=1)


def test_files_changed_still_wins_the_middle_slot():
    line = _turn_verdict(5.0, ("a.py", "b.py"), stopped=False, failures=1)

    assert "2 files changed" in line
    assert "no files changed" not in line


def test_the_line_is_ascii():
    """It reaches a Windows console and a Telegram HTML body; a decorative
    separator has crashed a cp1252 terminal here before."""
    line = _turn_verdict(1312.0, ("a.py",), stopped=True, failures=2, truncated=True)

    line.encode("ascii")


# -- the status -----------------------------------------------------------


def _published(loop) -> list:
    events: list = []
    loop.emit = events.append
    return events


def _turn_end(events) -> dict:
    return next(event.data for event in events if event.kind == "turn.end")


def test_a_cut_off_answer_is_not_reported_as_done(tmp_path: Path, monkeypatch):
    """The reported failure, at the seam where it was invented."""
    from tests.test_simple_chat import _loop, _text

    loop = _loop(tmp_path, [_text("Here is half an ans")])
    events = _published(loop)
    monkeypatch.setattr(loop, "_hit_the_length_limit", lambda: True)

    asyncio.run(loop.run("write me a plan"))

    assert _turn_end(events)["status"] == "incomplete"


def test_a_finished_answer_is_still_done(tmp_path: Path):
    """The fix must not turn every turn yellow."""
    from tests.test_simple_chat import _loop, _text

    loop = _loop(tmp_path, [_text("All done.")])
    events = _published(loop)

    asyncio.run(loop.run("what does this do?"))

    assert _turn_end(events)["status"] == "done"


def test_the_error_reaches_the_surface_that_checks_for_one(tmp_path: Path):
    """`_on_turn_end` reads `data.get("error")` and nothing ever put one there,
    so the check could not have failed a turn if it tried."""
    from tests.test_simple_chat import _loop, _text

    loop = _loop(tmp_path, [_text("ok")])
    events = _published(loop)
    asyncio.run(loop.run("hi"))

    assert "error" in _turn_end(events)


def test_the_counts_travel_with_the_verdict(tmp_path: Path):
    from tests.test_simple_chat import _loop, _text

    loop = _loop(tmp_path, [_text("ok")])
    events = _published(loop)
    asyncio.run(loop.run("hi"))
    data = _turn_end(events)

    assert data["failures"] == 0
    assert data["truncated"] is False


# -- the badge ------------------------------------------------------------


def _badge(status: str, error: str = "") -> str:
    from rich.console import Console

    from shamsu.cli.turn_render import CliTurnRenderer
    from shamsu.runtime.turn_stream import TurnEvent

    console = Console(record=True, width=100)
    renderer = CliTurnRenderer(console)
    renderer._on_turn_end(
        TurnEvent(
            seq=1,
            kind="turn.end",
            text="done in 21m52s",
            data={"status": status, "error": error},
        )
    )
    return console.export_text()


def test_an_incomplete_turn_is_neither_green_nor_red():
    """Green was the harness saying a turn succeeded directly above its own
    "This answer was cut off." Red would be just as wrong - the work up to the
    cut is real and is usually worth continuing from."""
    painted = _badge("incomplete")

    assert "INCOMPLETE" in painted
    assert "SUCCESS" not in painted
    assert "FAILED" not in painted


def test_done_is_still_success():
    assert "SUCCESS" in _badge("done")


def test_an_error_is_a_failure_whatever_the_status_says():
    assert "FAILED" in _badge("done", error="ConnectionError: refused")


def test_silence_is_not_a_verdict():
    """A caller that sends no status is not making a claim, and painting red on
    silence would be inventing one."""
    assert "SUCCESS" in _badge("")


# -- the read ceiling -----------------------------------------------------


def test_a_model_that_will_not_stop_reading_has_its_turn_ended(tmp_path: Path):
    """Two nudges were spent and ignored. A third sentence costs another eight
    reads and buys the same nothing - 21m52s of it, in the reported run."""
    from tests.test_simple_chat import _loop, _text, _tool

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    # More reads than any ceiling, so if the turn does not end it runs them all.
    turns = [_tool("read_file", filepath="a.py") for _ in range(40)]
    turns.append(_text("Finally, an answer."))
    loop = _loop(tmp_path, turns)

    result = asyncio.run(loop.run("review this"))

    assert result.stopped
    assert "stopped this turn" in result.final
    assert result.rounds < 40, "the turn ran to the end of the script instead of stopping"


def test_a_model_that_reads_then_works_is_left_alone(tmp_path: Path):
    """The ceiling is for a model that has lost the thread, not a thorough one.
    Reading a lot and then writing is how a careful turn looks."""
    from tests.test_simple_chat import _loop, _text, _tool

    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    turns = [_tool("read_file", filepath="a.py") for _ in range(6)]
    turns.append(_tool("write_file", filepath="b.py", content="y = 2\n"))
    turns.append(_text("Written."))
    loop = _loop(tmp_path, turns)

    result = asyncio.run(loop.run("review this"))

    assert not result.stopped
    assert "Written." in result.final


# -- the refused write ----------------------------------------------------


def test_a_refused_oversized_write_is_recoverable(tmp_path: Path):
    """The refusal used to end "Nothing you generated is lost; resend it in
    pieces", and that held for about four messages - `_shorten_arguments` drops
    content arguments and `MIN_VERBATIM_MESSAGES` is 4. Live 2026-08-22: a
    10,477-character plan refused, eight more reads, and then the model rebuilt
    the document into its reply where the reply cap cut it off. Two ceilings
    refused the same deliverable, and the second was only reached because the
    first had lied."""
    from tests.test_simple_chat import _loop, _text, _tool

    huge = "# plan\n" + ("a line of the plan document\n" * 900)
    loop = _loop(tmp_path, [_tool("write_file", filepath="fix-plan.md", content=huge), _text("ok")])

    asyncio.run(loop.run("write me a plan"))

    tool_messages = [m for m in loop.client.calls[1]["messages"] if m["role"] == "tool"]
    answer = tool_messages[0]["content"]
    assert "REFUSED" in answer

    spilled = list((tmp_path / ".shamsu" / "oversized").glob("*fix-plan.md"))
    assert spilled, "the payload was refused and then dropped"
    assert spilled[0].read_text(encoding="utf-8") == huge
    # The path has to be IN the answer, or saving it helps nobody.
    assert ".shamsu/oversized" in answer


def test_the_spill_stays_out_of_the_users_tree(tmp_path: Path):
    """A half-written document appearing next to real source is worse than the
    dead end it fixes."""
    from tests.test_simple_chat import _loop, _text, _tool

    huge = "x\n" * 9000
    loop = _loop(tmp_path, [_tool("write_file", filepath="fix-plan.md", content=huge), _text("ok")])
    asyncio.run(loop.run("go"))

    assert not (tmp_path / "fix-plan.md").exists()
    assert [p.name for p in tmp_path.iterdir()] == [".shamsu"]


def test_a_write_within_the_cap_is_untouched(tmp_path: Path):
    """The guard must not cost a normal write."""
    from tests.test_simple_chat import _loop, _text, _tool

    loop = _loop(tmp_path, [_tool("write_file", filepath="ok.md", content="# small\n"), _text("done")])
    asyncio.run(loop.run("go"))

    assert (tmp_path / "ok.md").read_text(encoding="utf-8") == "# small\n"
    assert not (tmp_path / ".shamsu" / "oversized").exists()
