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

    def test_the_refusal_carries_the_file_it_asked_for(self, gateway: ToolGateway) -> None:
        """The refusal runs the errand rather than assigning it.

        It used to say "call file.read on it first", which is a turn spent on
        something the runtime can do itself — and in the §31.1 suite the errand
        did not come back: the run read the file, read it again, and concluded,
        having arrived with a correct anchor on its very first call.
        """
        with pytest.raises(ToolPolicyViolation, match="A small widget library"):
            call(
                gateway,
                "file.patch",
                Phase.AUTHOR,
                path="README.md",
                mode="replace_text",
                find="x",
                replace="y",
            )

    def test_the_call_still_fails(self, gateway: ToolGateway, repo: Path) -> None:
        """Reading it for the model does not make the guessed anchor good."""
        with pytest.raises(ToolPolicyViolation, match="has not been read"):
            call(
                gateway,
                "file.patch",
                Phase.AUTHOR,
                path="README.md",
                mode="replace_text",
                find="x",
                replace="y",
            )
        assert (repo / "README.md").read_text(encoding="utf-8") == README

    def test_the_retry_is_permitted(self, gateway: ToolGateway, repo: Path) -> None:
        """The point of reading it: the next call goes straight through."""
        with pytest.raises(ToolPolicyViolation):
            call(gateway, "file.patch", Phase.AUTHOR, path="README.md", find="x", replace="y")

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

    def test_authoring_a_file_does_not_license_anchoring_in_it(self, gateway: ToolGateway) -> None:
        """Having written a file once is not the same as knowing it now.

        This asserted the opposite until a live build disproved it. The agent
        created `tasks.py` with stubbed methods, and two steps later anchored a
        `replace_text` on `"# Define the TaskList class"` — a line it had never
        written. Its context had rolled over in between, so the authorship
        credit outlived the memory it was standing in for, and the guess was
        let through to fail on a missing anchor.
        """
        assert call(
            gateway, "file.patch", Phase.AUTHOR, path="NOTES.md", mode="create", content="one\n"
        ).ok

        with pytest.raises(ToolPolicyViolation, match="has not been read"):
            call(
                gateway,
                "file.patch",
                Phase.AUTHOR,
                path="NOTES.md",
                mode="replace_text",
                find="one",
                replace="two",
            )

    def test_reading_it_back_restores_the_licence(self, gateway: ToolGateway, repo: Path) -> None:
        """The requirement is a read, and a read satisfies it. Nothing is stuck."""
        assert call(
            gateway, "file.patch", Phase.AUTHOR, path="NOTES.md", mode="create", content="one\n"
        ).ok
        assert call(gateway, "file.read", Phase.AUTHOR, path="NOTES.md").ok

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

    def test_its_own_draft_can_be_rewritten_without_a_read(
        self, gateway: ToolGateway, repo: Path
    ) -> None:
        """The escape hatch that keeps the stricter rule from trapping the agent.

        `create` on a file this run created from nothing is a redraft, not the
        destruction of someone else's work — so it is allowed, and an agent
        that has forgotten its own file is never cornered.
        """
        assert call(
            gateway, "file.patch", Phase.AUTHOR, path="NOTES.md", mode="create", content="one\n"
        ).ok

        result = call(
            gateway, "file.patch", Phase.AUTHOR, path="NOTES.md", mode="create", content="two\n"
        )
        assert result.ok, result.error
        assert (repo / "NOTES.md").read_text(encoding="utf-8") == "two\n"

    def test_a_pre_existing_file_is_still_protected(self, gateway: ToolGateway, repo: Path) -> None:
        """The provenance rule must not become a blanket licence to overwrite.

        `README.md` was in the repo before the run. `create` on it stays
        refused — this is the v1 data-loss case the awkward path exists for.

        A returned failure rather than a raised one: `create` declares no
        `requires_prior_read`, so it never reaches the gateway's policy check,
        and the tool itself decides. The file must be untouched either way.
        """
        result = call(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="README.md",
            mode="create",
            content="gone\n",
        )
        assert result.ok is False
        assert "already exists" in (result.error or "")
        assert (repo / "README.md").read_text(encoding="utf-8") == README

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
