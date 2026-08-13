"""A file must be read before it can be edited.

The invariant every editor a user is accustomed to enforces, and the one SHAMSU
lacked. `file.patch` matches an anchor the *model* supplies, and a small model
routinely supplies one it never saw: at best the exact match fails and the turn
is wasted, at worst it matches the wrong span silently.

Both halves of that were measured. A §31.1 "add an Installation section to
README.md" run opened with `file.patch` on a guessed anchor, failed, and never
recovered — two of seven tasks died that way. And little-coder's
`read-guard-edit` extension exists for the identical reason, arrived at
independently.

Enforced in the gateway rather than in the tool, because the tool that reads is
not the tool that edits and only the gateway sees both.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from shamsu.interfaces.cancellation import NullCancellationToken
from shamsu.interfaces.enums import Phase
from shamsu.interfaces.tools import ToolPolicyViolation, ToolRequest, ToolResult
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git

pytestmark = pytest.mark.integration

README = "# widget\n\nA small widget library.\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text(README, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    run_git(root, "config", "user.email", "a@b.c")
    run_git(root, "config", "user.name", "SHAMSU")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-m", "initial", "--no-verify")
    return root


@pytest.fixture
def gateway(repo: Path) -> ToolGateway:
    """The production wiring: the invariant is on by default."""
    return ToolGateway(authoring_tools(repo))


def call(gateway: ToolGateway, tool: str, phase: Phase, **arguments: object) -> ToolResult:
    with gateway.decision():
        return asyncio.run(
            gateway.invoke(
                ToolRequest(tool=tool, arguments=arguments), phase, NullCancellationToken()
            )
        )


class TestAnUnreadFileCannotBeEdited:
    def test_a_blind_patch_is_refused_before_it_runs(
        self, gateway: ToolGateway, repo: Path
    ) -> None:
        """Refused *before* execution: a guess costs a turn, not a file."""
        with pytest.raises(ToolPolicyViolation, match="has not been read"):
            call(
                gateway,
                "file.patch",
                Phase.AUTHOR,
                path="README.md",
                mode="replace_text",
                find="A small widget library.",
                replace="Something else.",
            )

        assert (repo / "README.md").read_text(encoding="utf-8") == README

    def test_the_refusal_says_what_to_do(self, gateway: ToolGateway) -> None:
        with pytest.raises(ToolPolicyViolation, match="Call file.read on it first"):
            call(
                gateway,
                "file.patch",
                Phase.AUTHOR,
                path="README.md",
                mode="replace_text",
                find="x",
                replace="y",
            )

    def test_reading_first_permits_the_edit(self, gateway: ToolGateway, repo: Path) -> None:
        assert call(gateway, "file.read", Phase.AUTHOR, path="README.md").ok

        result = call(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="README.md",
            mode="replace_text",
            find="A small widget library.",
            replace="A small widget library.\n\n## Installation\n\npip install .",
        )
        assert result.ok, result.error
        assert "## Installation" in (repo / "README.md").read_text(encoding="utf-8")

    def test_a_failed_read_does_not_license_an_edit(self, gateway: ToolGateway) -> None:
        """A read that failed revealed nothing."""
        assert call(gateway, "file.read", Phase.AUTHOR, path="nope.md").ok is False

        with pytest.raises(ToolPolicyViolation, match="has not been read"):
            call(
                gateway,
                "file.patch",
                Phase.AUTHOR,
                path="nope.md",
                mode="replace_text",
                find="x",
                replace="y",
            )

    def test_reading_one_file_does_not_license_editing_another(
        self, gateway: ToolGateway, repo: Path
    ) -> None:
        (repo / "other.md").write_text("other\n", encoding="utf-8")
        assert call(gateway, "file.read", Phase.AUTHOR, path="README.md").ok

        with pytest.raises(ToolPolicyViolation, match="other.md has not been read"):
            call(
                gateway,
                "file.patch",
                Phase.AUTHOR,
                path="other.md",
                mode="replace_text",
                find="other",
                replace="changed",
            )


class TestCreatingIsNotEditing:
    def test_a_new_file_needs_no_prior_read(self, gateway: ToolGateway, repo: Path) -> None:
        """Demanding a read of a file that does not exist asks the impossible."""
        result = call(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="NOTES.md",
            mode="create",
            content="notes\n",
        )
        assert result.ok, result.error
        assert (repo / "NOTES.md").read_text(encoding="utf-8") == "notes\n"

    def test_authoring_a_file_licenses_editing_it(self, gateway: ToolGateway, repo: Path) -> None:
        """The model supplied the contents, so it knows them."""
        assert call(
            gateway, "file.patch", Phase.AUTHOR, path="NOTES.md", mode="create", content="one\n"
        ).ok

        result = call(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="NOTES.md",
            mode="replace_text",
            find="one",
            replace="two",
        )
        assert result.ok, result.error
        assert (repo / "NOTES.md").read_text(encoding="utf-8") == "two\n"

    def test_a_wholesale_overwrite_still_needs_a_read(self, gateway: ToolGateway) -> None:
        """`replace_file` discards content, which is the case for knowing it."""
        with pytest.raises(ToolPolicyViolation, match="has not been read"):
            call(
                gateway,
                "file.patch",
                Phase.AUTHOR,
                path="README.md",
                mode="replace_file",
                content="gone\n",
                acknowledge_overwrite=True,
            )


class TestThePolicyIsOptional:
    def test_it_can_be_turned_off_for_tool_level_tests(self, repo: Path) -> None:
        """Same opt-out `WriteScope` and `approval` have, for the same reason."""
        relaxed = ToolGateway(authoring_tools(repo), require_read_before_edit=False)
        result = call(
            relaxed,
            "file.patch",
            Phase.AUTHOR,
            path="README.md",
            mode="replace_text",
            find="A small widget library.",
            replace="Changed.",
        )
        assert result.ok, result.error

    def test_it_is_on_by_default(self, repo: Path) -> None:
        """The production wiring must not have to remember to ask for it."""
        with pytest.raises(ToolPolicyViolation):
            call(
                ToolGateway(authoring_tools(repo)),
                "file.patch",
                Phase.AUTHOR,
                path="README.md",
                mode="replace_text",
                find="A small widget library.",
                replace="Changed.",
            )
