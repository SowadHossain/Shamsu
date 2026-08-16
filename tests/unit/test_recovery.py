"""Git as the recovery mechanism.

`CheckpointRecord.git_ref` existed from the first schema and nothing ever set
it, so a checkpoint recorded a state snapshot with no way to get the files
back. `rollback_to` and `PatchUndo.rollback_all` were implemented, tested, and
called by nothing.

The design decision worth pinning down here is the one *not* to auto-revert.
Reverting a failed step conflicts head-on with letting failure be local: later
steps keep running and some build on what the failed step wrote, so an
automatic rollback would turn one honest failure into a corrupted workspace.
The runtime captures the point it could return to and says so.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shamsu.runtime.recovery import head_ref, recovery_point
from shamsu.tools.git import run_git


def _repository(root: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    run_git(root, "config", "user.email", "t@example.com")
    run_git(root, "config", "user.name", "T")
    (root / "seed.py").write_text("x = 1\n", encoding="utf-8")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-m", "initial", "--no-verify")
    return root


class TestARefIsOnlyRecordedWhenItMeansSomething:
    def test_a_repository_has_a_ref(self, tmp_path: Path) -> None:
        _repository(tmp_path)
        ref = head_ref(tmp_path)
        assert ref and len(ref) >= 7

    def test_a_plain_folder_has_none(self, tmp_path: Path) -> None:
        assert head_ref(tmp_path) is None

    def test_a_repository_with_no_commits_has_none(self, tmp_path: Path) -> None:
        """Better `None` than a string that cannot be checked out."""
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
        assert head_ref(tmp_path) is None


class TestTheReportIsActionable:
    def test_it_names_the_command_to_run(self, tmp_path: Path) -> None:
        """'You can revert' is not actionable at the moment someone needs it."""
        _repository(tmp_path)
        rendered = recovery_point(tmp_path, ("calc.py",)).render()
        assert "git checkout" in rendered
        assert "calc.py" in rendered

    def test_it_warns_that_untracked_files_survive_the_checkout(self, tmp_path: Path) -> None:
        _repository(tmp_path)
        rendered = recovery_point(tmp_path, ("new.py",)).render()
        assert "untracked" in rendered

    def test_an_unchanged_tree_says_so(self, tmp_path: Path) -> None:
        _repository(tmp_path)
        assert "unchanged" in recovery_point(tmp_path).render()

    def test_no_repository_explains_the_fix(self, tmp_path: Path) -> None:
        point = recovery_point(tmp_path, ("calc.py",))
        assert point.recoverable is False
        assert "git init" in point.render()

    def test_a_long_change_list_is_summarised(self, tmp_path: Path) -> None:
        _repository(tmp_path)
        rendered = recovery_point(tmp_path, tuple(f"f{n}.py" for n in range(9))).render()
        assert "+4 more" in rendered


class TestTheRefIsRealEnoughToUse:
    def test_the_recorded_ref_can_restore_the_tree(self, tmp_path: Path) -> None:
        """The point of recording it: the command in the report has to work."""
        _repository(tmp_path)
        point = recovery_point(tmp_path)
        assert point.ref is not None

        (tmp_path / "seed.py").write_text("x = 999\n", encoding="utf-8")
        run_git(tmp_path, "checkout", point.ref, "--", ".")

        assert (tmp_path / "seed.py").read_text(encoding="utf-8") == "x = 1\n"
