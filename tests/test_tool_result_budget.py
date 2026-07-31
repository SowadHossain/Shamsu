"""Tests for per-tool-result token budgeting (G12): a single big read/grep result
is capped BEFORE it enters chat history so it can't blow the window mid-loop."""
from __future__ import annotations

from shamsu.agents.chat_loop import _budget_tool_result_json, _budget_tool_result_json_with_meta
from shamsu.context.budget import count_tokens


def test_small_result_passes_through_unchanged():
    text = '{"ok": true, "message": "Read file.", "data": {"content": "print(1)"}}'
    assert _budget_tool_result_json(text, 2000) == text


def test_large_result_is_capped_under_budget():
    big = '{"ok": true, "data": {"content": "' + ("token here " * 6000) + '"}}'
    assert count_tokens(big) > 500
    out = _budget_tool_result_json(big, 500)
    assert count_tokens(out) <= 500


def test_truncation_adds_a_narrow_scope_hint():
    big = "x y z " * 8000
    out = _budget_tool_result_json(big, 300)
    assert "truncated to fit" in out
    assert "read_file with start_line/end_line" in out


def test_zero_or_negative_budget_disables_capping():
    big = "x y z " * 5000
    assert _budget_tool_result_json(big, 0) == big
    assert _budget_tool_result_json(big, -5) == big


def test_budget_keeps_leading_content():
    big = "IMPORTANT_HEADER " + ("filler " * 5000)
    out = _budget_tool_result_json(big, 200)
    assert out.startswith("IMPORTANT_HEADER")


def test_budget_metadata_records_truncation_and_token_counts():
    big = "IMPORTANT_HEADER " + ("filler " * 5000)
    out, meta = _budget_tool_result_json_with_meta(big, 200)

    assert out.startswith("IMPORTANT_HEADER")
    assert meta["original_tokens"] > meta["returned_tokens"]
    assert meta["returned_tokens"] <= 200
    assert meta["max_tokens"] == 200
    assert meta["truncated"] is True
