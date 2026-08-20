"""What the browser shows as a conversation, and what it must not.

From a live session on 2026-08-20 in which the user asked exactly two
questions. `messages.jsonl` held 89 records; the browser drew 50 bubbles, and
attributed **twelve** of them to the user. Ten were loop nudges - "You have
already called read_file(js/game.js) this turn" - written with `role: user`
because that is the role the model has to read them in. Four more bubbles were
empty: assistant turns carrying only tool calls.

`activity.jsonl` had it right the whole time - two `turn.start` events, two
`assistant` events, one tagged `telegram` and one tagged `cli`. The terminal
and the phone render from that stream; the browser was the only surface reading
the model's context file and calling it a chat.
"""
from __future__ import annotations

import json
from pathlib import Path

from shamsu.runtime.turn_stream import TurnEvent, TurnStream
from shamsu.session.manager import ORIGIN_LOOP, SessionManager
from shamsu.webui import api


def session(tmp_path: Path):
    return SessionManager(tmp_path).create_session("thread")


def publish(tmp_path: Path, session_id: str, events: list[tuple]) -> None:
    stream = TurnStream(tmp_path, session_id)
    for seq, (kind, text, turn_id, source) in enumerate(events, start=1):
        stream.publish(
            TurnEvent(
                seq=seq,
                kind=kind,
                text=text,
                turn_id=turn_id,
                session_id=session_id,
                workspace=str(tmp_path),
                source=source,
            )
        )


def test_a_hybrid_thread_shows_two_questions_not_twelve(tmp_path: Path) -> None:
    """The live failure, reproduced: one turn from the phone, one from the
    terminal, with the loop's nudges written between them."""
    logger = session(tmp_path)
    publish(
        tmp_path,
        logger.session_id,
        [
            ("turn.start", "check game.js please", "t1", "telegram"),
            ("tool.call", "read_file js/game.js", "t1", "telegram"),
            ("assistant", "The file is corrupted.", "t1", "telegram"),
            ("turn.end", "stopped after 8m01s", "t1", "telegram"),
            ("turn.start", "fix it part by part", "t2", "cli"),
            ("assistant", "Here is part one.", "t2", "cli"),
            ("turn.end", "done in 1m02s", "t2", "cli"),
        ],
    )
    # The transcript the model reads, nudges and all.
    logger.append_message("user", "check game.js please", source="telegram")
    logger.append_message("assistant", "", tool_calls=[{"function": {"name": "read_file"}}])
    logger.append_message(
        "user",
        "You have already called read_file(js/game.js) this turn.",
        origin=ORIGIN_LOOP,
    )
    logger.append_message("assistant", "The file is corrupted.", source="telegram")

    payload = api.session_messages(tmp_path, logger.session_id)

    assert payload["built_from"] == "turns"
    roles = [row["role"] for row in payload["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert [row["content"] for row in payload["messages"] if row["role"] == "user"] == [
        "check game.js please",
        "fix it part by part",
    ]


def test_each_prompt_carries_the_surface_it_came_from(tmp_path: Path) -> None:
    logger = session(tmp_path)
    publish(
        tmp_path,
        logger.session_id,
        [
            ("turn.start", "from the phone", "t1", "telegram"),
            ("assistant", "ok", "t1", "telegram"),
            ("turn.start", "from the terminal", "t2", "cli"),
            ("assistant", "ok", "t2", "cli"),
        ],
    )
    rows = api.session_messages(tmp_path, logger.session_id)["messages"]
    assert [row["source"] for row in rows] == ["telegram", "telegram", "cli", "cli"]


def test_a_nudge_never_becomes_something_the_user_said(tmp_path: Path) -> None:
    """No turn stream, so the transcript is all there is - and it must still not
    put the loop's words in the user's mouth."""
    logger = session(tmp_path)
    logger.append_message("user", "the real question", source="cli")
    logger.append_message("user", "That reply was empty. Answer or call one tool.", origin=ORIGIN_LOOP)
    logger.append_message("assistant", "the real answer", source="cli")

    payload = api.session_messages(tmp_path, logger.session_id)

    assert payload["built_from"] == "transcript"
    assert [row["content"] for row in payload["messages"]] == [
        "the real question",
        "the real answer",
    ]


def test_an_unmarked_message_is_shown_rather_than_hidden(tmp_path: Path) -> None:
    """The safe direction. A forgotten `origin` can leak a message INTO a
    transcript; it must never be able to delete one from it."""
    logger = session(tmp_path)
    logger.append_message("user", "typed by a person, unmarked")

    rows = api.session_messages(tmp_path, logger.session_id)["messages"]
    assert [row["content"] for row in rows] == ["typed by a person, unmarked"]


def test_empty_assistant_turns_do_not_become_empty_bubbles(tmp_path: Path) -> None:
    """An assistant record with no content carried only tool calls. Four of
    them drew four blank bubbles in the live session."""
    logger = session(tmp_path)
    logger.append_message("user", "do it")
    logger.append_message("assistant", "", tool_calls=[{"function": {"name": "read_file"}}])
    logger.append_message("assistant", "done")

    rows = api.session_messages(tmp_path, logger.session_id)["messages"]
    assert [row["content"] for row in rows] == ["do it", "done"]


def test_a_turn_that_never_answered_still_shows_its_question(tmp_path: Path) -> None:
    """Otherwise the thread looks like it swallowed the prompt."""
    logger = session(tmp_path)
    publish(
        tmp_path,
        logger.session_id,
        [
            ("turn.start", "do the thing", "t1", "cli"),
            ("turn.end", "stopped after 3m21s", "t1", "cli"),
        ],
    )
    rows = api.session_messages(tmp_path, logger.session_id)["messages"]
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert rows[0]["content"] == "do the thing"
    assert rows[1]["content"] == ""
    assert rows[1]["verdict"] == "stopped after 3m21s"


def test_the_answer_carries_what_the_turn_cost(tmp_path: Path) -> None:
    """A three-minute run that changed nothing should not read as a quick
    reply."""
    logger = session(tmp_path)
    publish(
        tmp_path,
        logger.session_id,
        [
            ("turn.start", "fix it", "t1", "cli"),
            ("assistant", "I could not.", "t1", "cli"),
            ("turn.end", "stopped after 3m21s", "t1", "cli"),
        ],
    )
    answer = api.session_messages(tmp_path, logger.session_id)["messages"][1]
    assert answer["verdict"] == "stopped after 3m21s"


def test_a_session_with_no_turn_stream_falls_back_rather_than_showing_nothing(
    tmp_path: Path,
) -> None:
    logger = session(tmp_path)
    logger.append_message("user", "old question")
    logger.append_message("assistant", "old answer")

    payload = api.session_messages(tmp_path, logger.session_id)
    assert payload["built_from"] == "transcript"
    assert len(payload["messages"]) == 2


def test_an_activity_log_with_no_prompts_falls_back(tmp_path: Path) -> None:
    """A stream of status ticks and nothing else is not a conversation, and
    must not present as an empty one."""
    logger = session(tmp_path)
    publish(tmp_path, logger.session_id, [("status", "thinking 4s", "t1", "cli")])
    logger.append_message("user", "the question")
    logger.append_message("assistant", "the answer")

    payload = api.session_messages(tmp_path, logger.session_id)
    assert payload["built_from"] == "transcript"
    assert len(payload["messages"]) == 2


def test_the_conversation_is_redacted_on_the_way_out(tmp_path: Path) -> None:
    logger = session(tmp_path)
    publish(
        tmp_path,
        logger.session_id,
        [
            ("turn.start", "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "t1", "cli"),
            ("assistant", "ok", "t1", "cli"),
        ],
    )
    rows = api.session_messages(tmp_path, logger.session_id)["messages"]
    # The transcript on disk is lossless on purpose; what leaves the process is
    # not - and the new turn-stream path must not bypass that.
    assert "ghp_aaaa" not in json.dumps(rows)
    assert "[REDACTED]" in rows[0]["content"]


def test_status_and_tool_events_never_become_bubbles(tmp_path: Path) -> None:
    """They belong to the turn log, which the browser renders separately."""
    logger = session(tmp_path)
    publish(
        tmp_path,
        logger.session_id,
        [
            ("turn.start", "go", "t1", "cli"),
            ("status", "thinking 4s", "t1", "cli"),
            ("activity", "model responded in 9s", "t1", "cli"),
            ("tool.call", "read_file a.py", "t1", "cli"),
            ("tool.result", "12 lines", "t1", "cli"),
            ("assistant", "read it", "t1", "cli"),
        ],
    )
    rows = api.session_messages(tmp_path, logger.session_id)["messages"]
    assert [row["content"] for row in rows] == ["go", "read it"]
