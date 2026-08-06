from __future__ import annotations

from evals.__main__ import _select_cases
from evals.cases import SEED_CASES
from evals.prd_cases import PRD_BENCHMARK_CASES
from evals.prd_fixtures import PRD_BENCHMARK_FIXTURES, load_fixture_text
from shamsu.cli import repl


def test_prd_benchmark_fixtures_are_stable_and_machine_checkable():
    names = [fixture.name for fixture in PRD_BENCHMARK_FIXTURES]
    assert len(names) == len(set(names))
    assert {"medium", "long"} <= {fixture.size for fixture in PRD_BENCHMARK_FIXTURES}

    for fixture in PRD_BENCHMARK_FIXTURES:
        text = load_fixture_text(fixture)
        assert fixture.prd_path.is_file()
        assert fixture.target_dir
        assert fixture.prompt
        assert fixture.acceptance
        assert all(artifact.path for artifact in fixture.required_artifacts)
        extracted = repl._extract_prd_acceptance_commands(text)
        extracted_commands = {command for command, _expected in extracted}
        for acceptance in fixture.acceptance:
            assert acceptance.command in extracted_commands
            assert all(artifact.path for artifact in acceptance.expected_artifacts)


def test_prd_benchmark_expected_stdout_matches_prd_text():
    for fixture in PRD_BENCHMARK_FIXTURES:
        extracted = dict(repl._extract_prd_acceptance_commands(load_fixture_text(fixture)))
        for acceptance in fixture.acceptance:
            if acceptance.expected_stdout:
                assert extracted[acceptance.command] == acceptance.expected_stdout


def test_eval_cli_keeps_prd_benchmarks_opt_in():
    default_names = {case.name for case in _select_cases(include_prd=False, prd_only=False)}
    with_prd_names = {case.name for case in _select_cases(include_prd=True, prd_only=False)}
    prd_only_names = {case.name for case in _select_cases(include_prd=False, prd_only=True)}

    assert default_names == {case.name for case in SEED_CASES}
    assert prd_only_names == {case.name for case in PRD_BENCHMARK_CASES}
    assert default_names < with_prd_names
