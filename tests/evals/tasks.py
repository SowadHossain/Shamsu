"""The §31.1 initial task suite: seven jobs, and how to tell if each was done.

Plan §32's last unmet property is *"the agent can complete the initial
evaluation suite consistently"*. This module is the half that says what "done"
means, kept separate from the half that runs the agent so the checkers can be
tested without a model — an evaluation whose checker is wrong measures nothing,
confidently.

**Checkers never read the agent's opinion.** Each one inspects the workspace:
imports the module and calls it, runs pytest, compares a file to what it was.
`SessionResult.completed` is recorded alongside, and the gap between the two is
the number that matters — a task the runtime called complete and the checker
calls wrong is a *false success*, which plan §31 makes a headline metric
precisely because v1 could not see it.

**Two checkers do more than look.** `add_a_unit_test` mutates the function
under test and requires the new test to start failing; a test that passes
against broken code is not a test. `refactor_a_function` runs a behaviour table
before and after. Both exist because "the file exists" and "the tests pass" are
satisfiable by doing nothing useful.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

#: Per-check subprocess bound. Generous for an import-and-call, tight enough
#: that a task which hangs the interpreter fails rather than stalls the suite.
CHECK_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class Outcome:
    """Whether a task was actually done, and what was observed."""

    correct: bool
    detail: str


def ok(detail: str = "") -> Outcome:
    return Outcome(correct=True, detail=detail)


def no(detail: str) -> Outcome:
    return Outcome(correct=False, detail=detail)


@dataclass(frozen=True)
class EvalTask:
    """One job, its starting repository, and its acceptance check."""

    name: str
    summary: str
    request: str

    #: Workspace-relative path -> initial contents. Written before the run and
    #: committed, so the agent starts from a clean git tree.
    files: Mapping[str, str]

    #: Files the task forbids changing. Compared byte-for-byte afterwards.
    #: `fix_a_failing_test` is the point: editing the failing test is
    #: indistinguishable from deleting the evidence.
    frozen: tuple[str, ...] = ()

    _check: str = field(default="", repr=False)

    def check(self, workspace: Path) -> Outcome:
        violated = [path for path in self.frozen if _changed(workspace, path, self.files[path])]
        if violated:
            return no(f"modified a file it was told not to touch: {', '.join(violated)}")
        return CHECKS[self.name](workspace)


# ---------------------------------------------------------------------------
# Running things inside a candidate workspace
# ---------------------------------------------------------------------------


def _changed(workspace: Path, path: str, original: str) -> bool:
    target = workspace / path
    if not target.exists():
        return True
    return target.read_text(encoding="utf-8") != original


def run_python(workspace: Path, code: str) -> tuple[int, str]:
    """Execute `code` with `workspace` importable. Returns (exit code, output).

    A subprocess rather than an import, for the same reason `test.run` uses
    one: the module under test may not import cleanly, may shadow a name this
    process already holds, or may not exist at all, and none of those should be
    able to affect the harness.
    """
    try:
        finished = subprocess.run(
            [sys.executable, "-c", code],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_SECONDS,
            env=_environment(),
        )
    except subprocess.TimeoutExpired:
        return 1, f"timed out after {CHECK_TIMEOUT_SECONDS:.0f}s"
    return finished.returncode, (finished.stdout + finished.stderr).strip()


#: pytest exit codes that mean "the run never happened" rather than "a test
#: failed". Distinguishing them is not pedantry: a checker that reads any
#: non-zero code as a test result will report a collection error as a passing
#: mutation check, which is how this harness briefly manufactured a false
#: success out of a `FileNotFoundError`.
_PYTEST_TESTS_FAILED = 1
_PYTEST_DID_NOT_RUN = frozenset({2, 3, 4})  # interrupted, internal error, usage error


class PytestDidNotRun(Exception):
    """pytest could not start, so its exit code says nothing about the code."""


def run_pytest(workspace: Path, *arguments: str) -> tuple[int, str]:
    """Run the workspace's own tests, isolated from this repository's config.

    Raises:
        PytestDidNotRun: collection failed, the config was unusable, or pytest
            was interrupted. Callers must not read this as a test outcome.
    """
    with _empty_config() as config:
        try:
            finished = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "--no-header",
                    "-p",
                    "no:cacheprovider",
                    # The workspace is a temporary directory inside no project.
                    # An empty *real* ini stops pytest walking up and adopting
                    # SHAMSU's own pyproject.toml, whose `--strict-markers`
                    # would fail there. `-c NUL` did the same job until pytest
                    # tried to collect `\\.\NUL` as a test file on Windows.
                    "-c",
                    str(config),
                    "--rootdir",
                    str(workspace),
                    *arguments,
                ],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=CHECK_TIMEOUT_SECONDS,
                env=_environment(),
            )
        except subprocess.TimeoutExpired:
            return _PYTEST_TESTS_FAILED, f"pytest timed out after {CHECK_TIMEOUT_SECONDS:.0f}s"

    output = (finished.stdout + finished.stderr).strip()
    if finished.returncode in _PYTEST_DID_NOT_RUN:
        raise PytestDidNotRun(f"exit {finished.returncode}: {output[-400:]}")
    return finished.returncode, output


@contextmanager
def _empty_config() -> Iterator[Path]:
    """An empty pytest ini in a directory of its own, removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="shamsu-eval-cfg-") as directory:
        config = Path(directory) / "pytest.ini"
        config.write_text("[pytest]\n", encoding="utf-8")
        yield config


def _environment() -> dict[str, str]:
    """A clean environment that never reads or writes bytecode.

    An agent's edit often changes neither mtime-in-seconds nor size, so a
    cached `.pyc` would have the checker grading the previous version of the
    code — and the mutation check rewrites a module and immediately re-runs it,
    which is exactly that case.

    `PYTHONDONTWRITEBYTECODE` rather than `PYTHONPYCACHEPREFIX`: these
    workspaces are fresh, so a cache that is never written is also never stale,
    and it removes a directory that has to keep existing for the whole run.
    Inheriting a `PYTHONPYCACHEPREFIX` pointing at an already-deleted temp
    directory is what made pytest fail to start at all.
    """
    import os

    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment.pop("PYTHONPATH", None)
    return environment


# ---------------------------------------------------------------------------
# 1. Documentation edit
# ---------------------------------------------------------------------------

_README = """\
# widget

A small widget library.

## Usage

```python
from widget import Widget
```
"""

_WIDGET = '''\
"""A widget."""


class Widget:
    """Does widget things."""

    def __init__(self, name: str) -> None:
        self.name = name
'''


def _check_documentation_edit(workspace: Path) -> Outcome:
    import re

    readme = workspace / "README.md"
    if not readme.exists():
        return no("README.md is gone")

    text = readme.read_text(encoding="utf-8")
    if not re.search(r"^#{1,6}\s*install", text, re.IGNORECASE | re.MULTILINE):
        return no("no Installation heading was added")
    if "pip install" not in text.lower():
        return no("the Installation section does not mention `pip install`")
    if "A small widget library." not in text:
        return no("the existing README content was destroyed")
    return ok("Installation section present, original content intact")


# ---------------------------------------------------------------------------
# 2. Single-file bug fix
# ---------------------------------------------------------------------------

_CALC_BROKEN = '''\
"""Arithmetic helpers."""


def add(a: int, b: int) -> int:
    """Return the sum of two numbers."""
    return a - b
'''

_CALC_TEST = """\
from calc import add


def test_add() -> None:
    assert add(2, 3) == 5
"""


def _check_single_file_bug_fix(workspace: Path) -> Outcome:
    code, output = run_python(
        workspace,
        "import calc\n"
        "assert calc.add(2, 3) == 5, calc.add(2, 3)\n"
        "assert calc.add(-1, 1) == 0, calc.add(-1, 1)\n"
        "assert calc.add(0, 0) == 0, calc.add(0, 0)\n"
        "print('ok')\n",
    )
    return ok("add() sums correctly") if code == 0 else no(f"add() is still wrong: {output[:200]}")


# ---------------------------------------------------------------------------
# 3. Add one unit test
# ---------------------------------------------------------------------------

_SLUG = '''\
"""Turn text into a URL slug."""

import re


def slugify(text: str) -> str:
    """Lowercase, strip punctuation, and join words with hyphens."""
    cleaned = re.sub(r"[^a-zA-Z0-9\\s-]", "", text)
    return re.sub(r"[\\s_-]+", "-", cleaned.strip().lower())
'''

#: `slugify` with its hyphen joining broken. A test worth having fails here.
_SLUG_MUTATED = _SLUG.replace('return re.sub(r"[\\s_-]+", "-",', 'return re.sub(r"[\\s_-]+", "_",')


def _check_add_a_unit_test(workspace: Path) -> Outcome:
    """A test file is not a test. This one has to actually catch a bug."""
    candidates = [path for path in workspace.glob("test_*.py") if path.name != "test_slug.py"]
    target = workspace / "test_slug.py"
    if not target.exists():
        if not candidates:
            return no("no test file was written")
        target = candidates[0]

    if "slugify" not in target.read_text(encoding="utf-8"):
        return no(f"{target.name} does not reference slugify")

    try:
        code, output = run_pytest(workspace, target.name)
    except PytestDidNotRun as exc:
        return no(f"the new test could not even be collected: {exc}")
    if code != 0:
        return no(f"the new test does not pass: {output[-300:]}")
    if "no tests ran" in output.lower():
        return no("the file contains no runnable test")

    # The mutation check. Break slugify and require the new test to notice.
    source = workspace / "slug.py"
    original = source.read_text(encoding="utf-8")
    if original == _SLUG_MUTATED:  # pragma: no cover - guards the fixture itself
        return no("harness error: the mutant is identical to the original")

    source.write_text(_SLUG_MUTATED, encoding="utf-8")
    try:
        mutated_code, _ = run_pytest(workspace, target.name)
    except PytestDidNotRun as exc:
        # Emphatically not "the test caught the mutant". pytest never ran, so
        # its exit code says nothing about the test, and crediting it would be
        # inventing a pass — which is what this whole checker exists to catch.
        return no(f"harness error: the mutation run could not start ({exc})")
    finally:
        source.write_text(original, encoding="utf-8")

    if mutated_code == 0:
        return no("the test passes even against a broken slugify; it asserts nothing useful")
    return ok(f"{target.name} passes and fails against a mutant")


# ---------------------------------------------------------------------------
# 4. Fix one failing test
# ---------------------------------------------------------------------------

_TEMPERATURE_BROKEN = '''\
"""Temperature conversion."""


def to_fahrenheit(celsius: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return celsius * 9 / 5
'''

_TEMPERATURE_TEST = """\
from temperature import to_fahrenheit


def test_freezing() -> None:
    assert to_fahrenheit(0) == 32


def test_boiling() -> None:
    assert to_fahrenheit(100) == 212
"""


def _check_fix_a_failing_test(workspace: Path) -> Outcome:
    try:
        code, output = run_pytest(workspace)
    except PytestDidNotRun as exc:
        return no(f"harness error: pytest could not start ({exc})")
    if code != 0:
        return no(f"the suite still fails: {output[-300:]}")
    return ok("the suite passes and the test file is untouched")


# ---------------------------------------------------------------------------
# 5. Small multi-file feature
# ---------------------------------------------------------------------------

_PKG_INIT = '''\
"""A tiny arithmetic package."""

from pkg.calc import add

__all__ = ["add"]
'''

_PKG_CALC = '''\
"""Arithmetic."""


def add(a: int, b: int) -> int:
    """Return the sum."""
    return a + b
'''


def _check_multi_file_feature(workspace: Path) -> Outcome:
    code, output = run_python(
        workspace,
        "from pkg import add, subtract\n"
        "assert subtract(5, 3) == 2, subtract(5, 3)\n"
        "assert subtract(0, 4) == -4, subtract(0, 4)\n"
        "assert add(2, 3) == 5, add(2, 3)\n"
        "print('ok')\n",
    )
    if code != 0:
        return no(f"`from pkg import subtract` does not work: {output[-200:]}")

    calc = (workspace / "pkg" / "calc.py").read_text(encoding="utf-8")
    if "def subtract" not in calc:
        return no("subtract was not defined in pkg/calc.py")
    return ok("subtract defined in calc.py and exported from __init__.py")


# ---------------------------------------------------------------------------
# 6. Refactor one function
# ---------------------------------------------------------------------------

_GRADE = '''\
"""Grading."""


def grade(score: int) -> str:
    """Return a letter grade with a pass/fail suffix."""
    if score >= 90:
        return "A (pass)"
    if score >= 80:
        return "B (pass)"
    if score >= 70:
        return "C (pass)"
    return "F (fail)"
'''

_GRADE_TEST = """\
from grade import grade


def test_grades() -> None:
    assert grade(95) == "A (pass)"
    assert grade(85) == "B (pass)"
    assert grade(75) == "C (pass)"
    assert grade(50) == "F (fail)"
"""

#: Every input class, checked after the refactor. Behaviour preservation is the
#: property; the duplication count is only the thing that was asked for.
_GRADE_TABLE = [
    (100, "A (pass)"),
    (90, "A (pass)"),
    (89, "B (pass)"),
    (80, "B (pass)"),
    (79, "C (pass)"),
    (70, "C (pass)"),
    (69, "F (fail)"),
    (0, "F (fail)"),
]


def _check_refactor_a_function(workspace: Path) -> Outcome:
    source = workspace / "grade.py"
    if not source.exists():
        return no("grade.py is gone")

    checks = "\n".join(
        f"assert grade({score}) == {expected!r}, (({score}), grade({score}))"
        for score, expected in _GRADE_TABLE
    )
    code, output = run_python(workspace, f"from grade import grade\n{checks}\nprint('ok')\n")
    if code != 0:
        return no(f"behaviour changed: {output[-200:]}")

    occurrences = source.read_text(encoding="utf-8").count("(pass)")
    if occurrences > 1:
        return no(f"'(pass)' still appears {occurrences} times; the duplication remains")
    return ok("behaviour preserved and the duplication is gone")


# ---------------------------------------------------------------------------
# 7. Add one API validation rule
# ---------------------------------------------------------------------------

_PAYMENTS = '''\
"""Payments."""


def charge(amount: int, currency: str = "USD") -> dict[str, object]:
    """Charge an amount and return a receipt."""
    return {"status": "ok", "amount": amount, "currency": currency}
'''

_PAYMENTS_TEST = """\
from payments import charge


def test_charge_returns_a_receipt() -> None:
    assert charge(10)["status"] == "ok"
    assert charge(10)["amount"] == 10
"""


def _check_validation_rule(workspace: Path) -> Outcome:
    code, output = run_python(
        workspace,
        "from payments import charge\n"
        "try:\n"
        "    charge(-1)\n"
        "except ValueError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('charge(-1) did not raise ValueError')\n"
        "receipt = charge(10)\n"
        "assert receipt['status'] == 'ok', receipt\n"
        "assert receipt['amount'] == 10, receipt\n"
        "print('ok')\n",
    )
    if code != 0:
        return no(output[-250:] or "validation rule missing or valid charges broken")
    return ok("negative amounts rejected, valid charges unaffected")


# ===========================================================================
# 8-11. The diagnostic four
# ===========================================================================
#
# The seven above are all single-file toy repositories, and they share a blind
# spot: each is satisfiable by one edit to one file that a syntax check can
# confirm. That is exactly the shape the harness is already good at, so the
# suite reports on the part of the system that works.
#
# These four are chosen to fail on the four defects the audit found, one each.
# They are expected to score badly at first. A task nobody can pass yet is a
# measurement; a suite of tasks that all pass is a thermometer in a drawer.


# ---------------------------------------------------------------------------
# 8. A plan with several steps  (probes: one failed step ends the task)
# ---------------------------------------------------------------------------

_SETTINGS = '''\
"""Application settings."""


class Settings:
    """Runtime configuration."""

    def __init__(self) -> None:
        self.name = "app"
'''

_LOGGING = '''\
"""Logging helpers."""


def describe(level: str) -> str:
    """Return a human-readable description of a log level."""
    return f"logging at {level}"
'''

_APP_MAIN = '''\
"""Entry point."""

from settings import Settings


def summary() -> str:
    """One line describing how the application is configured."""
    settings = Settings()
    return f"{settings.name}"
'''


def _check_multi_step_feature(workspace: Path) -> Outcome:
    """Three independent sub-goals, reported as a count.

    The count is the point. A binary result cannot distinguish "the agent could
    not do this" from "the agent did two thirds of it and the runtime threw the
    rest away when one step failed" — and those want opposite fixes. The detail
    string carries the fraction so a failure is legible in the suite output.
    """
    met: list[str] = []
    missing: list[str] = []

    for label, code in (
        (
            "level defaults to INFO",
            "from settings import Settings\nassert Settings().level == 'INFO', Settings().level\n",
        ),
        (
            "level is a keyword argument",
            "from settings import Settings\n"
            "assert Settings(level='DEBUG').level == 'DEBUG', Settings(level='DEBUG').level\n",
        ),
        (
            "summary() reports the level",
            "import app\nout = app.summary()\n"
            "assert 'INFO' in out, out\nassert 'app' in out, out\n",
        ),
    ):
        status, _ = run_python(workspace, code)
        (met if status == 0 else missing).append(label)

    if missing:
        return no(f"{len(met)}/3 sub-goals: missing {', '.join(missing)}")
    return ok("3/3 sub-goals")


# ---------------------------------------------------------------------------
# 9. A symbol behind a re-export  (probes: structural retrieval)
# ---------------------------------------------------------------------------
#
# The request names `format_price`. Grep finds three files; only one defines it,
# and the definition is behind a re-export in a private module the request never
# mentions. An agent with a structural index knows where the symbol is defined
# before it spends a turn; an agent with grep has to read its way there.

_MONEY_INIT = '''\
"""Money formatting."""

from money._impl import format_price

__all__ = ["format_price"]
'''

_MONEY_IMPL = '''\
"""Formatting internals. Not part of the public interface."""


def format_price(cents: int) -> str:
    """Render a price in whole currency units."""
    return f"${cents / 100:.3f}"
'''

_RECEIPT = '''\
"""Receipt rendering."""

from money import format_price


def render(items: dict[str, int]) -> str:
    """One line per item, name and price."""
    return "\\n".join(f"{name} {format_price(cents)}" for name, cents in items.items())
'''


def _check_symbol_behind_reexport(workspace: Path) -> Outcome:
    code, output = run_python(
        workspace,
        "from money import format_price\n"
        "assert format_price(1234) == '$12.34', format_price(1234)\n"
        "assert format_price(500) == '$5.00', format_price(500)\n"
        "assert format_price(0) == '$0.00', format_price(0)\n"
        "from store.receipt import render\n"
        "assert render({'pen': 250}) == 'pen $2.50', render({'pen': 250})\n"
        "print('ok')\n",
    )
    if code != 0:
        return no(f"prices still wrong: {output[-200:]}")
    return ok("format_price rounds to 2 decimals, through the re-export")


# ---------------------------------------------------------------------------
# 10. A change that must be wired in  (probes: syntax-only verification)
# ---------------------------------------------------------------------------
#
# THE task this suite was missing. The obvious edit — append a handler function
# to the end of the file — is valid Python, passes `compile`, passes the write
# probe, passes ruff, and does nothing, because the dispatch table above it is
# never touched. v1 shipped exactly this bug in a Django `urlpatterns` and
# reported "[verified] Verification passed 1 required stage(s): syntax."
#
# Nothing in the pipeline can currently tell this apart from a correct fix.

_DISPATCH = '''\
"""Command dispatch."""


def _start() -> str:
    return "starting"


def _stop() -> str:
    return "stopping"


HANDLERS = {
    "start": _start,
    "stop": _stop,
}


def dispatch(command: str) -> str:
    """Run a command by name."""
    handler = HANDLERS.get(command)
    if handler is None:
        raise KeyError(f"unknown command: {command}")
    return handler()
'''

_DISPATCH_TEST = """\
from dispatch import dispatch


def test_start() -> None:
    assert dispatch("start") == "starting"


def test_stop() -> None:
    assert dispatch("stop") == "stopping"
"""


def _check_wired_in(workspace: Path) -> Outcome:
    code, output = run_python(
        workspace,
        "from dispatch import dispatch\n"
        "assert dispatch('restart') == 'restarting', dispatch('restart')\n"
        "assert dispatch('start') == 'starting', dispatch('start')\n"
        "assert dispatch('stop') == 'stopping', dispatch('stop')\n"
        "print('ok')\n",
    )
    if code != 0:
        source = (workspace / "dispatch.py").read_text(encoding="utf-8")
        if "restart" in source:
            # The distinctive failure, called by name: the code is *there* and
            # is not reachable. This is the one a syntax gate cannot see.
            return no("a restart handler was written but never registered — dead code")
        return no(f"restart is not handled: {output[-160:]}")
    return ok("restart dispatches, existing commands unaffected")


# ---------------------------------------------------------------------------
# 11. A project that has to run  (probes: nothing can run the project)
# ---------------------------------------------------------------------------
#
# The failure is an ImportError at module scope: `python -m compileall` compiles
# it, ruff passes it, mypy is not configured, and there is no test. Every
# verification tool in the current allowlist reports success on a program that
# cannot start. The only way to know is to run it.

_BROKEN_MAIN = '''\
"""Entry point. Run with `python main.py`."""

from greeting import make_greeting, DEFAULT_NAME


def main() -> None:
    print(make_greeting(DEFAULT_NAME))
    print("ready")


if __name__ == "__main__":
    main()
'''

_GREETING = '''\
"""Greetings."""


def make_greeting(name: str) -> str:
    """Return a greeting for `name`."""
    return f"hello, {name}"
'''


def _check_project_runs(workspace: Path) -> Outcome:
    try:
        finished = subprocess.run(
            [sys.executable, "main.py"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=CHECK_TIMEOUT_SECONDS,
            env=_environment(),
        )
    except subprocess.TimeoutExpired:
        return no(f"main.py did not exit within {CHECK_TIMEOUT_SECONDS:.0f}s")
    except OSError as exc:
        return no(f"could not run main.py: {exc}")

    output = (finished.stdout + finished.stderr).strip()
    if finished.returncode != 0:
        return no(f"main.py exits {finished.returncode}: {output[-200:]}")
    if "ready" not in finished.stdout:
        return no(f"main.py ran but never printed 'ready': {output[-160:]}")
    if "hello," not in finished.stdout:
        return no("the greeting was removed rather than fixed")
    return ok("main.py starts and prints 'ready'")


# ---------------------------------------------------------------------------
# The suite
# ---------------------------------------------------------------------------

TASKS: tuple[EvalTask, ...] = (
    EvalTask(
        name="documentation_edit",
        summary="Documentation edit",
        request=(
            "Add an 'Installation' section to README.md telling the user to run "
            "`pip install .`. Keep the existing content."
        ),
        files={"README.md": _README, "widget.py": _WIDGET},
    ),
    EvalTask(
        name="single_file_bug_fix",
        summary="Single-file bug fix",
        request=(
            "The add function in calc.py returns the wrong result: add(2, 3) "
            "gives -1 instead of 5. Fix calc.py so it returns the sum."
        ),
        files={"calc.py": _CALC_BROKEN, "test_calc.py": _CALC_TEST},
        frozen=("test_calc.py",),
    ),
    EvalTask(
        name="add_a_unit_test",
        summary="Add one unit test",
        request=(
            "Write a unit test for the slugify function in slug.py. Put it in a "
            "new file test_slug.py. Do not change slug.py."
        ),
        files={"slug.py": _SLUG},
        frozen=("slug.py",),
    ),
    EvalTask(
        name="fix_a_failing_test",
        summary="Fix one failing test",
        request=(
            "The tests in test_temperature.py are failing. Fix temperature.py so "
            "they pass. Do not modify test_temperature.py."
        ),
        files={
            "temperature.py": _TEMPERATURE_BROKEN,
            "test_temperature.py": _TEMPERATURE_TEST,
        },
        frozen=("test_temperature.py",),
    ),
    EvalTask(
        name="multi_file_feature",
        summary="Small multi-file feature",
        request=(
            "Add a subtract(a, b) function to pkg/calc.py that returns a - b, and "
            "export it from pkg/__init__.py so `from pkg import subtract` works."
        ),
        files={"pkg/__init__.py": _PKG_INIT, "pkg/calc.py": _PKG_CALC},
    ),
    EvalTask(
        name="refactor_a_function",
        summary="Refactor one function",
        request=(
            "Refactor the grade function in grade.py so the literal '(pass)' "
            "appears only once in the file. The return values must not change."
        ),
        files={"grade.py": _GRADE, "test_grade.py": _GRADE_TEST},
        frozen=("test_grade.py",),
    ),
    EvalTask(
        name="validation_rule",
        summary="Add one API validation rule",
        request=(
            "charge() in payments.py must raise a ValueError when amount is "
            "negative. Valid amounts must keep returning the same receipt."
        ),
        files={"payments.py": _PAYMENTS, "test_payments.py": _PAYMENTS_TEST},
        frozen=("test_payments.py",),
    ),
    EvalTask(
        name="multi_step_feature",
        summary="Three-part change",
        request=(
            "Give Settings in settings.py a 'level' field defaulting to 'INFO' "
            "and accepted as a keyword argument, and make summary() in app.py "
            "include the level in the line it returns. Leave logging_helpers.py "
            "alone."
        ),
        files={
            "settings.py": _SETTINGS,
            "logging_helpers.py": _LOGGING,
            "app.py": _APP_MAIN,
        },
        frozen=("logging_helpers.py",),
    ),
    EvalTask(
        name="symbol_behind_reexport",
        summary="Symbol behind a re-export",
        request=(
            "format_price shows three decimal places and should show two, so "
            "1234 renders as $12.34. Fix it where it is defined."
        ),
        files={
            "money/__init__.py": _MONEY_INIT,
            "money/_impl.py": _MONEY_IMPL,
            "store/__init__.py": "",
            "store/receipt.py": _RECEIPT,
        },
    ),
    EvalTask(
        name="must_be_wired_in",
        summary="Change that must be wired in",
        request=(
            "Add a 'restart' command to dispatch.py that returns the string "
            "'restarting'. dispatch('restart') must return it. The existing "
            "commands must keep working."
        ),
        files={"dispatch.py": _DISPATCH, "test_dispatch.py": _DISPATCH_TEST},
        frozen=("test_dispatch.py",),
    ),
    EvalTask(
        name="project_must_run",
        summary="Project that has to run",
        request=(
            "Running `python main.py` fails. Fix it so the program starts, "
            "prints a greeting, and then prints 'ready'."
        ),
        files={"main.py": _BROKEN_MAIN, "greeting.py": _GREETING},
    ),
)

CHECKS = {
    "documentation_edit": _check_documentation_edit,
    "single_file_bug_fix": _check_single_file_bug_fix,
    "add_a_unit_test": _check_add_a_unit_test,
    "fix_a_failing_test": _check_fix_a_failing_test,
    "multi_file_feature": _check_multi_file_feature,
    "refactor_a_function": _check_refactor_a_function,
    "validation_rule": _check_validation_rule,
    "multi_step_feature": _check_multi_step_feature,
    "symbol_behind_reexport": _check_symbol_behind_reexport,
    "must_be_wired_in": _check_wired_in,
    "project_must_run": _check_project_runs,
}

BY_NAME = {task.name: task for task in TASKS}


def materialise(task: EvalTask, workspace: Path) -> Path:
    """Write a task's starting repository into `workspace`."""
    workspace.mkdir(parents=True, exist_ok=True)
    for relative, content in task.files.items():
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return workspace


__all__ = [
    "BY_NAME",
    "CHECKS",
    "CHECK_TIMEOUT_SECONDS",
    "TASKS",
    "EvalTask",
    "Outcome",
    "PytestDidNotRun",
    "materialise",
    "no",
    "ok",
    "run_pytest",
    "run_python",
]
