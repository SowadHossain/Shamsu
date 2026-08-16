"""The path sandbox.

Every filesystem path the agent touches passes through here. This is a policy
boundary, not an OS sandbox -- a determined process can still call `open()`
directly. What it guarantees is that *tool arguments* cannot address anything
outside the workspace.

Three rules, in order of how often they are got wrong:

1. **Resolve before deciding.** `workspace/../etc/passwd` only looks safe until
   it is normalised. Every check happens on the resolved path.
2. **Follow symlinks.** A link inside the workspace pointing at `/etc` is an
   escape hatch that a purely lexical check waves through.
3. **Absolute paths inside the workspace are fine.** Rejecting them outright is
   tempting and wrong: users and models both write absolute paths constantly,
   and refusing them pushes callers into ad-hoc string munging.

Rule 3 is where v1 went wrong, and it is worth being precise about how. v1
recovered paths by regex-scraping prose, and its pattern started matching at a
word character -- so `/tmp/ws/notes.md` was captured as `tmp/ws/notes.md`, an
absolute path silently demoted to a relative one that resolved somewhere else
entirely (see `LEGACY_COMPONENTS.md`). Here a path is a typed tool argument
that arrives intact, and normalising it is this module's explicit job rather
than a side effect of a regex.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath


class PathEscape(Exception):
    """A path resolved outside the workspace.

    Carries both the request and the resolution, because "why was this
    rejected?" is unanswerable from the input alone once symlinks are involved.
    """

    def __init__(self, requested: str, resolved: Path, workspace: Path) -> None:
        super().__init__(
            f"path escapes the workspace: {requested!r} resolves to {resolved} "
            f"which is outside {workspace}"
        )
        self.requested = requested
        self.resolved = resolved
        self.workspace = workspace


class PathSandbox:
    """Confines path arguments to one workspace.

    The workspace is resolved once at construction, so a symlinked workspace
    root (`/tmp` on macOS is the classic case) compares correctly against
    resolved candidates instead of failing every check.
    """

    __slots__ = ("_workspace",)

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser().resolve()

    @property
    def workspace(self) -> Path:
        return self._workspace

    # -- resolution --------------------------------------------------------

    def resolve(self, candidate: str | Path) -> Path:
        """Resolve a path argument to an absolute path inside the workspace.

        Accepts relative paths, absolute paths that land inside the workspace,
        and `~` expansion. Works for paths that do not exist yet, so it can
        guard a write as well as a read.

        Raises:
            PathEscape: the resolved path is outside the workspace.
        """
        requested = str(candidate)
        raw = Path(requested).expanduser()

        # A relative path is relative to the workspace, never to the process
        # working directory -- which the agent does not control and which would
        # make the same argument mean different things at different times.
        joined = raw if raw.is_absolute() else self._workspace / raw

        resolved = joined.resolve()
        if not self._contains(resolved):
            raise PathEscape(requested, resolved, self._workspace)
        return resolved

    def relative(self, candidate: str | Path) -> str:
        """Resolve, then render as a workspace-relative POSIX path.

        The canonical form for anything stored or shown: artifact source refs,
        tool results, and model-facing text all use it, so the same file has
        one spelling everywhere regardless of how it was requested.
        """
        resolved = self.resolve(candidate)
        if resolved == self._workspace:
            return "."
        return PurePosixPath(resolved.relative_to(self._workspace)).as_posix()

    def contains(self, candidate: str | Path) -> bool:
        """Whether a path is inside the workspace. Never raises."""
        try:
            self.resolve(candidate)
        except (PathEscape, OSError, ValueError):
            return False
        return True

    def resolve_all(self, candidates: Iterable[str | Path]) -> list[Path]:
        """Resolve several paths, failing on the first escape.

        All-or-nothing on purpose: a tool given five paths of which one escapes
        should do nothing, not four fifths of the job.
        """
        return [self.resolve(candidate) for candidate in candidates]

    # -- internals ---------------------------------------------------------

    def _contains(self, resolved: Path) -> bool:
        if resolved == self._workspace:
            return True
        # `is_relative_to` compares path components, so `/ws-evil` is correctly
        # not inside `/ws` -- a plain `str.startswith` would accept it.
        return resolved.is_relative_to(self._workspace)


def workspace_key(path: str) -> str:
    """One spelling per workspace-relative file, for comparing paths as strings.

    Exists because the idiom it replaces was wrong in a way that reads as
    correct. Fourteen call sites across the package normalised paths with

        path.replace("\\\\", "/").lstrip("./")

    and `str.lstrip` strips a *character set*, not a prefix — so `.shamsu/`
    became `shamsu/`, `.github/workflows/ci.yml` became `github/...`, and any
    dot-file lost its leading dot. Mostly this was invisible, because both
    sides of a comparison were mangled identically. It stopped being invisible
    the moment one side was a literal: a check for `.shamsu` against a key that
    had already been shortened to `shamsu` never matched, so the agent's own
    state directory counted as a project change on every single step.

    Strips one leading `./` and nothing else. Trailing slashes go too, because
    `git status --porcelain` reports an untracked directory with one and no
    other caller ever wants it.
    """
    cleaned = path.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.rstrip("/")


#: Test-file conventions. Deliberately the ecosystems v2 targets first, and it
#: will need extending. The failure mode is the safe direction — an
#: unrecognised test file is merely editable, not silently exempt from
#: verification.
#:
#: Lives here rather than in `agent/repair.py`, where it started, because the
#: gateway needs it too: protecting tests only during *repair* left ordinary
#: authoring free to edit them, which is how this runtime produced its first
#: false success.
_TEST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"[^/]+_test\.py$"),
    re.compile(r"\.(test|spec)\.[jt]sx?$"),
    re.compile(r"(^|/)conftest\.py$"),
)


def looks_like_a_test(path: str) -> bool:
    """Whether a path is a test file by convention."""
    return any(pattern.search(path.replace("\\", "/")) for pattern in _TEST_PATTERNS)


__all__ = ["PathEscape", "PathSandbox", "looks_like_a_test", "workspace_key"]
