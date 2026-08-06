"""Executable eval cases for medium/long PRD benchmark fixtures."""
from __future__ import annotations

import subprocess
from pathlib import Path

from evals.harness import CheckOutcome, EvalCase
from evals.prd_fixtures import (
    PRDArtifactExpectation,
    PRDBenchmarkFixture,
    PRD_BENCHMARK_FIXTURES,
    load_fixture_text,
)


SETUP_TIMEOUT_S = 180.0
ACCEPTANCE_TIMEOUT_S = 90.0


def make_prd_eval_case(fixture: PRDBenchmarkFixture) -> EvalCase:
    return EvalCase(
        name=f"prd_{fixture.name}",
        prompt=fixture.prompt,
        seed=lambda workspace: _seed_prd(workspace, fixture),
        check=lambda workspace, _final: check_prd_fixture(workspace, fixture),
        long_running=True,
        tags=("prd_benchmark", *fixture.tags),
    )


PRD_BENCHMARK_CASES: list[EvalCase] = [
    make_prd_eval_case(fixture) for fixture in PRD_BENCHMARK_FIXTURES
]


def check_prd_fixture(workspace: Path, fixture: PRDBenchmarkFixture) -> CheckOutcome:
    target = workspace / fixture.target_dir
    if not target.is_dir():
        return CheckOutcome(False, f"missing target folder `{fixture.target_dir}`")

    for expectation in fixture.required_artifacts:
        ok, note = _check_artifact(target, expectation)
        if not ok:
            return CheckOutcome(False, note)

    for command in fixture.setup_commands:
        ok, note = _run_command(
            target,
            command,
            expected_stdout=(),
            timeout_s=SETUP_TIMEOUT_S,
        )
        if not ok:
            return CheckOutcome(False, f"setup failed: {note}")

    for command in fixture.acceptance:
        expected_stdout = tuple(
            item
            for item in (command.expected_stdout, *command.expected_stdout_contains)
            if item
        )
        ok, note = _run_command(
            target,
            command.command,
            expected_stdout=expected_stdout,
            timeout_s=ACCEPTANCE_TIMEOUT_S,
        )
        if not ok:
            return CheckOutcome(False, note)
        for expectation in command.expected_artifacts:
            ok, note = _check_artifact(target, expectation)
            if not ok:
                return CheckOutcome(False, note)

    return CheckOutcome(True, "acceptance passed")


def _seed_prd(workspace: Path, fixture: PRDBenchmarkFixture) -> None:
    (workspace / fixture.prd_path.name).write_text(
        load_fixture_text(fixture),
        encoding="utf-8",
    )


def _run_command(
    cwd: Path,
    command: str,
    *,
    expected_stdout: tuple[str, ...],
    timeout_s: float,
) -> tuple[bool, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False, f"`{command}` timed out after {timeout_s:g}s"

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if completed.returncode != 0:
        return (
            False,
            f"`{command}` exit {completed.returncode}; stdout={_short(stdout)} stderr={_short(stderr)}",
        )

    for expected in expected_stdout:
        if expected not in stdout:
            return False, f"`{command}` missing stdout `{expected}`; got {_short(stdout)}"

    return True, f"`{command}` passed"


def _check_artifact(root: Path, expectation: PRDArtifactExpectation) -> tuple[bool, str]:
    try:
        path = _safe_child(root, expectation.path)
    except ValueError as exc:
        return False, str(exc)

    if expectation.kind == "directory":
        if not path.is_dir():
            return False, f"missing directory `{expectation.path}`"
    elif expectation.kind == "file":
        if not path.is_file():
            return False, f"missing file `{expectation.path}`"
    else:
        return False, f"unknown artifact kind `{expectation.kind}` for `{expectation.path}`"

    if expectation.contains:
        content = path.read_text(encoding="utf-8", errors="replace")
        if expectation.contains not in content:
            return False, f"`{expectation.path}` missing `{expectation.contains}`"

    return True, f"`{expectation.path}` exists"


def _safe_child(root: Path, rel: str) -> Path:
    candidate = (root / rel).resolve()
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"artifact path escapes target folder: `{rel}`") from exc
    return candidate


def _short(text: str, limit: int = 220) -> str:
    squashed = " ".join(text.split())
    if len(squashed) <= limit:
        return squashed
    return squashed[:limit] + "..."
