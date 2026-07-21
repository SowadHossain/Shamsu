"""Tests for the eval harness framework (evals/harness.py) using a fake driver,
so the runner/scoring/reporting is provable without Ollama. The seed cases
themselves are exercised live via `python -m evals`."""
from __future__ import annotations

from pathlib import Path

import pytest

from evals.cases import SEED_CASES
from evals.harness import EvalCase, EvalReport, EvalResult, render_report, run_evals


def _passing_case(name: str = "ok") -> EvalCase:
    return EvalCase(
        name=name,
        prompt="do the thing",
        seed=lambda ws: (ws / "seed.txt").write_text("seeded", encoding="utf-8"),
        check=lambda ws, final: (ws / "made.txt").is_file() and "done" in final,
    )


async def _driver_that_writes(workspace: Path, case: EvalCase) -> str:
    # A faithful fake: honors the seed, produces the artifact the check wants.
    assert (workspace / "seed.txt").is_file(), "seed should run before the driver"
    (workspace / "made.txt").write_text("x", encoding="utf-8")
    return "done"


async def _driver_that_noops(workspace: Path, case: EvalCase) -> str:
    return "nothing happened"


async def _driver_that_raises(workspace: Path, case: EvalCase) -> str:
    raise RuntimeError("model exploded")


@pytest.mark.asyncio
async def test_run_evals_scores_pass(tmp_path: Path):
    report = await run_evals([_passing_case()], driver=_driver_that_writes, tier="default")
    assert report.total == 1
    assert report.passed == 1
    assert report.pass_rate == 1.0
    assert report.results[0].status == "PASS"


@pytest.mark.asyncio
async def test_run_evals_scores_fail_on_check():
    report = await run_evals([_passing_case()], driver=_driver_that_noops)
    assert report.passed == 0
    assert report.results[0].status == "FAIL"
    assert report.results[0].error == ""


@pytest.mark.asyncio
async def test_run_evals_records_driver_exception_as_error():
    report = await run_evals([_passing_case()], driver=_driver_that_raises)
    result = report.results[0]
    assert result.passed is False
    assert result.status == "ERROR"
    assert "model exploded" in result.error


@pytest.mark.asyncio
async def test_each_case_gets_an_isolated_workspace():
    seen: list[Path] = []

    async def _driver(workspace: Path, case: EvalCase) -> str:
        seen.append(workspace)
        return "done"

    cases = [_passing_case("a"), _passing_case("b")]
    await run_evals(cases, driver=_driver)
    assert len(seen) == 2
    assert seen[0] != seen[1]  # distinct temp dirs, no cross-contamination


@pytest.mark.asyncio
async def test_check_receives_final_text():
    got: dict[str, str] = {}

    async def _driver(workspace: Path, case: EvalCase) -> str:
        return "the answer is 42"

    def _check(workspace: Path, final: str) -> bool:
        got["final"] = final
        return "42" in final

    report = await run_evals(
        [EvalCase(name="qa", prompt="q", check=_check)], driver=_driver
    )
    assert report.passed == 1
    assert got["final"] == "the answer is 42"


def test_render_report_contains_rate_and_cases():
    report = EvalReport(
        results=[
            EvalResult(name="alpha", passed=True, duration_s=1.2),
            EvalResult(name="beta", passed=False, duration_s=3.4),
            EvalResult(name="gamma", passed=False, error="TimeoutError: x", duration_s=5.0),
        ],
        tier="default",
    )
    text = render_report(report)
    assert "1/3 (33%)" in text
    assert "alpha" in text and "PASS" in text
    assert "beta" in text and "FAIL" in text
    assert "gamma" in text and "ERROR" in text
    assert "Tier:** default" in text


def test_seed_cases_are_well_formed():
    names = [c.name for c in SEED_CASES]
    assert len(names) == len(set(names)), "case names must be unique"
    assert len(SEED_CASES) >= 6
    for case in SEED_CASES:
        assert case.prompt.strip()
        assert callable(case.check)


# ---------------------------------------------------------------------------
# Per-case driver (F1): planning never goes through the agent loop, so the
# default driver cannot score it. Cases may name their own.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_case_driver_is_used_when_no_run_driver_is_given():
    async def _case_driver(workspace: Path, case: EvalCase) -> str:
        return "from the case driver"

    case = EvalCase(
        name="custom",
        prompt="p",
        check=lambda _w, final: final == "from the case driver",
        driver=_case_driver,
    )
    report = await run_evals([case])
    assert report.passed == 1


@pytest.mark.asyncio
async def test_explicit_run_driver_overrides_a_case_driver():
    """A fake driver passed to run_evals must win, so unit tests never reach
    Ollama just because a case declares a live driver."""

    async def _case_driver(workspace: Path, case: EvalCase) -> str:
        raise AssertionError("the case driver must not run")

    async def _test_driver(workspace: Path, case: EvalCase) -> str:
        return "fake"

    case = EvalCase(
        name="custom",
        prompt="p",
        check=lambda _w, final: final == "fake",
        driver=_case_driver,
    )
    report = await run_evals([case], driver=_test_driver)
    assert report.passed == 1


def test_plan_grounding_check_rejects_a_hallucinated_file(tmp_path: Path):
    """The check that protects C1: a plan naming a file that doesn't exist is a
    hallucination the coder would inherit as trusted context."""
    from evals.cases import _check_plan_references_only_real_files

    (tmp_path / "game.js").write_text("// real", encoding="utf-8")

    real = "## Steps\n1. Edit game.js to add a pause flag.\n"
    assert _check_plan_references_only_real_files(tmp_path, real) is True

    hallucinated = "## Steps\n1. Edit src/engine/pause.js to add a pause flag.\n"
    assert _check_plan_references_only_real_files(tmp_path, hallucinated) is False

    # Proposing a NEW file is legitimate, not a hallucination.
    creating = "## Steps\n1. Create pause.js with the pause handler.\n"
    assert _check_plan_references_only_real_files(tmp_path, creating) is True

    assert _check_plan_references_only_real_files(tmp_path, "") is False


def test_destructive_ask_check_requires_no_action_taken(tmp_path: Path):
    """Asking is only correct if it ALSO didn't delete anything meanwhile."""
    from evals.cases import _check_asks_before_destructive_guess

    data = tmp_path / "data"
    data.mkdir()
    (data / "users.db").write_text("real", encoding="utf-8")
    (data / "users.db.bak").write_text("backup", encoding="utf-8")

    assert _check_asks_before_destructive_guess(tmp_path, "Which one should I delete?") is True
    assert _check_asks_before_destructive_guess(tmp_path, "Deleted it.") is False

    (data / "users.db").unlink()
    assert _check_asks_before_destructive_guess(tmp_path, "Which one should I delete?") is False


# ---------------------------------------------------------------------------
# Gap I3: single-sample evals can't tell a regression from a coin flip.
# Measured on real runs: the same unchanged commit gave bugfix_syntax_error
# PASS / FAIL / PASS. --samples scores by majority and flags flaky cases.
# ---------------------------------------------------------------------------


def _scripted_driver(sequence: list[bool]):
    """A driver that passes/fails according to a fixed script."""
    calls = {"n": 0}

    async def _driver(workspace: Path, case: EvalCase) -> str:
        index = calls["n"]
        calls["n"] += 1
        return "good" if sequence[index % len(sequence)] else "bad"

    return _driver


def _check_good(_workspace: Path, final: str) -> bool:
    return final == "good"


@pytest.mark.asyncio
async def test_samples_runs_each_case_n_times():
    seen = {"n": 0}

    async def _driver(workspace: Path, case: EvalCase) -> str:
        seen["n"] += 1
        return "good"

    case = EvalCase(name="c", prompt="p", check=_check_good)
    report = await run_evals([case], driver=_driver, samples=3)

    assert seen["n"] == 3
    assert report.results[0].passes == 3
    assert report.results[0].runs == 3


@pytest.mark.asyncio
async def test_majority_pass_survives_one_unlucky_roll():
    """2/3 is a pass: one bad sample must not condemn a good change."""
    case = EvalCase(name="c", prompt="p", check=_check_good)
    report = await run_evals([case], driver=_scripted_driver([True, False, True]), samples=3)

    result = report.results[0]
    assert result.passed is True
    assert result.passes == 2
    assert result.status == "PASS 2/3"


@pytest.mark.asyncio
async def test_majority_fail_is_not_rescued_by_one_lucky_roll():
    case = EvalCase(name="c", prompt="p", check=_check_good)
    report = await run_evals([case], driver=_scripted_driver([False, True, False]), samples=3)

    result = report.results[0]
    assert result.passed is False
    assert result.status == "FAIL 1/3"


@pytest.mark.asyncio
async def test_a_flaky_case_is_called_out_in_the_report():
    """The single most useful thing the harness can say: this number is not
    trustworthy. A silent 2/3 reads like a solid pass."""
    case = EvalCase(name="wobbly", prompt="p", check=_check_good)
    report = await run_evals([case], driver=_scripted_driver([True, False, True]), samples=3)

    assert report.results[0].flaky is True
    rendered = report.render()
    assert "FLAKY" in rendered
    assert "wobbly" in rendered
    assert "do not read a delta from them" in rendered


@pytest.mark.asyncio
async def test_consistent_results_are_not_flagged_flaky():
    case = EvalCase(name="steady", prompt="p", check=_check_good)
    report = await run_evals([case], driver=_scripted_driver([True]), samples=3)

    assert report.results[0].flaky is False
    assert "FLAKY" not in report.render()


@pytest.mark.asyncio
async def test_single_sample_keeps_the_old_boolean_behavior():
    case = EvalCase(name="c", prompt="p", check=_check_good)
    report = await run_evals([case], driver=_scripted_driver([True]), samples=1)

    result = report.results[0]
    assert result.status == "PASS"          # no "1/1" noise
    assert result.passes == 1 and result.runs == 1
    # ...and the report warns that one sample cannot resolve a delta.
    assert "single-sample" in report.render()


@pytest.mark.asyncio
async def test_a_flaky_case_reports_the_failing_attempt_not_the_passing_one():
    """When a case wobbles, the failure is the interesting half."""
    case = EvalCase(name="c", prompt="p", check=_check_good)
    report = await run_evals([case], driver=_scripted_driver([True, False, True]), samples=3)

    assert report.results[0].final == "bad"


def test_entrypoint_activates_the_tier_from_the_env(monkeypatch):
    """`python -m evals` must honor SHAMSU_MODEL_TIER (gap I3). The original
    entrypoint only READ active_tier() - a module global nothing in the eval
    process had initialized - so a "light baseline" silently ran (and was
    labeled) default. _resolve_tier must initialize, not just report."""
    import shamsu.runtime.models as models
    from evals.__main__ import _resolve_tier

    # setattr-to-current-value registers teardown restoration of the global
    # that _resolve_tier is about to mutate.
    monkeypatch.setattr(models, "_ACTIVE_TIER", models._ACTIVE_TIER)
    monkeypatch.setenv("SHAMSU_MODEL_TIER", "light")

    assert _resolve_tier() == "light"
    assert models.active_tier().value == "light"
