"""Tests for the verify gate (shamsu/verify/gate.py) and its honesty wiring into
the autonomous chat loop. The gate's contract: never report success unless the
change was verified, or is explicitly unverifiable."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shamsu.agents import chat_loop as chat_loop_module
from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.verify.gate import (
    VerifyOutcome,
    acceptance_checks_from_criteria,
    acceptance_commands_from_criteria,
    build_verification_plan,
    default_verify_command,
    stack_of,
    verify_and_repair,
    verify_only,
)


class _FakeRunner:
    """Records commands and returns scripted (exit, out, err) tuples."""

    def __init__(self, *results: tuple[int, str, str]) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, Path]] = []

    def run(self, command: str, cwd: Path) -> tuple[int, str, str]:
        self.calls.append((command, cwd))
        return self._results.pop(0) if self._results else (0, "", "")


# ---------------------------------------------------------------------------
# command selection
# ---------------------------------------------------------------------------


def test_default_verify_command_node():
    assert default_verify_command(["package.json", "src/main.ts"]) == "npm install && npm run build"


def test_default_verify_command_node_lightweight_is_unverifiable():
    # A node build is too heavy to run automatically on an interactive turn.
    assert default_verify_command(["package.json"], lightweight=True) == ""


def test_default_verify_command_python_lightweight_drops_install():
    cmd = default_verify_command(["app.py", "requirements.txt"], stack="python", lightweight=True)
    assert "pip install" not in cmd
    assert "py_compile app.py" in cmd


def test_default_verify_command_unknown_is_empty():
    assert default_verify_command(["notes.txt"]) == ""


def test_stack_of():
    assert stack_of(["package.json"]) == "node"
    assert stack_of(["src/App.tsx"]) == "node"
    assert stack_of(["project/manage.py"]) == "django"
    assert stack_of(["a.py"]) == "python"
    assert stack_of(["README.md"]) == ""


def test_acceptance_commands_only_extracts_executable_first_backtick():
    criteria = [
        "`npm test -- --run` exits 0.",
        "`python app.py seed` prints `seeded 4 rows`.",
        "`report.csv` exists after export.",
        "Run `npm test -- --run` again.",
    ]
    commands = acceptance_commands_from_criteria(criteria)
    checks = acceptance_checks_from_criteria(criteria)

    assert commands == ("npm test -- --run", "python app.py seed")
    assert checks[0].stdout_contains == ()
    assert checks[1].stdout_contains == ("seeded 4 rows",)


def test_node_plan_orders_build_test_and_optional_lint(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "build": "vite build",
                    "test": "vitest",
                    "lint": "eslint .",
                }
            }
        ),
        encoding="utf-8",
    )

    plan = build_verification_plan(
        tmp_path,
        ["package.json", "src/App.tsx"],
        lightweight=False,
        acceptance_commands=["node scripts/status.mjs"],
    )

    assert [step.stage for step in plan.steps] == [
        "setup",
        "build",
        "test",
        "lint",
        "acceptance",
    ]
    assert [step.command for step in plan.steps] == [
        "npm install",
        "npm run build",
        "npm test -- --run",
        "npm run lint",
        "node scripts/status.mjs",
    ]
    assert plan.steps[3].required is False
    assert all(step.cwd == tmp_path for step in plan.steps)


def test_node_placeholder_test_script_is_not_a_gate(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "build": "tsc",
                    "test": 'echo "Error: no test specified" && exit 1',
                }
            }
        ),
        encoding="utf-8",
    )

    plan = build_verification_plan(tmp_path, ["package.json"], lightweight=False)

    assert [step.stage for step in plan.steps] == ["setup", "build"]


def test_python_plan_discovers_setup_syntax_tests_and_lint(tmp_path: Path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("pytest\nruff\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("def test_ok(): assert True\n", encoding="utf-8")

    plan = build_verification_plan(
        tmp_path,
        ["app.py", "requirements.txt"],
        python_bin="python",
        lightweight=False,
    )

    assert [step.stage for step in plan.steps] == ["setup", "syntax", "test", "lint"]
    assert [step.command for step in plan.steps] == [
        "python -m pip install -r requirements.txt",
        "python -m py_compile app.py",
        "python -m pytest",
        "python -m ruff check .",
    ]
    assert plan.steps[-1].required is False


def test_django_plan_uses_manage_py_test_from_nested_project(tmp_path: Path):
    project = tmp_path / "backend"
    project.mkdir()
    (project / "manage.py").write_text("print('manage')\n", encoding="utf-8")
    (project / "views.py").write_text("VALUE = 1\n", encoding="utf-8")

    plan = build_verification_plan(
        tmp_path,
        ["backend/manage.py", "backend/views.py"],
        stack="django",
        python_bin="python",
    )

    assert [step.command for step in plan.steps] == [
        "python -m py_compile manage.py views.py",
        "python manage.py test",
    ]
    assert all(step.cwd == project for step in plan.steps)


# ---------------------------------------------------------------------------
# verify_only
# ---------------------------------------------------------------------------


def test_verify_only_verified(tmp_path: Path):
    runner = _FakeRunner((0, "", ""))
    outcome = verify_only(tmp_path, ["a.py"], command_runner=runner)
    assert outcome.verified is True
    assert outcome.unverifiable is False
    assert outcome.failed is False
    assert outcome.status() == "verified"
    assert "py_compile a.py" in runner.calls[0][0]


def test_verify_only_failed(tmp_path: Path):
    runner = _FakeRunner((1, "", "SyntaxError: bad"))
    outcome = verify_only(tmp_path, ["a.py"], command_runner=runner)
    assert outcome.verified is False
    assert outcome.unverifiable is False
    assert outcome.failed is True
    assert outcome.status() == "failed"
    assert outcome.exit_code == 1


def test_verify_only_unverifiable_does_not_run_anything(tmp_path: Path):
    runner = _FakeRunner((0, "", ""))
    outcome = verify_only(tmp_path, ["notes.txt"], command_runner=runner)
    assert outcome.unverifiable is True
    assert outcome.verified is False
    assert outcome.failed is False
    assert runner.calls == []  # no verifier available -> nothing executed


def test_verify_only_required_test_failure_blocks_acceptance(tmp_path: Path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("def test_no(): assert False\n", encoding="utf-8")
    runner = _FakeRunner((0, "", ""), (1, "", "1 failed"))

    outcome = verify_only(
        tmp_path,
        ["app.py"],
        command_runner=runner,
        acceptance_commands=["python app.py --smoke"],
    )

    assert outcome.failed is True
    assert outcome.failed_step is not None
    assert outcome.failed_step.stage == "test"
    assert [command for command, _cwd in runner.calls] == [
        outcome.steps[0].step.command,
        outcome.steps[1].step.command,
    ]
    assert "acceptance" not in [result.step.stage for result in outcome.steps]


def test_verify_only_optional_lint_failure_does_not_hide_acceptance(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "build": "vite build",
                    "test": "vitest run",
                    "lint": "eslint .",
                }
            }
        ),
        encoding="utf-8",
    )
    runner = _FakeRunner(
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        (1, "", "lint debt"),
        (0, "", ""),
    )

    outcome = verify_only(
        tmp_path,
        ["package.json"],
        command_runner=runner,
        lightweight=False,
        acceptance_commands=["node smoke.mjs"],
    )

    assert outcome.verified is True
    assert len(outcome.steps) == 5
    assert outcome.steps[3].step.stage == "lint"
    assert outcome.steps[3].passed is False
    assert outcome.steps[4].step.stage == "acceptance"
    assert "warning" in outcome.summary.lower()


def test_verify_only_acceptance_output_is_required_evidence(tmp_path: Path):
    (tmp_path / "app.py").write_text("print('wrong')\n", encoding="utf-8")
    runner = _FakeRunner((0, "", ""), (0, "wrong\n", ""))
    check = acceptance_checks_from_criteria(
        ["`python app.py` prints `expected output`."]
    )[0]

    outcome = verify_only(
        tmp_path,
        ["app.py"],
        command_runner=runner,
        acceptance_commands=[check],
    )

    assert outcome.failed is True
    assert outcome.failed_step is not None
    assert outcome.failed_step.stage == "acceptance"
    assert outcome.steps[-1].exit_code == 1
    assert "Acceptance output missing" in outcome.steps[-1].stderr


# ---------------------------------------------------------------------------
# verify_and_repair
# ---------------------------------------------------------------------------


def test_verify_and_repair_verified_never_calls_generate(tmp_path: Path):
    runner = _FakeRunner((0, "", ""))

    def _generate(system: str, user: str, schema: dict) -> str:  # pragma: no cover - must not run
        raise AssertionError("generate must not be called when verification already passes")

    outcome = verify_and_repair(tmp_path, ["a.py"], generate=_generate, command_runner=runner)
    assert outcome.verified is True
    assert outcome.exit_code == 0


def test_verify_and_repair_unverifiable(tmp_path: Path):
    called = {"generate": False}

    def _generate(system: str, user: str, schema: dict) -> str:
        called["generate"] = True
        return "{}"

    outcome = verify_and_repair(tmp_path, ["notes.txt"], generate=_generate)
    assert outcome.unverifiable is True
    assert called["generate"] is False


def test_verify_and_repair_reruns_whole_plan_after_test_recovers(tmp_path: Path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    runner = _FakeRunner(
        (0, "", ""),
        (1, "", "transient test failure"),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
    )

    def _generate(system: str, user: str, schema: dict) -> str:  # pragma: no cover
        raise AssertionError("a recovered verifier must not request a model edit")

    outcome = verify_and_repair(
        tmp_path,
        ["app.py"],
        generate=_generate,
        command_runner=runner,
    )

    assert outcome.verified is True
    assert outcome.repair_result is not None
    assert [command for command, _cwd in runner.calls].count(
        outcome.steps[-1].step.command
    ) == 3


def test_verify_outcome_status_values():
    assert VerifyOutcome(verified=True).status() == "verified"
    assert VerifyOutcome(verified=False, unverifiable=True).status() == "unverifiable"
    assert VerifyOutcome(verified=False).status() == "failed"
    assert VerifyOutcome(verified=False).failed is True


# ---------------------------------------------------------------------------
# chat-loop honesty wiring (_maybe_verify)
# ---------------------------------------------------------------------------


class _DummyLLM:
    async def run_specialist(self, specialist, pack):  # noqa: ANN001
        raise RuntimeError("LLM should not be used in _maybe_verify tests")


def _loop(tmp_path: Path, *, long_running: bool) -> AgentChatLoop:
    return AgentChatLoop(
        tmp_path,
        client=object(),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=_DummyLLM(),
        long_running=long_running,
    )


@pytest.mark.asyncio
async def test_maybe_verify_appends_failure_note(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        chat_loop_module,
        "verify_only",
        lambda *a, **k: VerifyOutcome(
            verified=False, exit_code=1, command="python -m py_compile a.py",
            summary="Verification FAILED: `python -m py_compile a.py` (exit 1).",
        ),
    )
    loop = _loop(tmp_path, long_running=True)
    final = await loop._maybe_verify("Done, all fixed.", ["a.py"])
    assert "UNCONFIRMED" in final
    assert "Done, all fixed." in final


@pytest.mark.asyncio
async def test_maybe_verify_appends_verified_note(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        chat_loop_module,
        "verify_only",
        lambda *a, **k: VerifyOutcome(
            verified=True, exit_code=0, command="python -m py_compile a.py",
            summary="Verification passed: `python -m py_compile a.py` (exit 0).",
        ),
    )
    loop = _loop(tmp_path, long_running=True)
    final = await loop._maybe_verify("Done.", ["a.py"])
    assert "[verified]" in final


@pytest.mark.asyncio
async def test_maybe_verify_unverifiable_leaves_answer_untouched(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        chat_loop_module,
        "verify_only",
        lambda *a, **k: VerifyOutcome(verified=False, unverifiable=True, summary="UNVERIFIED"),
    )
    loop = _loop(tmp_path, long_running=True)
    final = await loop._maybe_verify("Here is the answer.", ["notes.txt"])
    assert final == "Here is the answer."


@pytest.mark.asyncio
async def test_maybe_verify_runs_lightweight_check_when_not_long_running(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        chat_loop_module,
        "verify_only",
        lambda *a, **k: VerifyOutcome(
            verified=True,
            exit_code=0,
            command="python -m py_compile a.py",
            summary="Verification passed.",
        ),
    )
    loop = _loop(tmp_path, long_running=False)
    final = await loop._maybe_verify("Answer.", ["a.py"])
    assert "[verified]" in final


@pytest.mark.asyncio
async def test_maybe_verify_skips_when_no_writes(tmp_path: Path, monkeypatch):
    def _boom(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("verify must not run when nothing was written")

    monkeypatch.setattr(chat_loop_module, "verify_only", _boom)
    loop = _loop(tmp_path, long_running=True)
    final = await loop._maybe_verify("Answer.", [])
    assert final == "Answer."
