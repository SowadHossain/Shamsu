"""The runtime does its own verification, and never demands the impossible.

Two fixes with one shape: work the runtime can do deterministically should not
be charged to the model's turn budget or its memory.

**The diff review.** `GIT_DIFF_REVIEWED` is half the mandatory floor for every
change step and was earned only by the model remembering to call `git.inspect`.
A live build produced a working `cli.py`, registered `file_changed`, and ended
NOT COMPLETE because both attempts went on a bad anchor and the review call was
never made.

**Producible evidence.** A requirement nothing can satisfy is not a strict
gate. Outside git there is no diff; with no tests there is no suite; and four
evidence kinds have no producing tool at all while the planner's vocabulary
still maps prose onto them.

The property that must survive both: evidence is still registered only from an
observed tool execution. Nothing here lets a model assertion become a row.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from shamsu.interfaces.enums import EvidenceKind, Phase
from shamsu.runtime.session import (
    has_test_suite,
    is_git_repository,
    producible_evidence,
)
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git

pytestmark = pytest.mark.integration


def _repository(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    run_git(root, "config", "user.email", "t@example.com")
    run_git(root, "config", "user.name", "T")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-m", "initial", "--no-verify")
    return root


class TestWhatThisWorkspaceCanProve:
    def test_a_plain_folder_cannot_prove_a_diff(self, tmp_path: Path) -> None:
        gateway = ToolGateway(authoring_tools(tmp_path))
        producible = producible_evidence(gateway, tmp_path)

        assert EvidenceKind.GIT_DIFF_REVIEWED not in producible
        assert EvidenceKind.CHECKPOINT_CREATED not in producible
        assert EvidenceKind.FILE_CHANGED in producible, "patching still works without git"

    def test_a_repository_can(self, tmp_path: Path) -> None:
        _repository(tmp_path)
        gateway = ToolGateway(authoring_tools(tmp_path))
        assert EvidenceKind.GIT_DIFF_REVIEWED in producible_evidence(gateway, tmp_path)

    def test_a_project_with_no_tests_cannot_prove_tests_pass(self, tmp_path: Path) -> None:
        gateway = ToolGateway(authoring_tools(tmp_path))
        assert not has_test_suite(tmp_path)
        assert EvidenceKind.TESTS_PASSED not in producible_evidence(gateway, tmp_path)

    def test_a_project_with_tests_can(self, tmp_path: Path) -> None:
        (tmp_path / "test_thing.py").write_text("def test_x() -> None:\n    assert True\n")
        gateway = ToolGateway(authoring_tools(tmp_path))
        assert EvidenceKind.TESTS_PASSED in producible_evidence(gateway, tmp_path)

    def test_the_four_orphaned_kinds_are_never_producible(self, tmp_path: Path) -> None:
        """No tool produces these anywhere, and the vocabulary still maps onto them.

        This is the test that stops a plan step saying "verify the migration
        applies" and acquiring a requirement the gate can never open.
        """
        _repository(tmp_path)
        (tmp_path / "test_thing.py").write_text("def test_x() -> None:\n    assert True\n")
        gateway = ToolGateway(authoring_tools(tmp_path))
        producible = producible_evidence(gateway, tmp_path)

        for orphan in (
            EvidenceKind.HEALTH_CHECK_PASSED,
            EvidenceKind.SMOKE_TEST_PASSED,
            EvidenceKind.MIGRATION_APPLIED,
            EvidenceKind.SCHEMA_VERIFIED,
        ):
            assert orphan not in producible, f"{orphan.value} has no producing tool"

    def test_lint_and_typecheck_are_producible(self, tmp_path: Path) -> None:
        """check.run closed three of the seven; they must show up as closed."""
        gateway = ToolGateway(authoring_tools(tmp_path))
        producible = producible_evidence(gateway, tmp_path)
        assert EvidenceKind.LINT_PASSED in producible
        assert EvidenceKind.TYPECHECK_PASSED in producible
        assert EvidenceKind.BUILD_SUCCEEDED in producible


class TestTheReviewIsAgainstRealState:
    """`git.inspect` reports success on a clean tree, so the runtime asks git.

    The tool call is what backs the evidence; this is what decides whether
    there was anything to back. Registering on an unchanged tree would attest
    to a change that does not exist — and would hide the case the gate could
    not previously see at all: `file_changed` recorded for a write git cannot
    see, because the path is ignored or the edit was undone.
    """

    def test_a_clean_tree_reports_nothing_changed(self, tmp_path: Path) -> None:
        from shamsu.runtime.session import _porcelain_paths

        _repository(tmp_path)
        assert _porcelain_paths(run_git(tmp_path, "status", "--porcelain").stdout) == ()

    def test_a_modified_file_is_seen(self, tmp_path: Path) -> None:
        from shamsu.runtime.session import _porcelain_paths

        _repository(tmp_path)
        (tmp_path / "seed.txt").write_text("changed\n", encoding="utf-8")
        assert "seed.txt" in _porcelain_paths(run_git(tmp_path, "status", "--porcelain").stdout)

    def test_an_untracked_file_is_seen(self, tmp_path: Path) -> None:
        """The common case: a step that creates a file.

        `git diff` alone shows nothing for an untracked file, so a check built
        on the diff would report "nothing changed" for every new-file step.
        """
        from shamsu.runtime.session import _porcelain_paths

        _repository(tmp_path)
        (tmp_path / "brand_new.py").write_text("x = 1\n", encoding="utf-8")
        assert "brand_new.py" in _porcelain_paths(run_git(tmp_path, "status", "--porcelain").stdout)

    def test_a_rename_names_both_ends(self) -> None:
        from shamsu.runtime.session import _porcelain_paths

        paths = _porcelain_paths("R  old/name.py -> new/name.py\n")
        assert paths == ("old/name.py", "new/name.py")

    def test_quoted_paths_are_unquoted(self) -> None:
        from shamsu.runtime.session import _porcelain_paths

        assert _porcelain_paths('?? "has space.py"\n') == ("has space.py",)


class TestTheEvidenceStaysHonest:
    def test_git_inspect_still_produces_the_evidence_itself(self, tmp_path: Path) -> None:
        """The runtime initiates the call; the tool still earns the evidence.

        If this ever stopped being true the fix would be forging evidence
        rather than producing it.
        """
        _repository(tmp_path)
        (tmp_path / "seed.txt").write_text("changed\n", encoding="utf-8")

        gateway = ToolGateway(authoring_tools(tmp_path))
        from shamsu.interfaces.cancellation import NullCancellationToken
        from shamsu.interfaces.tools import ToolRequest

        result = asyncio.run(
            gateway.invoke(
                ToolRequest(tool="git.inspect", arguments={"include_diff": True}),
                Phase.VERIFY,
                NullCancellationToken(),
            )
        )
        assert result.ok
        assert EvidenceKind.GIT_DIFF_REVIEWED in result.evidence

    def test_is_git_repository_agrees_with_the_tool(self, tmp_path: Path) -> None:
        assert is_git_repository(tmp_path) is False
        _repository(tmp_path)
        assert is_git_repository(tmp_path) is True
