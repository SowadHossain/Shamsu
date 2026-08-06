"""A headless run should continue its workspace's session, within limits.

The OpenBazaar build created 124 SEPARATE sessions - one per prompt - so nothing
could persist between prompts. A question asked in prompt N was gone by N+1, and a
fresh instruction arriving mid-question got recorded as the ANSWER to the old one.
Neither is a bug in the question code; both are structural.

Continuation is bounded on purpose: live testing on 2026-08-02 found history
degrades after roughly six turns, with the model echoing stale text instead of
acting. So it is capped by age AND length.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from shamsu.session.manager import SessionManager


def _age(manager: SessionManager, session_id: str, seconds: int) -> None:
    """Backdate a session's updated_at by *seconds*."""
    metadata = manager.resolve(session_id)
    metadata.updated_at = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat()
    manager._write_metadata(metadata)
    manager._upsert_index(metadata)


def test_a_fresh_workspace_creates_a_session(tmp_path: Path):
    manager = SessionManager(tmp_path)

    logger = manager.continue_or_create("Headless Run")

    assert logger.metadata.session_id


def test_a_second_prompt_continues_the_same_session(tmp_path: Path):
    """The fix for answers not persisting across prompts."""
    manager = SessionManager(tmp_path)
    first = manager.continue_or_create("Headless Run")

    second = SessionManager(tmp_path).continue_or_create("Headless Run")

    assert second.metadata.session_id == first.metadata.session_id


def test_a_stale_session_is_not_continued(tmp_path: Path):
    manager = SessionManager(tmp_path)
    first = manager.continue_or_create("Headless Run")
    _age(manager, first.metadata.session_id, 4000)

    second = SessionManager(tmp_path).continue_or_create(
        "Headless Run", max_age_seconds=1800
    )

    assert second.metadata.session_id != first.metadata.session_id


def test_a_long_session_is_not_continued(tmp_path: Path):
    """Bounded by length, because a long transcript makes the model echo it."""
    manager = SessionManager(tmp_path)
    first = manager.continue_or_create("Headless Run")
    metadata = manager.resolve(first.metadata.session_id)
    metadata.message_count = 80
    manager._write_metadata(metadata)
    manager._upsert_index(metadata)

    second = SessionManager(tmp_path).continue_or_create(
        "Headless Run", max_messages=40
    )

    assert second.metadata.session_id != first.metadata.session_id


def test_a_session_just_under_the_limits_is_continued(tmp_path: Path):
    manager = SessionManager(tmp_path)
    first = manager.continue_or_create("Headless Run")
    metadata = manager.resolve(first.metadata.session_id)
    metadata.message_count = 39
    manager._write_metadata(metadata)
    manager._upsert_index(metadata)
    _age(manager, first.metadata.session_id, 1700)

    second = SessionManager(tmp_path).continue_or_create(
        "Headless Run", max_age_seconds=1800, max_messages=40
    )

    assert second.metadata.session_id == first.metadata.session_id


def test_bounds_can_be_disabled(tmp_path: Path):
    manager = SessionManager(tmp_path)
    first = manager.continue_or_create("Headless Run")
    _age(manager, first.metadata.session_id, 999_999)

    second = SessionManager(tmp_path).continue_or_create(
        "Headless Run", max_age_seconds=0, max_messages=0
    )

    assert second.metadata.session_id == first.metadata.session_id


def test_an_unparseable_timestamp_is_not_treated_as_fresh(tmp_path: Path):
    manager = SessionManager(tmp_path)
    first = manager.continue_or_create("Headless Run")
    metadata = manager.resolve(first.metadata.session_id)
    metadata.updated_at = "not a timestamp"
    manager._write_metadata(metadata)
    manager._upsert_index(metadata)

    second = SessionManager(tmp_path).continue_or_create("Headless Run")

    assert second.metadata.session_id != first.metadata.session_id


def test_an_explicit_session_id_still_wins(tmp_path: Path):
    """`--session <id>` must keep overriding the default."""
    manager = SessionManager(tmp_path)
    first = manager.continue_or_create("Headless Run")
    other = manager.create_session("Other Work")

    resumed = SessionManager(tmp_path).resume_session(other.metadata.session_id)

    assert resumed.metadata.session_id == other.metadata.session_id
    assert resumed.metadata.session_id != first.metadata.session_id
