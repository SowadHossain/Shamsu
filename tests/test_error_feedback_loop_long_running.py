from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.error_feedback_loop import LONG_RUNNING_MAX_ITERATIONS, ErrorFeedbackLoop
from shamsu.types import TestRunResult


class SequenceTestRunner:
    def __init__(self, results: list[TestRunResult]) -> None:
        self.results = results
        self.calls = 0

    def run(self, project_cwd: Path | str = ".") -> TestRunResult:
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


class _FixResult:
    def __init__(self, applied: bool = True) -> None:
        self.applied = applied
        self.changed_files = ["app/models.py"] if applied else []
        self.error = "" if applied else "Patch denied"


class FakeBugFixWorkflow:
    def __init__(self, applied: bool = True) -> None:
        self.applied = applied
        self.reports: list[str] = []

    async def run(self, report: str):
        self.reports.append(report)
        return _FixResult(self.applied)


class EmptySearch:
    def search(self, query: str, top_k: int = 5, boost_paths=None):
        return []

    def symbol_lookup(self, name: str):
        return []

    def fts_search(self, query: str, top_k: int = 5):
        return []


@pytest.mark.asyncio
async def test_long_running_mode_keeps_going_past_three_iterations_while_improving(tmp_path: Path):
    # Failure count strictly decreases each iteration: 5 -> 4 -> 3 -> 2 -> 1 -> 0.
    results = [TestRunResult(passed=0, failed=n) for n in (5, 4, 3, 2, 1, 0)]
    tests = SequenceTestRunner(results)
    bugfix = FakeBugFixWorkflow(applied=True)

    result = await ErrorFeedbackLoop(
        tmp_path, search=EmptySearch(), test_runner=tests, bugfix_workflow=bugfix,
        long_running=True,
    ).run(tmp_path)

    assert result.success is True
    assert len(result.iterations) == 5  # more than the old max_iterations=3 default
    assert LONG_RUNNING_MAX_ITERATIONS > 5


@pytest.mark.asyncio
async def test_long_running_mode_stops_early_when_stalled_instead_of_burning_the_ceiling(tmp_path: Path):
    # Failure count never improves: stays at 3 forever.
    results = [TestRunResult(passed=0, failed=3)]
    tests = SequenceTestRunner(results)
    bugfix = FakeBugFixWorkflow(applied=True)

    result = await ErrorFeedbackLoop(
        tmp_path, search=EmptySearch(), test_runner=tests, bugfix_workflow=bugfix,
        long_running=True,
    ).run(tmp_path)

    assert result.success is False
    assert "Stalled" in result.error
    # Stopped after the first non-improving iteration, not after burning through
    # all LONG_RUNNING_MAX_ITERATIONS attempts.
    assert len(result.iterations) == 1


@pytest.mark.asyncio
async def test_long_running_mode_stops_when_failures_increase(tmp_path: Path):
    results = [TestRunResult(passed=0, failed=2), TestRunResult(passed=0, failed=4)]
    tests = SequenceTestRunner(results)
    bugfix = FakeBugFixWorkflow(applied=True)

    result = await ErrorFeedbackLoop(
        tmp_path, search=EmptySearch(), test_runner=tests, bugfix_workflow=bugfix,
        long_running=True,
    ).run(tmp_path)

    assert result.success is False
    # Detected immediately after the first fix made things worse (4 > 2),
    # not after a second iteration.
    assert len(result.iterations) == 1


@pytest.mark.asyncio
async def test_default_mode_still_caps_at_three_iterations_even_when_improving(tmp_path: Path):
    """Confirms long_running=False (default) is unaffected by the stall
    check — it still stops at max_iterations regardless of progress."""
    results = [TestRunResult(passed=0, failed=n) for n in (5, 4, 3, 2, 1, 0)]
    tests = SequenceTestRunner(results)
    bugfix = FakeBugFixWorkflow(applied=True)

    result = await ErrorFeedbackLoop(
        tmp_path, search=EmptySearch(), test_runner=tests, bugfix_workflow=bugfix,
    ).run(tmp_path)

    assert result.success is False
    assert len(result.iterations) == 3
    assert "still failing after 3 fix attempts" in result.error
