"""Budget-aware history trimming + rolling-summary bookkeeping in ChatState."""
from __future__ import annotations

from shamsu.agents.chat_state import ChatState


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
    # user boundary, so the tail starts cleanly at u2.
    tail, start_abs = state.select_for_budget(9, _wc)

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
