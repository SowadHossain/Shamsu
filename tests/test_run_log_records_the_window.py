"""A run must record how full the window was.

The absence of this cost an hour. Live 2026-08-31 two runs failed, and the run
log held 23 model calls, a `prompt_sha256`, an 800-character preview of a
39,563-character prompt - and no token counts at all. Ollama returns
`prompt_eval_count` and `eval_count` on every response and `_record_usage`
already read both; neither ever reached disk. "It fails at 16k" had to be
reconstructed by dividing `prompt_chars` by four.
"""
from __future__ import annotations

import json

import pytest

from shamsu.action_ledger.ledger import start_run


@pytest.fixture
def ledger(tmp_path):
    return start_run(tmp_path, "a task")


def _finished(ledger):
    rows = [
        json.loads(line)
        for line in ledger.model_calls_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [row for row in rows if row.get("phase") == "finished"]


def test_the_counts_reach_the_run_log(ledger):
    ledger.log_model_call_started("coder", "m:7b", messages=[], tools=[])
    ledger.log_model_call_finished(
        "coder", "m:7b", "answer", prompt_tokens=9890, completion_tokens=412,
        context_window=16384,
    )
    row = _finished(ledger)[-1]
    assert row["prompt_tokens"] == 9890
    assert row["completion_tokens"] == 412
    assert row["context_window"] == 16384


def test_how_full_the_window_was_is_computed_not_left_to_arithmetic(ledger):
    """The number anyone actually wants, so nobody has to divide by four."""
    ledger.log_model_call_started("coder", "m:7b", messages=[], tools=[])
    ledger.log_model_call_finished(
        "coder", "m:7b", "a", prompt_tokens=8192, completion_tokens=1,
        context_window=16384,
    )
    assert _finished(ledger)[-1]["window_used_pct"] == 50.0


def test_a_truncated_generation_says_so(ledger):
    """`done_reason: length` is how a cut-off write is told apart from a
    model that simply stopped."""
    ledger.log_model_call_started("coder", "m:7b", messages=[], tools=[])
    ledger.log_model_call_finished(
        "coder", "m:7b", "half a fi", done_reason="length",
        prompt_tokens=10, completion_tokens=4096, context_window=16384,
    )
    assert _finished(ledger)[-1]["done_reason"] == "length"


def test_a_server_that_reports_nothing_adds_no_empty_fields(ledger):
    """An older server, or a failed call. Absent is not zero."""
    ledger.log_model_call_started("coder", "m:7b", messages=[], tools=[])
    ledger.log_model_call_finished("coder", "m:7b", "answer")
    row = _finished(ledger)[-1]
    assert "prompt_tokens" not in row
    assert "window_used_pct" not in row
