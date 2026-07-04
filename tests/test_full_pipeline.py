from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.error_feedback_loop import ErrorFeedbackResult
from shamsu.agents.full_pipeline import FullDjangoPipeline
from shamsu.cli.repl import _parse_generate_prd_args
from shamsu.tools.django import DjangoSetupResult
from shamsu.types import TestRunResult as ShamsuTestRunResult


class EmptySearch:
    def search(self, query: str, top_k: int = 5, boost_paths=None):
        return []

    def symbol_lookup(self, name: str):
        return []

    def fts_search(self, query: str, top_k: int = 5):
        return []


class FakeSetupRunner:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[Path | str] = []

    def run(self, project_cwd: Path | str = ".") -> DjangoSetupResult:
        self.calls.append(project_cwd)
        failures = [] if self.ok else [_failure()]
        return DjangoSetupResult(project_cwd=Path(project_cwd), failures=failures)


class FakeTestRunner:
    def __init__(self, result: ShamsuTestRunResult) -> None:
        self.result = result
        self.calls: list[Path | str] = []

    def run(self, project_cwd: Path | str = ".") -> ShamsuTestRunResult:
        self.calls.append(project_cwd)
        return self.result


class FakeFeedbackLoop:
    def __init__(self, result: ErrorFeedbackResult) -> None:
        self.result = result
        self.calls: list[Path | str] = []

    async def run(self, project_cwd: Path | str = ".") -> ErrorFeedbackResult:
        self.calls.append(project_cwd)
        return self.result


@pytest.mark.asyncio
async def test_full_pipeline_writes_project_runs_setup_and_tests(tmp_path: Path):
    prd = _prd(tmp_path)
    target = tmp_path / "generated"
    setup = FakeSetupRunner()
    tests = FakeTestRunner(ShamsuTestRunResult(passed=2, failed=0, raw_output="OK"))

    result = await FullDjangoPipeline(
        tmp_path,
        search=EmptySearch(),
        approval_func=lambda _request: True,
        setup_runner=setup,
        test_runner=tests,
    ).run(prd, target)

    assert result.success is True
    assert result.project is not None
    assert (target / "manage.py").exists()
    assert (target / "app" / "tests.py").exists()
    assert setup.calls == [target.resolve()]
    assert tests.calls == [target.resolve()]


@pytest.mark.asyncio
async def test_full_pipeline_stops_when_setup_fails(tmp_path: Path):
    prd = _prd(tmp_path)

    result = await FullDjangoPipeline(
        tmp_path,
        search=EmptySearch(),
        approval_func=lambda _request: True,
        setup_runner=FakeSetupRunner(ok=False),
        test_runner=FakeTestRunner(ShamsuTestRunResult(passed=0, failed=0)),
    ).run(prd, "generated")

    assert result.success is False
    assert result.error == "Django setup failed."
    assert result.setup_result is not None
    assert result.test_result is None


@pytest.mark.asyncio
async def test_full_pipeline_runs_feedback_loop_when_tests_fail(tmp_path: Path):
    prd = _prd(tmp_path)
    failing = ShamsuTestRunResult(passed=1, failed=1, raw_output="FAILED")
    fixed = ShamsuTestRunResult(passed=2, failed=0, raw_output="OK")
    feedback = FakeFeedbackLoop(ErrorFeedbackResult(success=True, iterations=[], final_result=fixed))

    result = await FullDjangoPipeline(
        tmp_path,
        search=EmptySearch(),
        approval_func=lambda _request: True,
        setup_runner=FakeSetupRunner(),
        test_runner=FakeTestRunner(failing),
        feedback_loop=feedback,
    ).run(prd, "generated")

    assert result.success is True
    assert result.feedback_result is not None
    assert result.test_result == fixed
    assert feedback.calls == [(tmp_path / "generated").resolve()]


class RecoveringSetupRunner:
    """Fails the first call, succeeds on any subsequent call."""

    def __init__(self) -> None:
        self.calls: list[Path | str] = []

    def run(self, project_cwd: Path | str = ".") -> DjangoSetupResult:
        self.calls.append(project_cwd)
        failures = [_failure()] if len(self.calls) == 1 else []
        return DjangoSetupResult(project_cwd=Path(project_cwd), failures=failures)


class NeverRecoveringSetupRunner:
    def __init__(self) -> None:
        self.calls: list[Path | str] = []

    def run(self, project_cwd: Path | str = ".") -> DjangoSetupResult:
        self.calls.append(project_cwd)
        return DjangoSetupResult(project_cwd=Path(project_cwd), failures=[_failure()])


class FakeAppliedBugFixWorkflow:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def run(self, report: str):
        return type("FixResult", (), {"applied": True, "changed_files": ["app/models.py"], "error": ""})()


class FakeDeniedBugFixWorkflow:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def run(self, report: str):
        return type("FixResult", (), {"applied": False, "changed_files": [], "error": "could not fix it"})()


@pytest.mark.asyncio
async def test_long_running_mode_retries_setup_failure_via_bugfix_and_succeeds(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "shamsu.agents.full_pipeline.BugFixWorkflow", FakeAppliedBugFixWorkflow
    )
    prd = _prd(tmp_path)
    setup = RecoveringSetupRunner()
    tests = FakeTestRunner(ShamsuTestRunResult(passed=2, failed=0, raw_output="OK"))

    result = await FullDjangoPipeline(
        tmp_path,
        search=EmptySearch(),
        approval_func=lambda _request: True,
        setup_runner=setup,
        test_runner=tests,
        long_running=True,
    ).run(prd, "generated")

    assert result.success is True
    assert len(setup.calls) == 2  # original attempt + one retry after the bugfix


@pytest.mark.asyncio
async def test_long_running_mode_still_fails_when_bugfix_cannot_fix_setup(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "shamsu.agents.full_pipeline.BugFixWorkflow", FakeDeniedBugFixWorkflow
    )
    prd = _prd(tmp_path)
    setup = NeverRecoveringSetupRunner()

    result = await FullDjangoPipeline(
        tmp_path,
        search=EmptySearch(),
        approval_func=lambda _request: True,
        setup_runner=setup,
        test_runner=FakeTestRunner(ShamsuTestRunResult(passed=0, failed=0)),
        long_running=True,
    ).run(prd, "generated")

    assert result.success is False
    assert result.error == "Django setup failed."
    # bugfix.applied was False, so no retry run() call was made beyond the original.
    assert len(setup.calls) == 1


@pytest.mark.asyncio
async def test_default_mode_does_not_retry_setup_failure(tmp_path: Path, monkeypatch):
    """Confirms long_running=False (default) is unaffected: no bugfix retry attempted."""
    monkeypatch.setattr(
        "shamsu.agents.full_pipeline.BugFixWorkflow", FakeAppliedBugFixWorkflow
    )
    prd = _prd(tmp_path)
    setup = RecoveringSetupRunner()

    result = await FullDjangoPipeline(
        tmp_path,
        search=EmptySearch(),
        approval_func=lambda _request: True,
        setup_runner=setup,
        test_runner=FakeTestRunner(ShamsuTestRunResult(passed=0, failed=0)),
    ).run(prd, "generated")

    assert result.success is False
    assert result.error == "Django setup failed."
    assert len(setup.calls) == 1  # no retry attempted


def test_generate_prd_arg_parser_accepts_output():
    assert _parse_generate_prd_args('generate-prd "docs/todo prd.md" --output generated') == (
        "docs/todo prd.md",
        "generated",
    )


def _prd(root: Path) -> Path:
    path = root / "todo.md"
    path.write_text(
        "# Todo App\n\n"
        "## Entities\n"
        "- Task: title (text), done (boolean), user (FK to User)\n",
        encoding="utf-8",
    )
    return path


def _failure():
    from shamsu.tools.django import DjangoSetupFailure

    return DjangoSetupFailure(
        step="migrate",
        command="python manage.py migrate",
        cwd=Path("."),
        exit_code=1,
        stderr="boom",
    )
