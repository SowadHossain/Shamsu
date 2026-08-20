"""The eval diff: a mechanical verdict on whether a change helped.

Each test names the judgement, not the mechanism. The point of this tool is
that §31.1 scored 1/7 to 5/7 across nine runs of identical code - so the tests
that matter most are the ones proving it REFUSES to call noise a result.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.diff import (
    EXIT_IMPROVED,
    EXIT_NOISE,
    EXIT_REGRESSED,
    EXIT_USAGE,
    IMPROVED,
    NOISE,
    REGRESSED,
    DiffError,
    compare,
    load_run,
    main,
)


def _report(tmp_path: Path, name: str, cases: dict[str, tuple[int, int]]) -> str:
    """Write a harness-shaped JSON report. `cases` maps name -> (passes, runs)."""
    payload = {
        "tier": "default",
        "total": len(cases),
        "passed": sum(1 for p, r in cases.values() if p == r),
        "results": [
            {
                "name": case,
                "passed": passes == runs,
                "passes": passes,
                "runs": runs,
                "duration_s": 10.0,
                "tags": [],
            }
            for case, (passes, runs) in cases.items()
        ],
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


# --- the verdict --------------------------------------------------------


def test_a_case_that_starts_passing_is_an_improvement(tmp_path):
    base = _report(tmp_path, "base.json", {"a": (0, 3), "b": (3, 3)})
    feat = _report(tmp_path, "feat.json", {"a": (3, 3), "b": (3, 3)})

    *_, result = compare(base, feat)

    assert result == IMPROVED


def test_a_case_that_stops_passing_is_a_regression(tmp_path):
    base = _report(tmp_path, "base.json", {"a": (3, 3), "b": (3, 3)})
    feat = _report(tmp_path, "feat.json", {"a": (0, 3), "b": (3, 3)})

    *_, result = compare(base, feat)

    assert result == REGRESSED


def test_nothing_moving_is_noise_not_success(tmp_path):
    base = _report(tmp_path, "base.json", {"a": (3, 3), "b": (0, 3)})
    feat = _report(tmp_path, "feat.json", {"a": (3, 3), "b": (0, 3)})

    _b, _f, _m, delta, result = compare(base, feat)

    assert result == NOISE
    assert delta == 0.0


def test_a_solid_case_breaking_outranks_a_better_average(tmp_path):
    """The override. A change that lifts three cases while destroying one that
    used to pass every single attempt is not an improvement, however the mean
    reads."""
    base = _report(
        tmp_path, "base.json", {"a": (3, 3), "b": (0, 3), "c": (0, 3), "d": (0, 3)}
    )
    feat = _report(
        tmp_path, "feat.json", {"a": (0, 3), "b": (3, 3), "c": (3, 3), "d": (3, 3)}
    )

    _b, _f, moves, delta, result = compare(base, feat)

    assert delta > 0, "the average really did rise"
    assert moves.hard_regressions == ["a"]
    assert result == REGRESSED


# --- the SHAMSU-specific half: samples and flakiness ---------------------


def test_a_movement_inside_the_flaky_set_is_not_a_delta(tmp_path):
    """BENCHMARK.md has said this in prose for months and nothing enforced it.
    1/3 -> 2/3 is two numbers from inside one case's own noise."""
    base = _report(tmp_path, "base.json", {"a": (1, 3), "b": (3, 3)})
    feat = _report(tmp_path, "feat.json", {"a": (2, 3), "b": (3, 3)})

    _b, _f, moves, delta, result = compare(base, feat)

    assert [n for n, _x, _y in moves.unreliable] == ["a"]
    assert moves.recovered == []
    assert delta == 0.0, "a flaky case must not contribute to the delta"
    assert result == NOISE


def test_a_flaky_case_going_solid_is_still_not_counted(tmp_path):
    """Even the flattering direction. 2/3 -> 3/3 may be the fix or may be the
    same coin landing differently; one run cannot tell them apart."""
    base = _report(tmp_path, "base.json", {"a": (2, 3), "b": (3, 3)})
    feat = _report(tmp_path, "feat.json", {"a": (3, 3), "b": (3, 3)})

    _b, _f, moves, delta, result = compare(base, feat)

    assert [n for n, _x, _y in moves.unreliable] == ["a"]
    assert delta == 0.0
    assert result == NOISE


def test_a_partial_rate_change_between_solid_states_does_count(tmp_path):
    """The other edge: 0/3 -> 3/3 is not flaky at either end, and must count -
    or the flaky rule would swallow every real result too."""
    base = _report(tmp_path, "base.json", {"a": (0, 3)})
    feat = _report(tmp_path, "feat.json", {"a": (3, 3)})

    _b, _f, moves, delta, result = compare(base, feat)

    assert [n for n, _x, _y in moves.recovered] == ["a"]
    assert delta == pytest.approx(1.0)
    assert result == IMPROVED


def test_unequal_sample_counts_refuse_to_compare(tmp_path):
    """Comparing 1 attempt against 3 compares a coin flip to a measurement.
    The harness footer asks for equal --samples; footers get scrolled past."""
    base = _report(tmp_path, "base.json", {"a": (1, 1)})
    feat = _report(tmp_path, "feat.json", {"a": (3, 3)})

    with pytest.raises(DiffError) as caught:
        compare(base, feat)

    assert "sample" in str(caught.value).lower()


def test_a_case_only_one_run_has_is_reported_not_counted(tmp_path):
    base = _report(tmp_path, "base.json", {"a": (3, 3), "gone": (3, 3)})
    feat = _report(tmp_path, "feat.json", {"a": (3, 3), "brand_new": (0, 3)})

    _b, _f, moves, delta, result = compare(base, feat)

    assert moves.added == ["brand_new"]
    assert moves.removed == ["gone"]
    assert delta == 0.0, "a case with nothing to compare against cannot move the delta"
    assert result == NOISE


# --- IO and the command line --------------------------------------------


def test_a_directory_argument_takes_its_newest_report(tmp_path):
    folder = tmp_path / "runs"
    folder.mkdir()
    old = Path(_report(folder, "old.json", {"a": (0, 3)}))
    new = Path(_report(folder, "new.json", {"a": (3, 3)}))
    import os, time

    os.utime(old, (time.time() - 500, time.time() - 500))

    assert load_run(str(folder)).path == str(new)


def test_a_file_that_is_not_a_harness_report_is_a_usage_error(tmp_path):
    junk = tmp_path / "junk.json"
    junk.write_text('{"hello": "world"}', encoding="utf-8")
    base = _report(tmp_path, "base.json", {"a": (3, 3)})

    assert main([base, str(junk)]) == EXIT_USAGE


def test_a_missing_file_is_a_usage_error_not_a_regression(tmp_path):
    """Exit 1 means "your change broke something". An unreadable path must
    never be reported as that."""
    base = _report(tmp_path, "base.json", {"a": (3, 3)})

    assert main([base, str(tmp_path / "nope.json")]) == EXIT_USAGE


def test_the_exit_codes_are_the_contract(tmp_path, capsys):
    base = _report(tmp_path, "base.json", {"a": (0, 3), "b": (3, 3)})
    up = _report(tmp_path, "up.json", {"a": (3, 3), "b": (3, 3)})
    down = _report(tmp_path, "down.json", {"a": (0, 3), "b": (0, 3)})
    same = _report(tmp_path, "same.json", {"a": (0, 3), "b": (3, 3)})

    assert main([base, up]) == EXIT_IMPROVED
    assert main([base, down]) == EXIT_REGRESSED
    assert main([base, same]) == EXIT_NOISE


def test_the_json_output_carries_the_same_verdict_as_the_exit_code(tmp_path, capsys):
    base = _report(tmp_path, "base.json", {"a": (0, 3)})
    feat = _report(tmp_path, "feat.json", {"a": (3, 3)})

    code = main([base, feat, "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["verdict"] == IMPROVED
    assert payload["exit_code"] == code == EXIT_IMPROVED
    assert payload["recovered"] == [{"name": "a", "before": 0.0, "after": 1.0}]


def test_the_report_names_the_flaky_cases_it_declined_to_count(tmp_path, capsys):
    base = _report(tmp_path, "base.json", {"steady": (3, 3), "coinflip": (1, 3)})
    feat = _report(tmp_path, "feat.json", {"steady": (3, 3), "coinflip": (2, 3)})

    main([base, feat])
    out = capsys.readouterr().out

    assert "MOVED BUT FLAKY" in out
    assert "coinflip" in out
    assert "VERDICT  : NOISE" in out


def test_an_old_report_without_sample_counts_still_compares(tmp_path):
    """`passes`/`runs` post-date the harness. A report written before them
    collapses to the boolean, which is what they mean at runs=1 anyway."""
    old = tmp_path / "old.json"
    old.write_text(
        json.dumps({"results": [{"name": "a", "passed": False}, {"name": "b", "passed": True}]}),
        encoding="utf-8",
    )
    new = tmp_path / "new.json"
    new.write_text(
        json.dumps({"results": [{"name": "a", "passed": True}, {"name": "b", "passed": True}]}),
        encoding="utf-8",
    )

    *_, result = compare(str(old), str(new))

    assert result == IMPROVED


# --- the flaky rule, sharpened ------------------------------------------


def test_a_movement_no_bigger_than_the_noise_is_still_withheld(tmp_path):
    """4/7 -> 7/7 looks like a fix and is not distinguishable from luck at seven
    samples (Fisher p~0.19). This is the real case from 2026-08-20."""
    base = _report(tmp_path, "base.json", {"asking": (4, 7)})
    feat = _report(tmp_path, "feat.json", {"asking": (7, 7)})

    _b, _f, moves, delta, result = compare(base, feat)

    assert [n for n, _x, _y in moves.unreliable] == ["asking"]
    assert delta == 0.0
    assert result == NOISE


def test_a_flaky_case_that_becomes_solidly_fixed_IS_counted(tmp_path):
    """The limitation the flat exclusion had: a case flaky BEFORE could never be
    shown to have been FIXED, however solid it became. With enough samples the
    movement stops being explicable as the same coin."""
    base = _report(tmp_path, "base.json", {"asking": (1, 20)})
    feat = _report(tmp_path, "feat.json", {"asking": (20, 20)})

    _b, _f, moves, delta, result = compare(base, feat)

    assert [n for n, _x, _y in moves.recovered] == ["asking"]
    assert moves.unreliable == []
    assert result == IMPROVED


def test_a_real_collapse_out_of_a_flaky_state_is_a_regression(tmp_path):
    """Judged as strictly in the other direction: a guard that quietly destroys
    a case must not hide behind "it was flaky anyway"."""
    base = _report(tmp_path, "base.json", {"asking": (19, 20)})
    feat = _report(tmp_path, "feat.json", {"asking": (0, 20)})

    _b, _f, moves, _delta, result = compare(base, feat)

    assert [n for n, _x, _y in moves.regressed] == ["asking"]
    assert result == REGRESSED


def test_a_small_wobble_at_large_sample_counts_is_still_noise(tmp_path):
    base = _report(tmp_path, "base.json", {"asking": (10, 20)})
    feat = _report(tmp_path, "feat.json", {"asking": (12, 20)})

    *_, result = compare(base, feat)

    assert result == NOISE
