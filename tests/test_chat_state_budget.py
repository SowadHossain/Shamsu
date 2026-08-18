"""Budget-aware history trimming + rolling-summary bookkeeping in ChatState."""
from __future__ import annotations

from shamsu.session.manager import SessionManager
from shamsu.agents.chat_state import ChatMessage, ChatState


def _wc(text: str) -> int:
    """Deterministic word-count stand-in for the real token counter."""
    return len(text.split())


def _state() -> ChatState:
    return ChatState("sys", session_logger=None, hydrate=False)


def test_select_keeps_everything_when_budget_is_large():
    state = _state()
    state.append_user("u1 a b")
    state.append_assistant("a1 x")
    state.append_user("u2 c d e")
    state.append_assistant("a2 y z")

    tail, start_abs = state.select_for_budget(1000, _wc)

    assert start_abs == 1  # nothing evicted
    assert [m.content for m in tail] == ["u1 a b", "a1 x", "u2 c d e", "a2 y z"]


def test_select_evicts_oldest_and_snaps_to_user_boundary():
    state = _state()
    state.append_user("u1 a b")       # 3
    state.append_assistant("a1 x")    # 2
    state.append_user("u2 c d e")     # 4
    state.append_assistant("a2 y z")  # 3

    # Budget 9 minus sys(1) admits a2(3)+u2(4)=7 but not a1(2) on top; cut lands on u2, a
    # user boundary, so the tail starts cleanly at u2. `per_message_overhead=0`
    # isolates the SELECTION rule from the chat-template envelope, which has
    # tests of its own below.
    tail, start_abs = state.select_for_budget(9, _wc, per_message_overhead=0)

    assert start_abs == 3
    assert [m.role for m in tail] == ["user", "assistant"]
    assert [m.content for m in tail] == ["u2 c d e", "a2 y z"]


def test_select_always_keeps_last_message_even_if_it_overflows():
    state = _state()
    state.append_user("u1 a b")
    state.append_assistant("this final answer is quite long indeed")  # 7 words

    tail, start_abs = state.select_for_budget(1, _wc)

    assert [m.content for m in tail] == ["this final answer is quite long indeed"]
    assert start_abs == 2


def test_select_snaps_forward_past_a_leading_non_user_message():
    state = _state()
    state.append_assistant("asst0 aa bb")  # atypical assistant-led opener
    state.append_user("u1 a b")
    state.append_assistant("a1 x")

    # Everything fits, but the suffix is snapped forward to the user boundary so
    # a dangling leading assistant turn is dropped rather than sent alone.
    tail, start_abs = state.select_for_budget(1000, _wc)

    assert start_abs == 2
    assert [m.role for m in tail] == ["user", "assistant"]


def test_newly_evicted_and_rolling_summary_advance_monotonically():
    state = _state()
    for i in range(4):
        state.append_user(f"u{i}")
        state.append_assistant(f"a{i}")

    pending = state.newly_evicted(start_abs=3)
    assert [m.content for m in pending] == ["u0", "a0"]  # _messages[1:3]

    state.update_rolling_summary("first summary", start_abs=3)
    assert state.rolling_summary == "first summary"

    # Next round evicts further; only the not-yet-summarized slice is returned.
    pending2 = state.newly_evicted(start_abs=5)
    assert [m.content for m in pending2] == ["u1", "a1"]  # _messages[3:5]

    # A smaller start_abs (budget grew) never rewinds the summarized watermark.
    state.update_rolling_summary("second summary", start_abs=2)
    assert state.newly_evicted(start_abs=6)  # watermark stayed at 5, not 2


def test_build_ollama_messages_prepends_system_and_summary():
    state = _state()
    state.append_user("u1")
    state.append_assistant("a1")
    state.update_rolling_summary("older stuff happened", start_abs=2)

    tail, _ = state.select_for_budget(1000, _wc)
    built = state.build_ollama_messages(tail, include_summary=True)

    assert built[0] == {"role": "system", "content": "sys"}
    assert built[1]["role"] == "system"
    assert "older stuff happened" in built[1]["content"]
    assert [m.get("content") for m in built[2:]] == ["u1", "a1"]

    # Without the summary flag, only system + tail are sent.
    built_no_summary = state.build_ollama_messages(tail, include_summary=False)
    assert len(built_no_summary) == 3
    assert built_no_summary[1]["content"] == "u1"


def test_user_persistence_keeps_clean_request_not_internal_harness(tmp_path):
    logger = SessionManager(tmp_path).create_session("clean")
    state = ChatState("sys", session_logger=logger, hydrate=False)

    state.append_user(
        "remove login\n\n## SHAMSU Task Harness\nhuge internal prompt",
        persisted_content="remove login",
    )

    stored = logger.read_messages(10)[-1]
    assert stored["content"] == "remove login"
    assert "SHAMSU Task Harness" not in stored["content"]


def test_legacy_hydration_strips_internal_harness_blocks(tmp_path):
    logger = SessionManager(tmp_path).create_session("legacy")
    logger.append_message(
        "user",
        "remove login\n\n## SHAMSU Task Harness\n"
        "Mode: code_edit\n\n## Active SHAMSU Skills\nlarge skill text",
    )

    state = ChatState("sys", session_logger=logger, hydrate=True)

    hydrated = [message for message in state.all_messages if message.role == "user"]
    assert hydrated[0].content == "remove login"


# ---------------------------------------------------------------------------
# Ground-truth accounting (SMALLCODE plan item A)
#
# A prompt believed to be 21,381 tokens was really ~31,400 of a 32,768 window,
# so the budget never trimmed and 19 generations were cut off mid-word. Two
# independent undercounts caused it; each gets a test that fails if it returns.
# ---------------------------------------------------------------------------


def _write_call(path: str, body: str) -> list[dict]:
    return [{"function": {"name": "write_file", "arguments": {"filepath": path, "content": body}}}]


def test_tool_call_payloads_are_charged_to_the_budget():
    """An assistant turn carrying a whole file used to cost ZERO."""
    from shamsu.context.budget import message_tokens

    body = "x = 1\n" * 400
    empty = ChatMessage("assistant", "")
    carrying = ChatMessage("assistant", "", tool_calls=_write_call("game.js", body))

    assert message_tokens(carrying, _wc) > message_tokens(empty, _wc) + 100


def test_selection_evicts_a_message_whose_cost_is_all_payload():
    """The write_file turn must be evictable; before, it was free and immortal."""
    state = _state()
    state.append_user("u1")
    state.append_assistant("", tool_calls=_write_call("game.js", "y = 2\n" * 200))
    state.append_user("u2")
    state.append_assistant("done")

    # A budget that comfortably fits the prose turns but not the payload.
    tail, start_abs = state.select_for_budget(40, _wc)

    assert start_abs > 1, "the payload turn was counted as free and never evicted"
    assert all(not m.tool_calls for m in tail)


def test_every_message_is_charged_its_chat_template_envelope():
    """Role markers and turn tokens are ~8/message and were counted as zero."""
    from shamsu.context.budget import PER_MESSAGE_OVERHEAD, message_tokens

    assert message_tokens(ChatMessage("user", ""), _wc) == PER_MESSAGE_OVERHEAD


def test_message_tokens_reads_dicts_and_objects_alike():
    """`ChatState` holds objects, `_call_model` builds dicts; both must count."""
    from shamsu.context.budget import message_tokens

    as_object = ChatMessage("assistant", "hello there", tool_calls=_write_call("a.py", "pass"))
    as_dict = {
        "role": "assistant",
        "content": "hello there",
        "tool_calls": _write_call("a.py", "pass"),
    }
    assert message_tokens(as_object, _wc) == message_tokens(as_dict, _wc)


def test_system_prompt_is_charged_before_history():
    """The budget is for the whole message list, system prompt included."""
    state = ChatState("a b c d e f g h i j", session_logger=None, hydrate=False)
    state.append_user("u1")
    state.append_assistant("a1")

    # 11 total, minus the 10-word system prompt, leaves room for one short
    # turn only - so the older one must fall out. If the system prompt were
    # not charged, both would fit and nothing would be evicted.
    _tail, start_abs = state.select_for_budget(11, _wc, per_message_overhead=0)
