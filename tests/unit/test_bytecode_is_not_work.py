"""Compiled bytecode must not count as a step's change.

`_review_changes` asks git what moved and grants `GIT_DIFF_REVIEWED` on the
answer. In a Django project with no `.gitignore`, the first `manage.py check`
leaves `marketplace/__pycache__/models.cpython-313.pyc` in the tree, and from
then on every step's diff review reported it:

    git.inspect  also changed: db.sqlite3,
                 marketplace/__pycache__/models.cpython-313.pyc,
                 marketplace/__pycache__/services.cpython-313.pyc

Bytecode is not something a step is ever asked to write, so crediting a step
for it can only ever be wrong — the same argument that already excludes
`.shamsu/`, and a stronger one, because `.shamsu/` at least holds real state.
"""

from __future__ import annotations

import pytest

from shamsu.runtime.session import _is_agent_state


class TestBookkeepingAndBuildOutputAreExcluded:
    @pytest.mark.parametrize(
        "path",
        [
            ".shamsu",
            ".shamsu/state.db",
            ".shamsu/artifacts/modules/x.json",
            "__pycache__/models.cpython-313.pyc",
            "marketplace/__pycache__/models.cpython-313.pyc",
            "a/b/c/__pycache__/deep.pyc",
            ".pytest_cache/v/cache/lastfailed",
            ".mypy_cache/3.13/builtins.data.json",
            ".ruff_cache/content",
        ],
    )
    def test_it_is_not_a_project_change(self, path: str) -> None:
        assert _is_agent_state(path) is True, path

    def test_windows_separators_are_handled(self) -> None:
        """git reports forward slashes, but the same helper normalises paths."""
        assert _is_agent_state("marketplace\\__pycache__\\models.cpython-313.pyc") is True


class TestRealWorkIsStillSeen:
    @pytest.mark.parametrize(
        "path",
        [
            "marketplace/models.py",
            "manage.py",
            ".github/workflows/ci.yml",
            ".gitignore",
            "db.sqlite3",
            "docs/__pycache__notes.md",
            "src/cache/handler.py",
        ],
    )
    def test_it_counts_as_a_change(self, path: str) -> None:
        assert _is_agent_state(path) is False, path

    def test_a_dot_directory_is_not_excluded_wholesale(self) -> None:
        """Editing a CI workflow is work a step can legitimately be given."""
        assert _is_agent_state(".github/workflows/release.yml") is False

    def test_a_file_merely_named_like_a_cache_is_work(self) -> None:
        assert _is_agent_state("app/pycache_helpers.py") is False
