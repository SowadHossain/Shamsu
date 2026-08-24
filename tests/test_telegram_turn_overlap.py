r"""A second Telegram message must not start a second turn on the same files.

Live 2026-08-24, `F:\Work\shamsu test - 24aug\demo-3\asteroid`:

    03:21:40  turn.start  turn-45fb3f7f8328  "I dont think the game is rendering..."
    03:23:17  turn.start  turn-b86e7b0788b9  "Read the phase 2 requirements..."
    03:32:31  turn.end    turn-45fb3f7f8328  done in 10m51s - 3 tool calls failed
    03:34:02  turn.end    turn-b86e7b0788b9  done in 10m44s - 2 tool calls failed

Nine minutes of overlap, both editing `src/main.js`. All three
`old_string not found in src/main.js. The file was NOT changed` failures that
session were one turn patching a file the other had moved underneath it.

`route_user_message` already had the guard: ask `active_runs_for_session`
whether something is in flight and merge the new message in as feedback if so.
It asked an empty registry. `register_run` is reached only through
`RunController`, which belongs to the legacy engine, so a simple-mode turn -
the default since 2026-08-18 - registered nowhere and was invisible to it.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shamsu.integrations.telegram import sessions as telegram_sessions
from shamsu.integrations.telegram.models import TelegramInboundMetadata
from shamsu.runtime import run_control


def _metadata(session_id: str, message_id: int) -> TelegramInboundMetadata:
    return TelegramInboundMetadata(
        source="telegram",
        telegram_user_id=1,
        telegram_chat_id=2,
        telegram_message_id=message_id,
        session_id=session_id,
        timestamp=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def _simple_mode_and_a_clean_registry(monkeypatch):
    # conftest pins SHAMSU_LEGACY_ROUTING=1 for every file but test_simple_chat,
    # and this bug lives on the SIMPLE path - which has been the production
    # default since 2026-08-18 and is what the demo-3 session ran.
    monkeypatch.delenv("SHAMSU_LEGACY_ROUTING", raising=False)
    run_control._RUNS.clear()
    yield
    run_control._RUNS.clear()


def test_a_simple_mode_turn_is_visible_while_it_runs(tmp_path: Path):
    """The whole bug in one assertion: the registry was empty mid-turn."""
    gateway = telegram_sessions.LocalShamsuSessionGateway(tmp_path)
    logger = gateway.session_manager.resume_session(gateway.ensure_default_session())
    seen: list[int] = []

    class SpyLoop:
        def __init__(self, *args, **kwargs):
            self.on_activity = kwargs.get("on_activity") or (lambda _m: None)

        async def run(self, text: str):
            # DURING the turn, which is the only moment that matters.
            seen.append(len(run_control.active_runs_for_session(logger.session_id)))
            from shamsu.agents.simple_chat import SimpleChatResult

            return SimpleChatResult(final=f"Echo: {text}")

    from shamsu.agents import simple_chat

    original = simple_chat.SimpleChatLoop
    simple_chat.SimpleChatLoop = SpyLoop
    try:
        asyncio.run(
            gateway.route_user_message("first", metadata=_metadata(logger.session_id, 1))
        )
    finally:
        simple_chat.SimpleChatLoop = original

    assert seen == [1], "the running turn must be registered while it runs"
    assert not run_control.active_runs_for_session(
        logger.session_id
    ), "and deregistered when it ends"


def test_a_message_arriving_mid_turn_becomes_feedback(tmp_path: Path):
    """Rather than a second turn racing the first over the same file."""
    gateway = telegram_sessions.LocalShamsuSessionGateway(tmp_path)
    session_id = gateway.ensure_default_session()
    logger = gateway.session_manager.resume_session(session_id)

    run = run_control.register_run("run-in-flight", session_logger=logger)
    try:
        result = asyncio.run(
            gateway.route_user_message("second", metadata=_metadata(session_id, 2))
        )
    finally:
        run_control._cleanup_run(run)

    assert result.run_id == "run-in-flight"
    assert "feedback" in result.text.lower()


def test_the_registration_is_released_even_when_the_turn_raises(tmp_path: Path):
    gateway = telegram_sessions.LocalShamsuSessionGateway(tmp_path)
    session_id = gateway.ensure_default_session()

    class Exploding:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, text: str):
            raise RuntimeError("model died mid-turn")

    from shamsu.agents import simple_chat

    original = simple_chat.SimpleChatLoop
    simple_chat.SimpleChatLoop = Exploding
    try:
        # Whether the failure propagates or is turned into an error reply is a
        # separate question and not what this test is about.
        with contextlib.suppress(Exception):
            asyncio.run(
                gateway.route_user_message("boom", metadata=_metadata(session_id, 3))
            )
    finally:
        simple_chat.SimpleChatLoop = original

    assert not run_control.active_runs_for_session(session_id), (
        "a crashed turn that stays registered blocks the session forever"
    )
