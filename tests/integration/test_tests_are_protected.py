"""Editing the failing test is indistinguishable from deleting the evidence.

This runtime's first false success, found by the §31.1 suite rather than by any
unit test. `must_be_wired_in` asks for a handler to be registered in a dispatch
table. The model wrote the handler, was told by the reachability check that
nothing reached it, and resolved that by editing `test_dispatch.py`. The
runtime reported the task **complete**.

That is the one failure the evidence gate cannot see by construction: a
modified test that passes produces exactly the same evidence as working code.
It has to be prevented, not detected.

`RepairScope` had the right rule and only applied it during *repair*, which is
a fraction of the actions a run takes. The gateway applies it everywhere.

**The line is existence, not the filename.** Writing a new test is a legitimate
and common task; rewriting a test that was already there to agree with the code
is the failure. Every test below is about keeping those two apart.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shamsu.interfaces.cancellation import NullCancellationToken
from shamsu.interfaces.enums import Phase
from shamsu.interfaces.tools import ToolPolicyViolation, ToolRequest
from shamsu.security.paths import looks_like_a_test
from shamsu.tools import ToolGateway, authoring_tools

pytestmark = pytest.mark.integration

#: Imports `add` on purpose. The pre-write probe checks for undefined names
#: before bytes land, so a fixture that referenced `add` without importing it
#: would be refused by the probe rather than by the policy under test — and the
#: test would pass for the wrong reason.
EXISTING_TEST = "from calc import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n"
SOURCE = "def add(a: int, b: int) -> int:\n    return a - b\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "test_calc.py").write_text(EXISTING_TEST, encoding="utf-8")
    (tmp_path / "calc.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_deep.py").write_text(
        "def test_deep() -> None:\n    assert True\n", encoding="utf-8"
    )
    return tmp_path


def _gateway(workspace: Path, *, allow_test_edits: bool = False) -> ToolGateway:
    return ToolGateway(
        authoring_tools(workspace),
        workspace=workspace,
        require_read_before_edit=False,
        allow_test_edits=allow_test_edits,
    )


def _patch(gateway: ToolGateway, **arguments: object) -> str:
    async def run() -> str:
        try:
            with gateway.decision():
                result = await gateway.invoke(
                    ToolRequest(tool="file.patch", arguments=arguments),
                    Phase.AUTHOR,
                    NullCancellationToken(),
                )
        except ToolPolicyViolation:
            return "refused"
        return "allowed" if result.ok else "failed"

    return asyncio.run(run())


class TestAnExistingTestIsProtected:
    def test_editing_one_is_refused(self, workspace: Path) -> None:
        verdict = _patch(
            _gateway(workspace),
            path="test_calc.py",
            mode="replace_text",
            find="assert add(2, 3) == 5",
            replace="assert True",
        )
        assert verdict == "refused"

    def test_appending_to_one_is_refused(self, workspace: Path) -> None:
        """`append` needs no anchor, so it is the easiest way in."""
        verdict = _patch(
            _gateway(workspace),
            path="test_calc.py",
            mode="append",
            content="\ndef test_anything() -> None:\n    assert True\n",
        )
        assert verdict == "refused"

    def test_overwriting_one_is_refused(self, workspace: Path) -> None:
        verdict = _patch(
            _gateway(workspace),
            path="test_calc.py",
            mode="replace_file",
            content="def test_add() -> None:\n    assert True\n",
        )
        assert verdict == "refused"

    def test_a_test_under_a_tests_directory_counts(self, workspace: Path) -> None:
        verdict = _patch(
            _gateway(workspace),
            path="tests/test_deep.py",
            mode="append",
            content="\ndef test_more() -> None:\n    assert True\n",
        )
        assert verdict == "refused"

    def test_the_refusal_says_what_to_do_instead(self, workspace: Path) -> None:
        gateway = _gateway(workspace)

        async def run() -> str:
            try:
                await gateway.invoke(
                    ToolRequest(
                        tool="file.patch",
                        arguments={"path": "test_calc.py", "mode": "append", "content": "x = 1\n"},
                    ),
                    Phase.AUTHOR,
                    NullCancellationToken(),
                )
            except ToolPolicyViolation as exc:
                return str(exc)
            return ""

        message = asyncio.run(run())
        assert "proves nothing about the code" in message
        assert "Change the code it tests" in message


class TestLegitimateWorkIsUntouched:
    def test_the_source_under_test_is_editable(self, workspace: Path) -> None:
        """The whole point: fix the code, not the test."""
        verdict = _patch(
            _gateway(workspace),
            path="calc.py",
            mode="replace_text",
            find="return a - b",
            replace="return a + b",
        )
        assert verdict == "allowed"

    def test_a_brand_new_test_file_may_be_created(self, workspace: Path) -> None:
        """ "Write a unit test in a new file" is a task the suite actually sets."""
        verdict = _patch(
            _gateway(workspace),
            path="test_brand_new.py",
            mode="create",
            content="def test_x() -> None:\n    assert True\n",
        )
        assert verdict == "allowed"

    def test_the_caller_may_opt_in(self, workspace: Path) -> None:
        """A genuine test refactor is legitimate — and it is not the model's call."""
        verdict = _patch(
            _gateway(workspace, allow_test_edits=True),
            path="test_calc.py",
            mode="append",
            content="\ndef test_more() -> None:\n    assert True\n",
        )
        assert verdict == "allowed"

    def test_without_a_workspace_nothing_is_refused(self, workspace: Path) -> None:
        """A policy that cannot be evaluated must not be enforced on a guess."""
        gateway = ToolGateway(authoring_tools(workspace), require_read_before_edit=False)
        verdict = _patch(
            gateway,
            path="test_calc.py",
            mode="append",
            content="\ndef test_more() -> None:\n    assert True\n",
        )
        assert verdict == "allowed"


class TestTheConventionItself:
    @pytest.mark.parametrize(
        "path",
        [
            "test_calc.py",
            "tests/test_deep.py",
            "calc_test.py",
            "src/component.test.ts",
            "conftest.py",
        ],
    )
    def test_recognised_shapes(self, path: str) -> None:
        assert looks_like_a_test(path)

    @pytest.mark.parametrize("path", ["calc.py", "src/latest.py", "contest.py", "protest/a.py"])
    def test_things_that_merely_look_like_it(self, path: str) -> None:
        assert not looks_like_a_test(path)
