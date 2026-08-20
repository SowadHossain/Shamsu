"""Read-only detection: what counts as "do not change anything"."""
from __future__ import annotations


def test_reading_selectively_is_not_a_read_only_task():
    """Live 2026-08-20: "Fix the file part by part: read the skeleton first,
    read only the functions you need, then fix the issues" was classified
    read-only, so a run that fixed a real bug reported `contract violation:
    prompt forbade file changes but 2 changed`.

    The sentence asks the model to read SELECTIVELY - the opposite of a refusal
    to write, and exactly the phrasing the outline-first read path invites."""
    from shamsu.safety.read_only import applies

    assert not applies(
        "player.js has bugs. Fix the file part by part: read the skeleton "
        "first, read only the functions you need, then fix the issues."
    )
    assert not applies("read only what you need")
    assert not applies("read only those methods")


def test_the_read_only_mode_is_still_recognised():
    """Narrowing the spaced form must not switch the guard off."""
    from shamsu.safety.read_only import applies

    assert applies("this is read-only")
    assert applies("treat the repo as readonly")
    assert applies("this task is read only")
    assert applies("do not change any files")
    assert applies("leave the files alone")
