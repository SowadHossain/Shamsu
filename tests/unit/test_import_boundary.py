"""The legacy boundary must be mechanically enforced, not merely documented.

Plan section 8.1: production v2 code must not import from `legacy-code/`, and
that directory must not be on the production Python path. These tests check
both the rule and the checker -- a boundary check that cannot fail is not a
boundary check.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import check_import_boundary as boundary  # noqa: E402


class TestRealRepository:
    def test_src_tree_is_clean(self, repo_root: Path) -> None:
        assert boundary.main(["--root", str(repo_root)]) == 0

    def test_runs_as_a_script(self, repo_root: Path) -> None:
        """CI invokes this as a subprocess, so the entry point must work too."""
        result = subprocess.run(
            [sys.executable, "scripts/check_import_boundary.py", "--root", "."],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr

    def test_legacy_package_is_not_importable(self) -> None:
        """The archive must not be reachable through normal imports."""
        with pytest.raises(ModuleNotFoundError):
            __import__("legacy_code")

    def test_archive_is_not_on_sys_path(self) -> None:
        assert not [entry for entry in sys.path if "legacy-code" in entry]


class TestCheckerCatchesViolations:
    """Negative cases. Each writes a fake src/ tree and asserts a failure."""

    @staticmethod
    def _tree(tmp_path: Path, source: str) -> Path:
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        (src / "module.py").write_text(source, encoding="utf-8")
        return tmp_path

    def test_detects_plain_import(self, tmp_path: Path) -> None:
        root = self._tree(tmp_path, "import legacy_code\n")
        assert boundary.main(["--root", str(root)]) == 1

    def test_detects_from_import(self, tmp_path: Path) -> None:
        root = self._tree(tmp_path, "from legacy_code.shamsu.agents import chat_loop\n")
        assert boundary.main(["--root", str(root)]) == 1

    def test_detects_submodule_import(self, tmp_path: Path) -> None:
        root = self._tree(tmp_path, "import legacy_code.shamsu.safety as safety\n")
        assert boundary.main(["--root", str(root)]) == 1

    def test_detects_path_literal(self, tmp_path: Path) -> None:
        """A hard-coded path escapes the boundary without ever being an import."""
        root = self._tree(tmp_path, 'PATH = "legacy-code/shamsu/llm/output.py"\n')
        assert boundary.main(["--root", str(root)]) == 1

    def test_detects_sys_path_injection(self, tmp_path: Path) -> None:
        root = self._tree(
            tmp_path,
            "import sys\nsys.path.insert(0, '../legacy-code')\n",
        )
        assert boundary.main(["--root", str(root)]) == 1

    def test_detects_importlib_indirection(self, tmp_path: Path) -> None:
        root = self._tree(
            tmp_path,
            "import importlib\nm = importlib.import_module('legacy_code.shamsu')\n",
        )
        assert boundary.main(["--root", str(root)]) == 1


class TestCheckerAllowsLegitimateProse:
    """Documentation about the boundary must not trip the boundary."""

    @staticmethod
    def _tree(tmp_path: Path, source: str) -> Path:
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        (src / "module.py").write_text(source, encoding="utf-8")
        return tmp_path

    def test_module_docstring_may_mention_the_archive(self, tmp_path: Path) -> None:
        root = self._tree(
            tmp_path,
            '"""See legacy-code/LEGACY_README.md for why this was rewritten."""\n',
        )
        assert boundary.main(["--root", str(root)]) == 0

    def test_function_docstring_may_mention_the_archive(self, tmp_path: Path) -> None:
        root = self._tree(
            tmp_path,
            'def f() -> None:\n    """Migrated from legacy-code/shamsu/llm/output.py."""\n',
        )
        assert boundary.main(["--root", str(root)]) == 0

    def test_comment_may_mention_the_archive(self, tmp_path: Path) -> None:
        root = self._tree(tmp_path, "# ported from legacy-code, rewritten\nX = 1\n")
        assert boundary.main(["--root", str(root)]) == 0


class TestExclusionPragma:
    """`# boundary-ok` lets code NAME the archive in order to EXCLUDE it.

    The motivating case is the artifact scanner's ignore list: the archive is
    tracked by git, so without an explicit exclusion SHAMSU would generate
    artifacts describing v1 while working on v2.
    """

    @staticmethod
    def _tree(tmp_path: Path, source: str) -> Path:
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        (src / "module.py").write_text(source, encoding="utf-8")
        return tmp_path

    def test_a_marked_exclusion_literal_is_allowed(self, tmp_path: Path) -> None:
        root = self._tree(
            tmp_path,
            'IGNORED = {\n    "legacy-code",  # boundary-ok: excluded from scanning\n}\n',
        )
        assert boundary.main(["--root", str(root)]) == 0

    def test_an_unmarked_literal_is_still_a_violation(self, tmp_path: Path) -> None:
        """The pragma must be opt-in per line, never ambient."""
        root = self._tree(tmp_path, 'IGNORED = {\n    "legacy-code",\n}\n')
        assert boundary.main(["--root", str(root)]) == 1

    def test_the_pragma_cannot_smuggle_an_import(self, tmp_path: Path) -> None:
        """Exclusion lists, not dependencies. An import is never exemptible."""
        root = self._tree(tmp_path, "import legacy_code  # boundary-ok: I promise\n")
        assert boundary.main(["--root", str(root)]) == 1

    def test_the_pragma_cannot_smuggle_a_from_import(self, tmp_path: Path) -> None:
        root = self._tree(tmp_path, "from legacy_code.shamsu import x  # boundary-ok: nope\n")
        assert boundary.main(["--root", str(root)]) == 1

    def test_the_pragma_does_not_leak_to_other_lines(self, tmp_path: Path) -> None:
        root = self._tree(
            tmp_path,
            'A = "legacy-code"  # boundary-ok: excluded\nB = "legacy-code/shamsu/x.py"\n',
        )
        assert boundary.main(["--root", str(root)]) == 1


class TestPackaging:
    def test_flags_a_dependency_on_the_archive(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["shamsu-legacy @ file://./legacy-code"]\n',
            encoding="utf-8",
        )
        assert boundary.main(["--root", str(tmp_path)]) == 1

    def test_flags_shipping_the_archive_in_the_wheel(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\n\n'
            "[tool.hatch.build.targets.wheel]\n"
            'packages = ["src/shamsu", "legacy-code/shamsu"]\n',
            encoding="utf-8",
        )
        assert boundary.main(["--root", str(tmp_path)]) == 1

    def test_real_pyproject_passes(self, repo_root: Path) -> None:
        """The root project's own enforcement rules must not self-flag."""
        assert boundary.check_packaging(repo_root) == []
