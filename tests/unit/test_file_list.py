"""`file.list` — the directory listing the read-only surface was missing."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shamsu.interfaces.cancellation import NullCancellationToken
from shamsu.interfaces.enums import Phase
from shamsu.interfaces.tools import ToolPolicyViolation, ToolResult
from shamsu.tools.readonly import FileListTool


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "src" / "deep" / "nested.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hello\n", encoding="utf-8")

    # Noise that must never appear in a listing.
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "app.cpython-313.pyc").write_bytes(b"\x00")
    (tmp_path / "node_modules" / "left-pad").mkdir(parents=True)
    (tmp_path / "node_modules" / "left-pad" / "index.js").write_text("", encoding="utf-8")
    return tmp_path


def listing(workspace: Path, **arguments: object) -> ToolResult:
    tool = FileListTool(workspace)
    return asyncio.run(tool.run(tool.parse(arguments), NullCancellationToken()))


class TestListing:
    def test_lists_the_workspace_root(self, workspace: Path) -> None:
        result = listing(workspace)
        assert result.ok
        assert "README.md" in result.output
        assert "src/" in result.output

    def test_directories_come_before_files(self, workspace: Path) -> None:
        body = listing(workspace).output
        assert body.index("src/") < body.index("README.md")

    def test_depth_one_does_not_descend(self, workspace: Path) -> None:
        assert "nested.py" not in listing(workspace).output

    def test_depth_three_descends(self, workspace: Path) -> None:
        assert "nested.py" in listing(workspace, depth=3).output

    def test_ignored_trees_are_pruned(self, workspace: Path) -> None:
        body = listing(workspace, depth=3).output
        assert "__pycache__" not in body
        assert "node_modules" not in body
        assert "left-pad" not in body

    def test_file_sizes_are_shown(self, workspace: Path) -> None:
        assert " B)" in listing(workspace).output

    def test_an_empty_directory_says_so(self, tmp_path: Path) -> None:
        (tmp_path / "blank").mkdir()
        result = listing(tmp_path, path="blank")
        assert result.ok
        assert "empty" in result.output


class TestRefusals:
    def test_a_missing_directory_fails_honestly(self, workspace: Path) -> None:
        result = listing(workspace, path="nope")
        assert not result.ok
        assert "no such directory" in (result.error or "")

    def test_a_file_is_redirected_to_file_read(self, workspace: Path) -> None:
        result = listing(workspace, path="README.md")
        assert not result.ok
        assert "file.read" in (result.error or "")

    def test_escaping_the_workspace_is_refused(self, workspace: Path) -> None:
        result = listing(workspace, path="../..")
        assert not result.ok

    def test_an_absurd_depth_is_refused_at_the_schema(self, workspace: Path) -> None:
        with pytest.raises(ToolPolicyViolation):
            FileListTool(workspace).parse({"depth": 99})

    def test_the_entry_cap_is_reported_not_silent(self, tmp_path: Path) -> None:
        for index in range(30):
            (tmp_path / f"file{index:02d}.txt").write_text("x", encoding="utf-8")
        result = listing(tmp_path, max_entries=5)
        assert result.ok
        assert "capped" in result.output


class TestPolicy:
    def test_it_is_read_only_and_produces_no_evidence(self) -> None:
        contract = FileListTool.contract
        assert not contract.mutating
        assert not contract.produces_evidence

    def test_it_is_available_wherever_reading_is(self) -> None:
        assert Phase.INSPECT in FileListTool.contract.allowed_phases
        assert Phase.AUTHOR in FileListTool.contract.allowed_phases
