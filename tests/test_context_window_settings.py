"""The context window: one source, one legal set of values, no typos.

Three defects, one theme - a window is a VRAM reservation that every call in
the process has to agree on, and three separate places were free to disagree
about it.

* `shared_num_ctx` read the environment and NOT `settings.json`, while
  `simple_chat.max_ctx` read both. So a window set from inside SHAMSU applied
  to the chat call and not to any background call, and Ollama reloads a ~6GB
  model whenever `num_ctx` changes. Live 2026-08-30: a saved 32786 - one key
  away from 32768 - against a manager asking 32768.
* `/context window 32k` computed `"32".replace("k", "024")` = 32024. Correct
  for `1k` and wrong for every other value; `4k` became 4024, which is below
  the floor and was refused with a message about 4096.
* Three budgets were sized against the install-wide maximum rather than the
  window the session actually has, so a session walked down to 8k by
  `_shrink_for_oom` still allowed 8,000 tokens for one tool result: 98% of it.
"""
from __future__ import annotations

import pytest

from shamsu.agents.simple_chat import (
    SKELETON_BUDGET_RATIO,
    max_ctx,
    summary_budget,
    tool_result_budget,
)
from shamsu.cli.repl import parse_ctx_window, snap_ctx_window
from shamsu.context.budget import (
    DEFAULT_CHAT_CTX,
    MIN_USABLE_CTX_WINDOW,
    OFFERED_CTX_WINDOWS,
    chat_ctx_ceiling,
    shared_num_ctx,
)

MODEL = "qwen2.5-coder:7b-instruct-q4_K_M"


# -- one source for the ceiling ---------------------------------------------


@pytest.fixture
def saved_window(monkeypatch):
    """Pin what `settings.chat_max_ctx()` returns, without touching the file."""

    def pin(value):
        monkeypatch.setattr(
            "shamsu.runtime.settings.chat_max_ctx", lambda: value, raising=False
        )

    monkeypatch.delenv("SHAMSU_CHAT_MAX_CTX", raising=False)
    return pin


def test_a_saved_window_reaches_the_background_calls_too(saved_window):
    """The regression. Simple mode obeyed the setting; the manager did not."""
    saved_window(16384)
    assert max_ctx() == 16384
    assert shared_num_ctx(MODEL) == 16384


def test_the_environment_still_wins_over_a_saved_window(monkeypatch, saved_window):
    saved_window(8192)
    monkeypatch.setenv("SHAMSU_CHAT_MAX_CTX", "16384")
    assert max_ctx() == 16384
    assert shared_num_ctx(MODEL) == 16384


def test_no_setting_and_no_env_falls_back_to_the_default(saved_window):
    saved_window(None)
    assert chat_ctx_ceiling() == DEFAULT_CHAT_CTX


def test_a_model_is_never_asked_for_more_than_it_holds(saved_window):
    """The ceiling caps the ask; the model caps it again, and lower wins."""
    saved_window(32768)
    assert shared_num_ctx("smollm:1.7b") == 8192  # table says 8k for this family


@pytest.mark.parametrize("window", OFFERED_CTX_WINDOWS)
def test_every_offered_window_agrees_across_both_readers(saved_window, window):
    saved_window(window)
    assert max_ctx() == shared_num_ctx(MODEL) or shared_num_ctx(MODEL) == min(
        window, 32768
    )


# -- you cannot mistype a window --------------------------------------------


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("4k", 4096),
        ("8k", 8192),
        ("16k", 16384),
        ("32k", 32768),
        ("64k", 65536),
        ("32768", 32768),
        ("32,768", 32768),
        ("32_768", 32768),
        ("  16k  ", 16384),
    ],
)
def test_the_k_suffix_is_arithmetic_not_string_surgery(typed, expected):
    """`.replace("k", "024")` gave 4024 for 4k and 32024 for 32k."""
    assert parse_ctx_window(typed) == expected


@pytest.mark.parametrize("typed", ["abc", "", "   ", "16kb", "k", "1.5k", "-8192"])
def test_an_unreadable_window_is_rejected_rather_than_guessed(typed):
    assert parse_ctx_window(typed) is None


@pytest.mark.parametrize(
    ("asked", "snapped"),
    [
        (32786, 32768),  # THE typo, live on 2026-08-30
        (32024, 32768),  # what `/context window 32k` used to compute
        (32768, 32768),
        (30000, 32768),
        (12000, 8192),  # genuinely nearer 8192 (3808) than 16384 (4384)
        (14000, 16384),
        (5000, 4096),
        (999999, 32768),
        (1, 4096),
    ],
)
def test_a_near_miss_snaps_to_a_window_the_rest_of_the_system_uses(asked, snapped):
    assert snap_ctx_window(asked) == snapped


def test_snapping_only_ever_returns_an_offered_window():
    for asked in range(1, 70000, 337):
        assert snap_ctx_window(asked) in OFFERED_CTX_WINDOWS


def test_the_floor_is_the_one_settings_enforces():
    assert MIN_USABLE_CTX_WINDOW == min(OFFERED_CTX_WINDOWS)


# -- the windows are offered, not remembered --------------------------------


class _Document:
    def __init__(self, text: str) -> None:
        self.text_before_cursor = text


def _completions(text: str) -> list[str]:
    from shamsu.cli.repl import SlashCommandCompleter

    return [c.text for c in SlashCommandCompleter(None).get_completions(_Document(text), None)]


def test_every_window_is_offered_when_the_argument_is_empty():
    assert _completions("/context window ") == [str(w) for w in OFFERED_CTX_WINDOWS]


def test_a_partial_number_narrows_the_offer():
    assert _completions("/context window 16") == ["16384"]
    assert _completions("/context window 3") == ["32768"]


def test_the_k_form_is_offered_too():
    assert _completions("/context window 8k") == ["8k"]


def test_a_completion_says_what_the_window_costs():
    from shamsu.cli.repl import SlashCommandCompleter

    metas = [
        str(c.display_meta)
        for c in SlashCommandCompleter(None).get_completions(
            _Document("/context window "), None
        )
    ]
    assert any("reply reserve" in meta for meta in metas)


# -- budgets follow the window the session actually has ----------------------


@pytest.mark.parametrize("window", OFFERED_CTX_WINDOWS)
def test_one_tool_result_never_takes_most_of_the_window(window):
    """At 8k this used to return 8000 - 98% of the context."""
    budget = tool_result_budget(window)
    assert budget <= window // 2
    # And above the floor, it is a SHARE rather than a constant.
    if window > 8192:
        assert abs(budget / window - 0.25) < 0.02


@pytest.mark.parametrize("window", OFFERED_CTX_WINDOWS)
def test_the_summary_and_skeleton_budgets_are_shares_of_the_window(window):
    assert summary_budget(window) == window // 16
    assert int(window * SKELETON_BUDGET_RATIO) <= window // 16


def test_a_shrunken_session_re_derives_its_budgets():
    """`_shrink_for_oom` walks a session to 8k; the budgets must follow it."""
    assert tool_result_budget(32768) > tool_result_budget(8192)
    assert summary_budget(32768) > summary_budget(8192)
