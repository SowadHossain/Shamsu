from __future__ import annotations

import ast
from pathlib import Path

import pytest

from shamsu.agents.error_feedback_loop import ErrorFeedbackLoop
from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.templates.django.frontend import (
    render_dashboard,
    render_django_test_files,
    render_frontend_django_files,
)
from shamsu.templates.django.writer import DjangoProjectWriter
from shamsu.tools.django import DJANGO_TEST_COMMAND, DjangoTestRunner, parse_django_test_output
from shamsu.types import TestRunResult as ShamsuTestRunResult


class FakeCommandRunner:
    def __init__(self, result: tuple[int, str, str]) -> None:
        self.result = result
        self.calls: list[tuple[str, Path]] = []

    def run(self, command: str, cwd: Path) -> tuple[int, str, str]:
        self.calls.append((command, cwd))
        return self.result

    def run_tests(self, cwd: Path) -> ShamsuTestRunResult:
        raise NotImplementedError


class SequenceTestRunner:
    def __init__(self, results: list[ShamsuTestRunResult]) -> None:
        self.results = results
        self.calls = 0

    def run(self, project_cwd: Path | str = ".") -> ShamsuTestRunResult:
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


class FakeBugFixWorkflow:
    def __init__(self, applied: bool = True) -> None:
        self.applied = applied
        self.reports: list[str] = []

    async def run(self, report: str):
        self.reports.append(report)
        return _FixResult(self.applied)


class _FixResult:
    def __init__(self, applied: bool) -> None:
        self.applied = applied
        self.changed_files = ["app/models.py"] if applied else []
        self.error = "" if applied else "Patch denied"


class EmptySearch:
    def search(self, query: str, top_k: int = 5):
        return []

    def symbol_lookup(self, name: str):
        return []

    def fts_search(self, query: str, top_k: int = 5):
        return []


def _spec(text: str = ""):
    prd = text or "# Todo App\n\n## Entities\n- Task: title (text), done (boolean), user (FK to User)\n"
    return build_project_spec(parse_prd_text(prd, fallback_title="PRD", markdown=True))


def test_django_test_runner_runs_manage_py_test_and_parses_ok(tmp_path: Path):
    project = _django_project(tmp_path)
    runner = FakeCommandRunner((0, "Ran 3 tests in 0.01s\n\nOK\n", ""))

    result = DjangoTestRunner(tmp_path, command_runner=runner).run(project)

    assert runner.calls == [(DJANGO_TEST_COMMAND, project)]
    assert result.passed == 3
    assert result.failed == 0


def test_django_test_output_parser_handles_failures_errors_and_redaction():
    output = (
        "FAIL: test_create (app.tests.TaskApiTests.test_create)\n"
        "Traceback (most recent call last):\n"
        '  File "app/tests.py", line 42, in test_create\n'
        "AssertionError: 400 != 201\n\n"
        "ERROR: test_list (app.tests.TaskApiTests.test_list)\n"
        "Traceback (most recent call last):\n"
        '  File "app/views.py", line 12, in task_list\n'
        "ValueError: SECRET_KEY = 'django-insecure-secret'\n\n"
        "Ran 5 tests in 0.02s\n\n"
        "FAILED (failures=1, errors=1)\n"
    )

    result = parse_django_test_output(output, exit_code=1)

    assert result.passed == 3
    assert result.failed == 2
    assert len(result.failures) == 2
    assert result.failures[0].file == "app/tests.py"
    assert result.failures[0].line == 42
    assert "[REDACTED]" in result.raw_output
    assert "django-insecure-secret" not in result.raw_output


def test_dashboard_generator_uses_daisyui_stats_tables_and_urls():
    dashboard = render_dashboard(_spec())

    assert '{% extends "base.html" %}' in dashboard
    assert "stats" in dashboard
    assert "table table-sm" in dashboard
    assert "{% url 'task-list' %}" in dashboard
    assert "{% url 'task-detail' task.id %}" in dashboard


def test_frontend_and_django_test_generators_render_expected_files():
    project = _spec()
    frontend = render_frontend_django_files(project)
    tests = render_django_test_files(project)

    assert set(frontend) == {
        "app/templates/dashboard.html",
        "app/templates/resource_list.html",
        "app/templates/resource_detail.html",
        "app/templates/resource_form.html",
    }
    assert "{{ form|crispy }}" in frontend["app/templates/resource_form.html"]
    assert "hx-boost" in frontend["app/templates/resource_form.html"]
    assert "class TaskApiTests(TestCase):" in tests["app/tests.py"]
    assert "APIClient" in tests["app/tests.py"]
    assert "def test_delete" in tests["app/tests.py"]
    ast.parse(tests["app/tests.py"])


def test_project_writer_now_writes_m5_dashboard_and_tests(tmp_path: Path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")

    state = DjangoProjectWriter(tmp_path, approval_func=lambda _request: True).write_project(_spec(), prd)

    assert (tmp_path / "app" / "templates" / "dashboard.html").exists()
    assert (tmp_path / "app" / "tests.py").exists()
    assert "app/templates/dashboard.html" in state.completed_files
    assert "app/tests.py" in state.completed_files


@pytest.mark.asyncio
async def test_error_feedback_loop_runs_fix_and_retests_until_success(tmp_path: Path):
    failing = ShamsuTestRunResult(passed=1, failed=1, raw_output="FAILED (failures=1)")
    passing = ShamsuTestRunResult(passed=2, failed=0, raw_output="OK")
    tests = SequenceTestRunner([failing, passing])
    bugfix = FakeBugFixWorkflow(applied=True)

    result = await ErrorFeedbackLoop(
        tmp_path,
        search=EmptySearch(),
        test_runner=tests,
        bugfix_workflow=bugfix,
    ).run(tmp_path)

    assert result.success is True
    assert tests.calls == 2
    assert len(result.iterations) == 1
    assert bugfix.reports
    assert "Django test feedback iteration 1 failed" in bugfix.reports[0]


@pytest.mark.asyncio
async def test_error_feedback_loop_stops_after_three_failed_iterations(tmp_path: Path):
    failing = ShamsuTestRunResult(passed=0, failed=1, raw_output="FAILED (errors=1)")
    tests = SequenceTestRunner([failing])
    bugfix = FakeBugFixWorkflow(applied=True)

    result = await ErrorFeedbackLoop(
        tmp_path,
        search=EmptySearch(),
        test_runner=tests,
        bugfix_workflow=bugfix,
        max_iterations=3,
    ).run(tmp_path)

    assert result.success is False
    assert len(result.iterations) == 3
    assert "still failing" in result.error


@pytest.mark.asyncio
async def test_error_feedback_loop_stops_when_patch_is_not_applied(tmp_path: Path):
    failing = ShamsuTestRunResult(passed=0, failed=1, raw_output="FAILED (failures=1)")
    bugfix = FakeBugFixWorkflow(applied=False)

    result = await ErrorFeedbackLoop(
        tmp_path,
        search=EmptySearch(),
        test_runner=SequenceTestRunner([failing]),
        bugfix_workflow=bugfix,
    ).run(tmp_path)

    assert result.success is False
    assert result.error == "Patch denied"
    assert len(result.iterations) == 1


def _django_project(root: Path) -> Path:
    project = root / "generated"
    project.mkdir()
    (project / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    return project
