"""The three defects that let a change request be answered in prose.

All of them were invisible on qwen3.5:9b and fatal on a 7B, which is why they
shipped: the harness nets were measured on the larger model, where they are a
safety margin, and are load-bearing on the smaller ones this project targets.
"""
from __future__ import annotations

from shamsu.agents.simple_chat import (
    active_tool_schemas,
    answered_a_change_request_with_prose,
    describes_an_unmade_edit,
)
from shamsu.agents.tool_classifier import categories_for

WRITE_TOOLS = {"write_file", "append_file", "patch_file", "replace_symbol"}


def _roster(request: str) -> set[str]:
    return {
        schema["function"]["name"]
        for schema in active_tool_schemas(request=request, context_window=32768)
    }


# --- the roster could withhold the write tools outright ------------------------

def test_build_me_a_thing_can_still_write_it():
    """The exact shipped failure. "build me a snake game" scored `run` 2.5
    against `write` 0.0 - confidently, at 0.833 - because the write signal
    required "build" to be followed literally by "a"."""
    for request in (
        "build me a snake game",
        "build me a todo app",
        "can you build me a dashboard",
        "build the API",
        "build me a website with login",
        "build out the menu system",
    ):
        assert WRITE_TOOLS & _roster(request), request


def test_a_looking_request_can_still_act_on_what_it_finds():
    """A pasted traceback scores `read` on the `.js` alone and means "fix this".
    Live 2026-08-24 exactly that turn went on to patch eight files."""
    request = "menu.js:198 Uncaught ReferenceError: module is not defined"
    assert WRITE_TOOLS & _roster(request)


def test_planning_is_still_the_one_thing_that_cannot_write():
    """The exemption is the category's whole point: a planning turn that can
    call write_file is a normal turn with a different label."""
    for request in ("plan how to add authentication", "outline the approach first"):
        assert "write" not in categories_for(request), request
        assert not (WRITE_TOOLS & _roster(request)), request


def test_a_planning_request_too_short_to_score_still_gets_everything():
    """Pre-existing and deliberately left alone. "plan the refactor first" is 23
    characters, under SHORT_MESSAGE_CHARS, and `plan` is not one of the words
    that make a short message a task - so it scores nothing, and a no-idea
    verdict has always meant "send everything" rather than "send nothing"."""
    assert categories_for("plan the refactor first") == ()
    assert WRITE_TOOLS & _roster("plan the refactor first")


def test_it_still_narrows():
    """Not a licence to send everything - the token saving is the point."""
    everything = _roster("")
    assert len(_roster("run the tests")) < len(everything)


# --- the safety net needed the model to name a file ---------------------------

_REPLY_NAMING_NOTHING = """Sure, I can modify the file to print 200 prime numbers.
Here is the updated code:

```python
def show(count):
    n, seen = 2, 0
    while seen < count:
        print(n)
        n += 1

show(200)
```

Would you like me to write this updated code to the file?
"""


def test_the_old_detector_is_blind_when_no_file_is_named():
    """Not a bug being asserted as correct - the boundary being pinned. This is
    why the new detector exists, and the next test is the same reply passing."""
    assert describes_an_unmade_edit(_REPLY_NAMING_NOTHING, ["primes.py"]) == ""


def test_a_change_request_answered_in_prose_is_caught_without_a_filename():
    assert answered_a_change_request_with_prose(
        _REPLY_NAMING_NOTHING, "Can you modify the file and make it print 200 numbers?"
    )


def test_a_planning_request_is_never_nudged_into_writing():
    """`plan` and `write` both score 3.0 on this, and PRIORITY breaks the tie
    toward `write`, so the winner cannot be trusted and the scores are compared
    directly. Getting this wrong would turn a plan into an unasked-for edit."""
    for request in (
        "plan how you would change it",
        "plan how to add authentication",
        "how should we approach the refactor?",
    ):
        assert not answered_a_change_request_with_prose(_REPLY_NAMING_NOTHING, request), request


def test_a_question_is_not_a_change_request():
    for request in ("what does this file do?", "review the modules", "run the tests"):
        assert not answered_a_change_request_with_prose(_REPLY_NAMING_NOTHING, request), request


def test_prose_without_code_is_not_a_withheld_edit():
    assert not answered_a_change_request_with_prose("I would change the loop.", "modify the file")
