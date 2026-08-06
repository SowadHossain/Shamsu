"""The path sandbox, attacked.

This is a security boundary, so the tests are written as attempts to get out of
it rather than as demonstrations that it works. Plan §31.2 lists "attempted
path escape" in the adversarial suite; this is that suite.

The boundary is a *policy* layer: it constrains what tool arguments can
address. It does not stop a process from calling `open()` directly, and nothing
here should be read as claiming otherwise.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.security import PathEscape, PathSandbox

pytestmark = pytest.mark.adversarial


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("password", encoding="utf-8")
    return root


@pytest.fixture
def sandbox(workspace: Path) -> PathSandbox:
    return PathSandbox(workspace)


class TestLegitimateAccess:
    """The sandbox has to be usable, or callers route around it."""

    def test_relative_paths_resolve_under_the_workspace(
        self, sandbox: PathSandbox, workspace: Path
    ) -> None:
        assert sandbox.resolve("src/app.py") == workspace / "src" / "app.py"

    def test_a_path_that_does_not_exist_yet_is_allowed(
        self, sandbox: PathSandbox, workspace: Path
    ) -> None:
        """The sandbox guards writes too, so it cannot require existence."""
        assert sandbox.resolve("src/new_file.py") == workspace / "src" / "new_file.py"

    def test_absolute_paths_inside_the_workspace_are_accepted(
        self, sandbox: PathSandbox, workspace: Path
    ) -> None:
        """Rejecting these is tempting and wrong.

        Users and models write absolute paths constantly. v1 tried to normalise
        them by regex and silently demoted `/tmp/ws/x.md` to `tmp/ws/x.md`
        (see LEGACY_COMPONENTS.md). Accepting them explicitly is the fix.
        """
        target = workspace / "src" / "app.py"
        assert sandbox.resolve(str(target)) == target

    def test_interior_dot_dot_that_stays_inside_is_fine(
        self, sandbox: PathSandbox, workspace: Path
    ) -> None:
        assert sandbox.resolve("src/../src/app.py") == workspace / "src" / "app.py"

    def test_the_workspace_root_itself_is_inside(self, sandbox: PathSandbox) -> None:
        assert sandbox.relative(".") == "."

    def test_relative_rendering_is_posix_everywhere(self, sandbox: PathSandbox) -> None:
        """One spelling per file, whatever platform produced the request."""
        assert sandbox.relative("src/app.py") == "src/app.py"

    def test_an_absolute_request_renders_the_same_as_a_relative_one(
        self, sandbox: PathSandbox, workspace: Path
    ) -> None:
        assert sandbox.relative(str(workspace / "src" / "app.py")) == "src/app.py"


class TestEscapeAttempts:
    @pytest.mark.parametrize(
        "attempt",
        [
            "../outside/secret.txt",
            "../../etc/passwd",
            "src/../../outside/secret.txt",
            "./../../outside/secret.txt",
            "src/../../../../../../etc/shadow",
        ],
    )
    def test_dot_dot_traversal_is_refused(self, sandbox: PathSandbox, attempt: str) -> None:
        with pytest.raises(PathEscape):
            sandbox.resolve(attempt)

    @pytest.mark.parametrize("attempt", ["/etc/passwd", "/tmp", "/"])
    def test_absolute_paths_outside_are_refused(self, sandbox: PathSandbox, attempt: str) -> None:
        with pytest.raises(PathEscape):
            sandbox.resolve(attempt)

    def test_a_sibling_with_a_shared_prefix_is_not_inside(self, tmp_path: Path) -> None:
        """`/ws-evil` must not count as inside `/ws`.

        A `str.startswith` check accepts it. Component-wise comparison does not,
        which is why `is_relative_to` is used rather than string matching.
        """
        (tmp_path / "ws").mkdir()
        (tmp_path / "ws-evil").mkdir()
        sandbox = PathSandbox(tmp_path / "ws")

        with pytest.raises(PathEscape):
            sandbox.resolve(str(tmp_path / "ws-evil" / "payload.py"))

    def test_a_symlink_pointing_out_is_refused(
        self, sandbox: PathSandbox, workspace: Path, tmp_path: Path
    ) -> None:
        """A lexical check waves this straight through."""
        try:
            (workspace / "escape").symlink_to(tmp_path / "outside")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with pytest.raises(PathEscape):
            sandbox.resolve("escape/secret.txt")

    def test_a_symlink_to_a_file_outside_is_refused(
        self, sandbox: PathSandbox, workspace: Path, tmp_path: Path
    ) -> None:
        try:
            (workspace / "leak.txt").symlink_to(tmp_path / "outside" / "secret.txt")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with pytest.raises(PathEscape):
            sandbox.resolve("leak.txt")

    def test_a_symlink_staying_inside_is_allowed(
        self, sandbox: PathSandbox, workspace: Path
    ) -> None:
        """Not every symlink is an attack; refusing all of them breaks repos."""
        try:
            (workspace / "alias.py").symlink_to(workspace / "src" / "app.py")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        assert sandbox.resolve("alias.py") == workspace / "src" / "app.py"

    def test_home_expansion_cannot_escape(self, sandbox: PathSandbox) -> None:
        """`~` expands before the check, so it cannot smuggle a path out."""
        with pytest.raises(PathEscape):
            sandbox.resolve("~/.ssh/id_rsa")

    def test_the_error_explains_the_resolution(self, sandbox: PathSandbox) -> None:
        """'Why was this rejected?' is unanswerable from the input alone."""
        with pytest.raises(PathEscape) as excinfo:
            sandbox.resolve("../outside/secret.txt")
        assert excinfo.value.requested == "../outside/secret.txt"
        assert "outside" in str(excinfo.value.resolved)


class TestContainsNeverRaises:
    @pytest.mark.parametrize("attempt", ["src/app.py", "../outside/secret.txt", "/etc/passwd", ""])
    def test_returns_a_boolean_for_anything(self, sandbox: PathSandbox, attempt: str) -> None:
        assert isinstance(sandbox.contains(attempt), bool)

    def test_an_empty_path_is_the_workspace_itself(self, sandbox: PathSandbox) -> None:
        assert sandbox.contains("") is True


class TestBatchResolution:
    def test_all_or_nothing(self, sandbox: PathSandbox) -> None:
        """A tool given five paths, one of which escapes, should do nothing."""
        with pytest.raises(PathEscape):
            sandbox.resolve_all(["src/app.py", "../outside/secret.txt"])

    def test_a_clean_batch_resolves(self, sandbox: PathSandbox) -> None:
        assert len(sandbox.resolve_all(["src/app.py", "src/other.py"])) == 2


class TestSymlinkedWorkspaceRoot:
    def test_a_symlinked_workspace_still_works(self, tmp_path: Path) -> None:
        """`/tmp` is a symlink on macOS; without resolving the root once at
        construction, every single check would fail."""
        real = tmp_path / "real_workspace"
        real.mkdir()
        (real / "app.py").write_text("x = 1\n", encoding="utf-8")

        link = tmp_path / "linked_workspace"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        sandbox = PathSandbox(link)
        assert sandbox.relative("app.py") == "app.py"
