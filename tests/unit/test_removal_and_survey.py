"""Deleting files, and telling the model what is already in the repository.

Two gaps found by asking what a normal developer session needs.

**Nothing could delete anything.** `file.patch` creates, replaces and appends;
"modify, edit, create, delete" was two thirds true. A refactor that splits a
module cannot finish without removing the original, and an agent without the
tool works around it by emptying the file — leaving a zero-byte import target
that still resolves and fails much later.

**The planner could not see the repository.** `FrameInputs` has had an
`artifacts` slot since the layer was written and it is filled zero times in the
execution path; `ArtifactRegistry` is constructed nowhere in `src/`. A live PRD
build put a whole four-feature system into one invented filename, because
nothing in the frame said a project has more than one file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shamsu.interfaces.cancellation import NullCancellationToken
from shamsu.interfaces.enums import EvidenceKind, Phase, Risk
from shamsu.runtime.profile import render_map
from shamsu.tools.editing import FileRemoveInput, FileRemoveTool

MODULE = "def add(a, b):\n    return a + b\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "calc.py").write_text(MODULE, encoding="utf-8")
    (tmp_path / "old.py").write_text("legacy = True\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


def run(tool: FileRemoveTool, **arguments: object) -> object:
    return asyncio.run(tool.run(FileRemoveInput.model_validate(arguments), NullCancellationToken()))


class TestDeleting:
    def test_a_confirmed_delete_removes_the_file(self, workspace: Path) -> None:
        tool = FileRemoveTool(workspace)
        result = run(tool, path="old.py", acknowledge_delete=True)
        assert result.ok, result.error  # type: ignore[attr-defined]
        assert not (workspace / "old.py").exists()

    def test_deleting_without_confirming_is_refused(self) -> None:
        """Same reasoning as `replace_file`: destroying content earns a word."""
        with pytest.raises(ValueError, match="acknowledge_delete"):
            FileRemoveInput(path="old.py")

    def test_a_missing_file_fails_honestly(self, workspace: Path) -> None:
        result = run(FileRemoveTool(workspace), path="nope.py", acknowledge_delete=True)
        assert result.ok is False  # type: ignore[attr-defined]

    def test_a_directory_is_refused(self, workspace: Path) -> None:
        result = run(FileRemoveTool(workspace), path="pkg", acknowledge_delete=True)
        assert result.ok is False  # type: ignore[attr-defined]

    def test_escaping_the_workspace_is_refused(self, workspace: Path) -> None:
        result = run(FileRemoveTool(workspace), path="../outside.py", acknowledge_delete=True)
        assert result.ok is False  # type: ignore[attr-defined]

    def test_a_delete_is_reversible(self, workspace: Path) -> None:
        """`reversible=True` on the contract is a claim the runtime relies on."""
        tool = FileRemoveTool(workspace)
        run(tool, path="calc.py", acknowledge_delete=True)
        assert not (workspace / "calc.py").exists()

        tool.rollback_all()
        assert (workspace / "calc.py").read_text(encoding="utf-8") == MODULE


class TestMoving:
    def test_a_move_needs_no_deletion_confirmation(self, workspace: Path) -> None:
        """Moving preserves the content, so it is not the destructive case."""
        tool = FileRemoveTool(workspace)
        result = run(tool, path="calc.py", destination="pkg/calc.py")
        assert result.ok, result.error  # type: ignore[attr-defined]
        assert not (workspace / "calc.py").exists()
        assert (workspace / "pkg" / "calc.py").read_text(encoding="utf-8") == MODULE

    def test_moving_onto_an_existing_file_is_refused(self, workspace: Path) -> None:
        result = run(FileRemoveTool(workspace), path="calc.py", destination="old.py")
        assert result.ok is False  # type: ignore[attr-defined]
        assert (workspace / "old.py").exists()

    def test_moving_to_the_same_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="nothing to do"):
            FileRemoveInput(path="calc.py", destination="calc.py")

    def test_a_move_is_reversible_in_both_directions(self, workspace: Path) -> None:
        """Rollback unwinds newest first, so the copy goes before the original
        comes back — the other order would leave both files in place."""
        tool = FileRemoveTool(workspace)
        run(tool, path="calc.py", destination="pkg/calc.py")

        tool.rollback_all()
        assert (workspace / "calc.py").read_text(encoding="utf-8") == MODULE
        assert not (workspace / "pkg" / "calc.py").exists()

    def test_both_ends_are_declared_as_writes(self, workspace: Path) -> None:
        """A `WriteScope` has to cover the destination as well as the source."""
        tool = FileRemoveTool(workspace)
        targets = tool.write_targets(FileRemoveInput(path="a.py", destination="b.py"))
        assert set(targets) == {"a.py", "b.py"}


class TestTheContract:
    def test_removal_outranks_editing_in_risk(self) -> None:
        """A bad edit leaves something to read; a bad deletion leaves nothing."""
        assert FileRemoveTool.contract.risk is Risk.HIGH

    def test_it_produces_the_change_evidence(self) -> None:
        assert EvidenceKind.FILE_CHANGED in FileRemoveTool.contract.produces_evidence

    def test_it_is_declared_mutating(self) -> None:
        assert FileRemoveTool.contract.mutating is True

    def test_it_cannot_run_while_inspecting(self) -> None:
        assert Phase.INSPECT not in FileRemoveTool.contract.allowed_phases

    def test_removing_always_requires_a_prior_read(self, workspace: Path) -> None:
        """Stricter than `file.patch`: there is no additive case here."""
        tool = FileRemoveTool(workspace)
        assert tool.requires_prior_read(FileRemoveInput(path="a.py", destination="b.py")) == (
            "a.py",
        )
        assert tool.requires_prior_read(FileRemoveInput(path="a.py", acknowledge_delete=True)) == (
            "a.py",
        )


class TestTheRepositoryMapNamesFiles:
    def test_the_paths_themselves_are_listed(self, workspace: Path) -> None:
        """The generator counts files per directory and names none of them,
        which answers "how big is this" and not "which file do I modify"."""
        rendered = render_map(workspace, use_git=False)
        assert "calc.py" in rendered
        assert "old.py" in rendered

    def test_a_two_file_project_still_says_something(self, tmp_path: Path) -> None:
        """The case that mattered: a greenfield PRD workspace reported only
        "2 indexed files across 1 directories" and named neither."""
        (tmp_path / "PRD.md").write_text("# spec\n", encoding="utf-8")
        assert "PRD.md" in render_map(tmp_path, use_git=False)

    def test_it_is_bounded(self, tmp_path: Path) -> None:
        for index in range(200):
            (tmp_path / f"mod{index}.py").write_text("x = 1\n", encoding="utf-8")
        rendered = render_map(tmp_path, use_git=False, limit=20)
        assert len(rendered.splitlines()) < 60
        assert "more" in rendered

    def test_an_empty_workspace_does_not_crash(self, tmp_path: Path) -> None:
        assert isinstance(render_map(tmp_path, use_git=False), str)
