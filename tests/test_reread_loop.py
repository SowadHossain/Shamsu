"""The re-read loop that ate the 2026-08-24 snake-game run.

One prompt carried 32 elided tool results, every one of them saying "call
read_file for the current contents". The harness dropped the file bodies and
then, thirty-two times in the same breath, told the model to fetch them back.
Each re-read is a fresh multi-KB result that raises the pressure that triggers
the next sweep, so `read_file` became 21 of the run's 64 tool calls, two turns
burned all 24 rounds, and the model said so itself: "I've been re-reading files
without making progress."

Two causes, and both are needed - the stub that invites the re-read, and the
protection cap that guarantees a file keeps falling out of protection.
"""
from __future__ import annotations

import json

from shamsu.agents.simple_chat import (
    MAX_PROTECTED_READ_PATHS,
    RE_READ_LIMIT,
    _any_read_path,
    elide_tool_result,
)


def _read_payload(path: str, body: str = "x = 1\n" * 200) -> str:
    return json.dumps(
        {
            "ok": True,
            "message": f"read {path}",
            "data": {"resolved_filepath": path, "total_lines": 200, "content": body},
        }
    )


# -- the stub stops asking once asking has failed --------------------------


def test_a_first_elision_still_offers_the_re_read():
    stub = elide_tool_result("read_file", _read_payload("js/config.js"), rereads=1)

    assert "call read_file" in stub
    assert "x = 1" not in stub, "the body is still dropped - that is the point"


def test_a_file_read_too_often_is_no_longer_invited_back():
    stub = elide_tool_result(
        "read_file", _read_payload("js/config.js"), rereads=RE_READ_LIMIT
    )

    assert "call read_file" not in stub, "this is the sentence that built the loop"
    assert "already read this" in stub
    assert "js/config.js" in stub, "it still says WHICH file, so the model can orient"


def test_the_stub_still_carries_the_facts_worth_keeping():
    stub = json.loads(
        elide_tool_result("read_file", _read_payload("js/game.js"), rereads=9)
    )

    assert stub["data"]["resolved_filepath"] == "js/game.js"
    assert stub["data"]["total_lines"] == 200


def test_an_unrecoverable_result_is_untouched_by_the_count():
    payload = json.dumps({"ok": False, "message": "boom", "data": {"stdout": "no"}})
    assert elide_tool_result("run_command", payload, rereads=9) == elide_tool_result(
        "run_command", payload, rereads=0
    )


# -- counting a read, including one already thrown away --------------------


def test_an_elided_stub_still_counts_as_a_read():
    """A stub IS a read - one whose body was thrown away.

    Counting only intact reads would miss every lap of the loop, because the
    loop is made of stubs.
    """
    stub = elide_tool_result("read_file", _read_payload("js/snake.js"), rereads=1)

    assert _any_read_path(stub) == "js/snake.js"
    assert _any_read_path(_read_payload("js/snake.js")) == "js/snake.js"


def test_a_result_that_is_not_a_read_has_no_path():
    assert _any_read_path('{"ok": true, "message": "ran"}') == ""
    assert _any_read_path("not json at all") == ""


# -- protection covers a real project ---------------------------------------


def test_protection_covers_more_paths_than_a_four_file_project():
    """Four was measured on a four-file task; the snake game had ten.

    Six paths permanently outside protection is a set that ROTATES, and the
    rotation is what the loop feeds on.
    """
    assert MAX_PROTECTED_READ_PATHS >= 8
