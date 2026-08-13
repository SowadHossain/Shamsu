"""`check.run` — lint, type-check, and build, and the evidence each one earns.

The property under test that matters most is the *narrowing*: a successful
`ruff` run must register `lint_passed` and nothing else. A tool that granted
its whole contract per call would let one lint run satisfy a step that required
a build, which is exactly the forgery the evidence architecture exists to stop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shamsu.interfaces.cancellation import NullCancellationToken
from shamsu.interfaces.enums import EvidenceKind, Phase
from shamsu.interfaces.tools import ToolResult
from shamsu.tools.checks import Check, CheckRunTool

#: A check that always passes, and one that always fails, without needing any
#: linter to be installed. `sys.executable -c` is available by construction.
PASSES = Check(
    argv=("python", "-c", "print('clean')"),
    evidence=EvidenceKind.LINT_PASSED,
    describes="fake lint",
)
FAILS = Check(
    argv=("python", "-c", "import sys; print('E501 line too long'); sys.exit(1)"),
    evidence=EvidenceKind.LINT_PASSED,
    describes="fake lint",
)
BUILDS = Check(
    argv=("python", "-c", "print('built')"),
    evidence=EvidenceKind.BUILD_SUCCEEDED,
    describes="fake build",
    accepts_target=False,
)
MISSING = Check(
    argv=("shamsu-no-such-executable",),
    evidence=EvidenceKind.TYPECHECK_PASSED,
    describes="fake typecheck",
)


def run(workspace: Path, command: str, **arguments: object) -> ToolResult:
    import sys

    # `python` is not on PATH on every Windows box; bind the argv to the
    # interpreter running the tests, exactly as the real defaults do.
    def bind(check: Check) -> Check:
        if check.argv[0] != "python":
            return check
        return Check(
            argv=(sys.executable, *check.argv[1:]),
            evidence=check.evidence,
            describes=check.describes,
            accepts_target=check.accepts_target,
        )

    tool = CheckRunTool(
        workspace,
        checks={
            "passes": bind(PASSES),
            "fails": bind(FAILS),
            "builds": bind(BUILDS),
            "missing": MISSING,
        },
    )
    return asyncio.run(
        tool.run(tool.parse({"command": command, **arguments}), NullCancellationToken())
    )


class TestEvidenceIsNarrowedPerCommand:
    def test_a_passing_lint_earns_only_lint_passed(self, tmp_path: Path) -> None:
        result = run(tmp_path, "passes")
        assert result.ok
        assert result.evidence == frozenset({EvidenceKind.LINT_PASSED})

    def test_a_passing_build_earns_only_build_succeeded(self, tmp_path: Path) -> None:
        result = run(tmp_path, "builds")
        assert result.ok
        assert result.evidence == frozenset({EvidenceKind.BUILD_SUCCEEDED})

    def test_a_failing_check_earns_nothing(self, tmp_path: Path) -> None:
        result = run(tmp_path, "fails")
        assert not result.ok
        assert not result.evidence

    def test_the_contract_declares_the_union(self) -> None:
        assert CheckRunTool.contract.produces_evidence == frozenset(
            {
                EvidenceKind.LINT_PASSED,
                EvidenceKind.TYPECHECK_PASSED,
                EvidenceKind.BUILD_SUCCEEDED,
            }
        )

    def test_a_tool_cannot_grant_undeclared_evidence(self, tmp_path: Path) -> None:
        """The subset rule is enforced, not merely documented."""
        tool = CheckRunTool(tmp_path)
        with pytest.raises(ValueError, match="does not declare"):
            tool.ok("fine", evidence={EvidenceKind.TESTS_PASSED})


class TestReporting:
    def test_failure_output_reaches_the_agent(self, tmp_path: Path) -> None:
        result = run(tmp_path, "fails")
        assert "E501 line too long" in (result.error or "")

    def test_a_missing_executable_is_an_environment_problem(self, tmp_path: Path) -> None:
        result = run(tmp_path, "missing")
        assert not result.ok
        assert "not installed" in (result.error or "")
        assert "environment problem" in (result.error or "")

    def test_an_unknown_command_lists_the_real_ones(self, tmp_path: Path) -> None:
        result = run(tmp_path, "definitely-not-a-check")
        assert not result.ok
        assert "available:" in (result.error or "")
        assert "passes" in (result.error or "")

    def test_a_whole_project_check_refuses_a_target(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        result = run(tmp_path, "builds", target="app.py")
        assert not result.ok
        assert "takes no target" in (result.error or "")

    def test_a_missing_target_fails_before_running(self, tmp_path: Path) -> None:
        result = run(tmp_path, "passes", target="nope.py")
        assert not result.ok
        assert "no such file" in (result.error or "")

    def test_escaping_the_workspace_is_refused(self, tmp_path: Path) -> None:
        result = run(tmp_path, "passes", target="../../etc/passwd")
        assert not result.ok


class TestPolicy:
    def test_it_cannot_run_while_authoring(self) -> None:
        """Checking during AUTHOR would let a step verify an unfinished edit."""
        assert Phase.AUTHOR not in CheckRunTool.contract.allowed_phases
        assert Phase.VERIFY in CheckRunTool.contract.allowed_phases
        assert Phase.REPAIR in CheckRunTool.contract.allowed_phases

    def test_it_does_not_write(self) -> None:
        assert not CheckRunTool.contract.mutating

    def test_every_default_check_declares_producible_evidence(self) -> None:
        from shamsu.tools.checks import DEFAULT_CHECKS

        for name, check in DEFAULT_CHECKS.items():
            assert check.evidence in CheckRunTool.contract.produces_evidence, name
