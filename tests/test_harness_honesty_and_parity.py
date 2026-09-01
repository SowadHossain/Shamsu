"""Things the harness knew and never said, and things one surface did alone.

Two groups, from the 2026-08-31 audit.

**Honesty.** A turn was reported as `done in 8m12s - 4 files changed` while its
only contract assertion sat `pending` from the first minute, the loop had
steered the model ten times (five on one stuck file), and seventeen tool
payloads had been thrown out of the window to make room. All three were
measured; none reached the one line anyone reads.

**Parity.** Three surfaces built the same loop three different ways. Telegram
skipped `make_approval_func` entirely, so it asked the broker about every file
write - a 900s card each - where the CLI auto-approves what the sandbox has
already fenced, and it never consulted `get_approval_override()`. The web runner
passed no `ActionLedger`, so a turn started in the browser left no mutation
journal, no `/undo`, and nothing in `/runs`.
"""
from __future__ import annotations

import inspect

import pytest

from shamsu.agents.simple_chat import _WIRING_SUFFIXES, _turn_verdict


# -- the verdict says what the turn cost -------------------------------------


def test_a_plain_turn_stays_short():
    """The counters must not turn a question-and-answer turn into a report."""
    assert _turn_verdict(12, [], stopped=False) == "done in 12s"


def test_an_unfinished_contract_is_named():
    line = _turn_verdict(
        492, ["a.js"], stopped=False, contract="4 of 9 checks outstanding"
    )
    assert "4 of 9 checks outstanding" in line


def test_the_number_of_corrections_is_named():
    line = _turn_verdict(492, ["a.js"], stopped=False, corrections=5)
    assert "steered 5 times" in line
    assert "steered 1 time" in _turn_verdict(10, [], stopped=False, corrections=1)


def test_context_trimming_is_named():
    assert "context trimmed 17x" in _turn_verdict(492, [], stopped=False, elisions=17)


def test_the_voice_demo_turn_would_now_read_honestly():
    """What that session actually reported, against what it knew."""
    line = _turn_verdict(
        1300,
        [],
        stopped=True,
        failures=26,
        contract="1 of 1 checks outstanding",
        corrections=10,
        elisions=17,
    )
    for expected in ("stopped after", "26 tool calls failed", "1 of 1 checks outstanding",
                     "steered 10 times", "context trimmed 17x"):
        assert expected in line


# -- cross-file checks reach every language the verifier handles --------------


@pytest.mark.parametrize("suffix", [".py", ".ts", ".tsx", ".jsx", ".vue", ".svelte", ".sql", ".prisma"])
def test_the_gate_covers_what_the_verifier_can_answer(suffix):
    """It was `{".html", ".htm", ".js"}` while the verifier had handled all of
    these for months, so a Python project got syntax checks and nothing else."""
    assert suffix in _WIRING_SUFFIXES


def test_the_gate_is_read_from_the_verifier_not_restated():
    from shamsu.verify.wiring import _SOURCE_SUFFIXES

    assert _WIRING_SUFFIXES == frozenset(_SOURCE_SUFFIXES)


# -- three surfaces, one wiring ----------------------------------------------


def _source(func) -> str:
    return inspect.getsource(func)


def test_the_web_runner_passes_a_ledger():
    """A browser turn left no mutation journal, so `/undo` had nothing to undo."""
    import shamsu.control.runner as runner

    body = inspect.getsource(runner)
    assert "start_run(" in body
    assert "action_ledger=ledger" in body
    # And the loop gets it too, not only the tool registry.
    assert body.count("action_ledger=ledger") >= 2


def test_telegram_builds_its_tools_through_the_shared_wiring():
    """Raw, it asked the broker about every file write - 900s a card - while the
    CLI auto-approves what the sandbox already fenced."""
    import shamsu.integrations.telegram.sessions as sessions

    body = inspect.getsource(sessions)
    simple_at = body.index("if simple_mode_enabled():")
    legacy_at = body.index("else:", simple_at)
    simple_branch = body[simple_at:legacy_at]
    assert "build_simple_tools(" in simple_branch


def test_the_legacy_telegram_path_keeps_its_own_registry():
    """`make_approval_func` encodes SIMPLE mode's policy; applying it to a loop
    with different tools would be a change nobody asked for."""
    import shamsu.integrations.telegram.sessions as sessions

    body = inspect.getsource(sessions)
    assert "AgentToolRegistry(" in body


# -- sampling is decided here, not by whatever the server defaults to ---------


def test_the_sampling_parameters_are_pinned():
    from shamsu.agents.simple_chat import REPEAT_PENALTY, TOP_K, TOP_P

    # 1.0 is OFF. The tokens a repetition penalty punishes in prose are the
    # tokens source code is made of - indentation, `self.`, a closing brace.
    assert REPEAT_PENALTY == 1.0
    assert 0 < TOP_P <= 1
    assert TOP_K > 0


def test_every_pinned_parameter_reaches_the_request():
    body = inspect.getsource(
        __import__("shamsu.agents.simple_chat", fromlist=["x"]).SimpleChatLoop._call_model
    )
    for key in ("repeat_penalty", "top_p", "top_k", "num_ctx", "num_predict", "num_keep"):
        assert f'"{key}"' in body, key
