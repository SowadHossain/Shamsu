"""The read-loop guard slept through the run it exists for.

`ReadLoop` has a good escalation - nudge, insist, stop - and a real distinction
between a model opening a seven-file project and a model reading one file seven
times. On 2026-08-24 it fired none of it. Two turns burned the full 24 rounds
with 21 `read_file` calls between them and the guard armed the whole way.

The call site asked "was any tool called that is not a read?" and treated yes
as production. `contract_assert_pass` is not a read, so each of the ten in that
session reset the streak to zero and `READS_BEFORE_INSISTING = 8` was never
reached. A model marking its own homework looked like a model doing work.
"""
from __future__ import annotations

from shamsu.agents.loop_guards import (
    BOOKKEEPING_TOOLS,
    LOOKING_TOOLS,
    READ_LOOP_EXHAUSTED,
    READS_BEFORE_INSISTING,
    ReadLoopDetector,
)


def _produced(names: list[str]) -> bool:
    """Exactly the call site's rule, so the test pins the rule and not a copy."""
    return bool(set(names) - LOOKING_TOOLS - BOOKKEEPING_TOOLS)


def test_bookkeeping_is_neither_a_read_nor_production():
    assert not (BOOKKEEPING_TOOLS & LOOKING_TOOLS), "a contract call is not a read"
    for name in ("contract_assert_pass", "contract_assert_skip", "contract_status"):
        assert not _produced([name]), name


def test_real_work_still_counts_as_production():
    for name in ("run_command", "run_tests", "write_file", "patch_file"):
        assert _produced([name]), name


def test_reads_interleaved_with_self_assertions_still_reach_the_ceiling():
    """The exact shape of the failing run: read, mark it passed, repeat."""
    guard = ReadLoopDetector()
    signals = []
    for index in range(READS_BEFORE_INSISTING * 3):
        for names, targets in (
            (["read_file"], [f"js/module{index % 8}.js"]),
            (["contract_assert_pass"], []),
        ):
            signal = guard.record(names, _produced(names), targets=targets)
            if signal:
                signals.append(signal)

    assert signals, "the guard never spoke, which is the reported failure"


def test_a_genuine_edit_between_reads_still_clears_the_streak():
    """The guard is for a turn with nothing to show, not a careful one."""
    guard = ReadLoopDetector()
    for _ in range(READS_BEFORE_INSISTING * 2):
        guard.record(["read_file"], False, targets=["a.py"])
        guard.record(["write_file"], True, targets=[])

    assert guard.streak == 0
    assert not guard.insisted, "a model that reads then writes is working"


def test_circling_one_file_ends_the_turn():
    """Reading the same path over and over is the case that must stop."""
    guard = ReadLoopDetector()
    ended = None
    for _ in range(READS_BEFORE_INSISTING * 4):
        signal = guard.record(["read_file"], False, targets=["js/game.js"])
        if signal and signal.reason == READ_LOOP_EXHAUSTED:
            ended = signal
            break

    assert ended is not None, "circling one file has to end the turn"
    assert "stopped this turn" in ended.correction
