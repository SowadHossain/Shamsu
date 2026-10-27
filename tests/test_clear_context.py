"""`/clear` - the model starts fresh; the transcript keeps everything.

The half of context control that was missing. `/compact clear` forgets the
DIGEST of older turns and leaves the turns, so the next prompt rebuilds a
digest from the same messages and carries on remembering. Nothing could say
"stop carrying this conversation" except `/sessions new`, which changes which
thread you are in.

The rule these tests exist to hold: clearing hides messages from the MODEL and
never destroys them. A context you cannot clear is an annoyance; a transcript
that a clear deletes is data loss.
"""
from __future__ import annotations

import pytest

from shamsu.session.manager import SessionManager


@pytest.fixture
def logger(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.create_session("clearing")
    for index in range(6):
        session.append_message("user" if index % 2 == 0 else "assistant", f"m{index}")
    return session


def test_a_fresh_session_hides_nothing(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.create_session("new")
    assert session.context_floor() == 0


def test_clearing_reports_what_it_forgets(logger):
    assert logger.clear_context() == 6


def test_the_transcript_survives_a_clear(logger):
    """The whole point. `/sessions show` and history_search must still work."""
    logger.clear_context()
    assert len(logger.read_messages(None)) == 6


def test_the_floor_moves_to_the_end(logger):
    logger.clear_context()
    assert logger.context_floor() == 6


def test_the_summary_goes_with_it(logger):
    """A digest of turns the model can no longer see is worse than neither -
    it reads as a memory of something absent."""
    logger.save_summary("earlier the user asked for a game", 3)
    logger.clear_context()
    summary, _upto = logger.load_summary()
    assert summary == ""


def test_messages_after_a_clear_are_kept(logger):
    logger.clear_context()
    logger.append_message("user", "the new first thing")
    assert logger.context_floor() == 6
    assert len(logger.read_messages(None)) == 7


def test_clearing_twice_is_not_destructive(logger):
    logger.clear_context()
    logger.append_message("user", "after")
    assert logger.clear_context() == 7
    assert len(logger.read_messages(None)) == 7


# -- the part that decides what the model sees -------------------------------


def _hydrated(logger):
    from shamsu.agents.chat_state import ChatState

    state = ChatState("sys", session_logger=logger, hydrate=True)
    return [
        m.content for m in state.all_messages if m.role in {"user", "assistant"}
    ]


def test_history_reaches_the_model_before_a_clear(logger):
    assert "m0" in _hydrated(logger)


def test_history_does_not_reach_the_model_after_a_clear(logger):
    """The behaviour `/clear` is for."""
    logger.clear_context()
    assert _hydrated(logger) == []


def test_only_what_came_after_reaches_the_model(logger):
    logger.clear_context()
    logger.append_message("user", "the new first thing")
    carried = _hydrated(logger)
    assert carried == ["the new first thing"]
    assert not any(text.startswith("m") for text in carried)
