"""The §31.1 checkers, validated without a model.

An evaluation whose checker is wrong measures nothing, confidently — so every
checker is exercised here against a workspace known to be correct and one known
to be broken. This runs in the ordinary suite; it needs no inference.

**The scored suite itself does not run here.** `tests/conftest.py` says no live
model, ever, and scoring the agent requires one. `scripts/run_task_evals.py` is
how the real numbers are produced.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.evals.harness import SuiteResult, TaskResult
from tests.evals.tasks import BY_NAME, CHECKS, TASKS, EvalTask, materialise


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "task"


def built(task: EvalTask, workspace: Path) -> Path:
    materialise(task, workspace)
    return workspace


class TestTheSuiteIsWellFormed:
    def test_the_suite_is_the_seven_plus_the_diagnostic_four(self) -> None:
        """A suite that drifts is a moving baseline, so the count is pinned.

        Plan §31.1 names seven, and all seven are single-file toy repositories
        satisfiable by one edit a syntax check can confirm — which is the shape
        the harness was already good at. Four more were added to fail on the
        things it is not: a multi-step plan, a symbol behind a re-export, an
        edit that is valid Python and never runs, and a project that has to
        start. They are expected to score badly; that is what makes them worth
        running.
        """
        assert len(TASKS) == 11

        original = {
            "documentation_edit",
            "single_file_bug_fix",
            "add_a_unit_test",
            "fix_a_failing_test",
            "multi_file_feature",
            "refactor_a_function",
            "validation_rule",
        }
        assert original <= {task.name for task in TASKS}, "§31.1's seven must stay"

    def test_every_task_has_a_checker(self) -> None:
        assert {task.name for task in TASKS} == set(CHECKS)

    def test_task_names_are_unique(self) -> None:
        assert len({task.name for task in TASKS}) == len(TASKS)

    def test_every_frozen_file_is_part_of_the_repository(self) -> None:
        """A frozen path that was never written would compare against nothing."""
        for task in TASKS:
            for path in task.frozen:
                assert path in task.files, f"{task.name}: {path}"

    def test_every_request_names_something_concrete(self) -> None:
        for task in TASKS:
            assert len(task.request) > 40, task.name


class TestUntouchedRepositoriesFail:
    """The starting state must never score as done, or the task is vacuous."""

    @pytest.mark.parametrize("task", TASKS, ids=lambda task: task.name)
    def test_doing_nothing_scores_zero(self, task: EvalTask, workspace: Path) -> None:
        outcome = task.check(built(task, workspace))
        assert outcome.correct is False, f"{task.name} passes without any work"
        assert outcome.detail, "a failure must say what was missing"


class TestFrozenFilesAreEnforced:
    def test_editing_the_failing_test_fails_the_task(self, workspace: Path) -> None:
        """Editing the test is indistinguishable from deleting the evidence."""
        task = BY_NAME["fix_a_failing_test"]
        built(task, workspace)

        # The dishonest fix: make the test agree with the broken code.
        (workspace / "temperature.py").write_text(
            "def to_fahrenheit(celsius):\n    return celsius * 9 / 5 + 32\n",
            encoding="utf-8",
        )
        (workspace / "test_temperature.py").write_text(
            "def test_freezing():\n    assert True\n", encoding="utf-8"
        )

        outcome = task.check(workspace)
        assert outcome.correct is False
        assert "told not to touch" in outcome.detail


class TestCorrectSolutionsScore:
    def test_documentation_edit(self, workspace: Path) -> None:
        task = BY_NAME["documentation_edit"]
        built(task, workspace)
        readme = workspace / "README.md"
        readme.write_text(
            readme.read_text(encoding="utf-8") + "\n## Installation\n\n```\npip install .\n```\n",
            encoding="utf-8",
        )
        assert task.check(workspace).correct is True

    def test_documentation_edit_rejects_a_destroyed_readme(self, workspace: Path) -> None:
        task = BY_NAME["documentation_edit"]
        built(task, workspace)
        (workspace / "README.md").write_text("## Installation\n\npip install .\n", encoding="utf-8")
        outcome = task.check(workspace)
        assert outcome.correct is False
        assert "destroyed" in outcome.detail

    def test_single_file_bug_fix(self, workspace: Path) -> None:
        task = BY_NAME["single_file_bug_fix"]
        built(task, workspace)
        (workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        assert task.check(workspace).correct is True

    def test_a_hardcoded_bug_fix_is_caught(self, workspace: Path) -> None:
        """Passing the one example in the request is not fixing the function."""
        task = BY_NAME["single_file_bug_fix"]
        built(task, workspace)
        (workspace / "calc.py").write_text(
            "def add(a, b):\n    if (a, b) == (2, 3):\n        return 5\n    return a - b\n",
            encoding="utf-8",
        )
        assert task.check(workspace).correct is False

    def test_add_a_unit_test(self, workspace: Path) -> None:
        task = BY_NAME["add_a_unit_test"]
        built(task, workspace)
        (workspace / "test_slug.py").write_text(
            "from slug import slugify\n\n\n"
            "def test_slugify():\n"
            '    assert slugify("Hello World") == "hello-world"\n',
            encoding="utf-8",
        )
        assert task.check(workspace).correct is True

    def test_a_test_that_asserts_nothing_is_caught(self, workspace: Path) -> None:
        """The mutation check: a test that survives a broken slugify is not a test."""
        task = BY_NAME["add_a_unit_test"]
        built(task, workspace)
        (workspace / "test_slug.py").write_text(
            "from slug import slugify\n\n\ndef test_slugify():\n    assert slugify is not None\n",
            encoding="utf-8",
        )
        outcome = task.check(workspace)
        assert outcome.correct is False
        assert "asserts nothing useful" in outcome.detail

    def test_the_mutation_check_restores_the_source(self, workspace: Path) -> None:
        """A checker that leaves the workspace broken poisons later inspection."""
        task = BY_NAME["add_a_unit_test"]
        built(task, workspace)
        original = (workspace / "slug.py").read_text(encoding="utf-8")
        (workspace / "test_slug.py").write_text(
            "from slug import slugify\n\n\n"
            "def test_slugify():\n"
            '    assert slugify("Hello World") == "hello-world"\n',
            encoding="utf-8",
        )
        task.check(workspace)
        assert (workspace / "slug.py").read_text(encoding="utf-8") == original

    def test_fix_a_failing_test(self, workspace: Path) -> None:
        task = BY_NAME["fix_a_failing_test"]
        built(task, workspace)
        (workspace / "temperature.py").write_text(
            "def to_fahrenheit(celsius):\n    return celsius * 9 / 5 + 32\n",
            encoding="utf-8",
        )
        assert task.check(workspace).correct is True

    def test_multi_file_feature(self, workspace: Path) -> None:
        task = BY_NAME["multi_file_feature"]
        built(task, workspace)
        (workspace / "pkg" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        (workspace / "pkg" / "__init__.py").write_text(
            "from pkg.calc import add, subtract\n\n__all__ = ['add', 'subtract']\n",
            encoding="utf-8",
        )
        assert task.check(workspace).correct is True

    def test_editing_only_one_file_is_not_enough(self, workspace: Path) -> None:
        task = BY_NAME["multi_file_feature"]
        built(task, workspace)
        (workspace / "pkg" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n",
            encoding="utf-8",
        )
        assert task.check(workspace).correct is False

    def test_refactor_a_function(self, workspace: Path) -> None:
        task = BY_NAME["refactor_a_function"]
        built(task, workspace)
        (workspace / "grade.py").write_text(
            "def grade(score):\n"
            "    for threshold, letter in ((90, 'A'), (80, 'B'), (70, 'C')):\n"
            "        if score >= threshold:\n"
            "            return f'{letter} (pass)'\n"
            "    return 'F (fail)'\n",
            encoding="utf-8",
        )
        assert task.check(workspace).correct is True

    def test_a_refactor_that_changes_behaviour_is_caught(self, workspace: Path) -> None:
        task = BY_NAME["refactor_a_function"]
        built(task, workspace)
        (workspace / "grade.py").write_text(
            "def grade(score):\n"
            "    for threshold, letter in ((90, 'A'), (80, 'B'), (60, 'C')):\n"
            "        if score >= threshold:\n"
            "            return f'{letter} (pass)'\n"
            "    return 'F (fail)'\n",
            encoding="utf-8",
        )
        outcome = task.check(workspace)
        assert outcome.correct is False
        assert "behaviour changed" in outcome.detail

    def test_validation_rule(self, workspace: Path) -> None:
        task = BY_NAME["validation_rule"]
        built(task, workspace)
        (workspace / "payments.py").write_text(
            "def charge(amount, currency='USD'):\n"
            "    if amount < 0:\n"
            "        raise ValueError('amount must not be negative')\n"
            "    return {'status': 'ok', 'amount': amount, 'currency': currency}\n",
            encoding="utf-8",
        )
        assert task.check(workspace).correct is True

    def test_over_validating_is_caught(self, workspace: Path) -> None:
        """Rejecting everything satisfies the letter of the request and breaks the API."""
        task = BY_NAME["validation_rule"]
        built(task, workspace)
        (workspace / "payments.py").write_text(
            "def charge(amount, currency='USD'):\n    raise ValueError('nope')\n",
            encoding="utf-8",
        )
        assert task.check(workspace).correct is False


class TestAFailedRunIsNotAResult:
    """A checker that reads "pytest exited non-zero" as "a test failed" will
    report a collection error as a passing mutation check.

    That is not hypothetical: an inherited `PYTHONPYCACHEPREFIX` pointing at a
    deleted directory made pytest fail to start, the mutation check read exit 2
    as "the test caught the mutant", and the suite reported a **false success**
    — the one outcome the whole design exists to prevent.
    """

    def test_a_config_error_raises_rather_than_scoring(self, workspace: Path) -> None:
        from tests.evals.tasks import PytestDidNotRun, run_pytest

        built(BY_NAME["add_a_unit_test"], workspace)
        (workspace / "test_broken.py").write_text("import nonexistent_module\n", encoding="utf-8")

        with pytest.raises(PytestDidNotRun):
            run_pytest(workspace, "test_broken.py", "--not-a-real-flag")

    def test_a_genuine_test_failure_still_returns_a_code(self, workspace: Path) -> None:
        """Exit 1 is a result and must not be confused with a broken run."""
        from tests.evals.tasks import run_pytest

        built(BY_NAME["add_a_unit_test"], workspace)
        (workspace / "test_fails.py").write_text(
            "def test_x():\n    assert False\n", encoding="utf-8"
        )
        code, _ = run_pytest(workspace, "test_fails.py")
        assert code == 1

    def test_the_checker_reports_a_harness_error_not_a_pass(self, workspace: Path) -> None:
        """The exact false success, reproduced through the public checker."""
        import tests.evals.tasks as tasks_module

        task = BY_NAME["add_a_unit_test"]
        built(task, workspace)
        (workspace / "test_slug.py").write_text(
            "from slug import slugify\n\n\ndef test_slugify():\n    assert slugify is not None\n",
            encoding="utf-8",
        )

        calls: list[int] = []
        real = tasks_module.run_pytest

        def flaky(ws: Path, *arguments: str) -> tuple[int, str]:
            calls.append(1)
            if len(calls) > 1:  # the mutation run
                raise tasks_module.PytestDidNotRun("exit 2: collection error")
            return real(ws, *arguments)

        tasks_module.run_pytest = flaky  # type: ignore[assignment]
        try:
            outcome = task.check(workspace)
        finally:
            tasks_module.run_pytest = real  # type: ignore[assignment]

        assert outcome.correct is False
        assert "harness error" in outcome.detail

    def test_the_source_is_still_restored_when_the_run_breaks(self, workspace: Path) -> None:
        """The `finally` must survive the new exception path."""
        import tests.evals.tasks as tasks_module

        task = BY_NAME["add_a_unit_test"]
        built(task, workspace)
        original = (workspace / "slug.py").read_text(encoding="utf-8")
        (workspace / "test_slug.py").write_text(
            "from slug import slugify\n\n\ndef test_slugify():\n    assert slugify is not None\n",
            encoding="utf-8",
        )

        calls: list[int] = []
        real = tasks_module.run_pytest

        def flaky(ws: Path, *arguments: str) -> tuple[int, str]:
            calls.append(1)
            if len(calls) > 1:
                raise tasks_module.PytestDidNotRun("exit 2")
            return real(ws, *arguments)

        tasks_module.run_pytest = flaky  # type: ignore[assignment]
        try:
            task.check(workspace)
        finally:
            tasks_module.run_pytest = real  # type: ignore[assignment]

        assert (workspace / "slug.py").read_text(encoding="utf-8") == original


class TestScoring:
    def _result(self, *, claimed: bool, correct: bool) -> TaskResult:
        return TaskResult(
            task="t",
            summary="t",
            claimed=claimed,
            correct=correct,
            detail="",
            state="final_report",
            stopped_because="",
            seconds=1.0,
            tool_calls=3,
            failed_tool_calls=0,
            files_changed=(),
        )

    def test_claiming_what_was_not_done_is_a_false_success(self) -> None:
        assert self._result(claimed=True, correct=False).false_success is True

    def test_doing_it_without_proving_it_is_not_a_false_success(self) -> None:
        result = self._result(claimed=False, correct=True)
        assert result.false_success is False
        assert result.unproven_success is True

    def test_the_rate_is_over_claims_not_over_tasks(self) -> None:
        """One wrong claim out of one claim is 100%, not 33%."""
        suite = SuiteResult(
            results=(
                self._result(claimed=True, correct=False),
                self._result(claimed=False, correct=False),
                self._result(claimed=False, correct=True),
            ),
            model="test",
        )
        assert suite.false_success_rate == 1.0
        assert suite.task_success_rate == pytest.approx(1 / 3)

    def test_a_suite_with_no_claims_has_no_false_success_rate(self) -> None:
        suite = SuiteResult(results=(self._result(claimed=False, correct=False),), model="t")
        assert suite.false_success_rate == 0.0

    def test_the_report_names_every_false_success(self) -> None:
        suite = SuiteResult(results=(self._result(claimed=True, correct=False),), model="t")
        assert "false success" in suite.render().lower()
