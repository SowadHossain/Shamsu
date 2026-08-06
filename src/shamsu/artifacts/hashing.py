"""Content hashing and repository scanning.

Freshness has to be computable without asking a model, and without trusting a
timestamp. mtime is not enough: a checkout, a stash pop, or a `touch` all move
it without changing content, and a fast edit can leave it unchanged. So the
question "is this artifact still true?" is answered by comparing content
hashes of the files it was derived from.

Scanning is deliberately conservative about what it walks. An artifact system
that indexes `.venv` or `node_modules` produces enormous, useless artifacts and
makes every refresh slow enough that people turn it off.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

HASH_PREFIX = "sha256"

#: Directories never walked. `legacy-code` is here because SHAMSU indexes its
#: own repository during development, and the archived tree is reference
#: material -- artifacts describing it would be noise at best and misleading
#: structural claims at worst.
DEFAULT_IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        "node_modules",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        ".shamsu",
        ".idea",
        ".vscode",
        "site-packages",
        # Named here to EXCLUDE it, which is the opposite of depending on it.
        # Needed because the archive IS tracked by git, so the git-listed path
        # would otherwise sweep v1 in and generate artifacts describing it.
        "legacy-code",  # boundary-ok: exclusion, never an import
    }
)

#: Suffixes that carry no structure worth an artifact. Binary blobs would also
#: blow the scan's memory budget for no benefit.
DEFAULT_IGNORED_SUFFIXES: frozenset[str] = frozenset(
    {
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".o",
        ".a",
        ".class",
        ".jar",
        ".zip",
        ".tar",
        ".gz",
        ".bz2",
        ".xz",
        ".7z",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".svg",
        ".pdf",
        ".mp3",
        ".mp4",
        ".mov",
        ".avi",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".db",
        ".sqlite",
        ".sqlite3",
        ".lock",
    }
)

#: Files larger than this are recorded by size rather than content. A 40 MB
#: generated file has no useful structure and hashing it every refresh is a
#: waste; recording size still detects that it changed.
LARGE_FILE_BYTES = 2_000_000

_READ_CHUNK = 65_536


def hash_bytes(data: bytes) -> str:
    """Hash raw content."""
    return f"{HASH_PREFIX}:{hashlib.sha256(data).hexdigest()}"


def hash_text(text: str) -> str:
    """Hash text as UTF-8."""
    return hash_bytes(text.encode("utf-8"))


def hash_file(path: Path) -> str:
    """Hash a file's content, streaming so a large file need not fit in memory.

    Returns a `size:` marker for files past `LARGE_FILE_BYTES` and a `missing:`
    marker for files that cannot be read. Both are stable strings that compare
    correctly, so a caller never has to special-case them -- and an unreadable
    file reads as *changed* rather than silently unchanged, which is the safe
    direction for invalidation.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return "missing:"

    if size > LARGE_FILE_BYTES:
        return f"size:{size}"

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK):
                digest.update(chunk)
    except OSError:
        return "missing:"
    return f"{HASH_PREFIX}:{digest.hexdigest()}"


def is_ignored(
    relative: Path,
    *,
    ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
    ignored_suffixes: frozenset[str] = DEFAULT_IGNORED_SUFFIXES,
) -> bool:
    """Whether a repository-relative path is excluded from scanning."""
    if any(part in ignored_dirs for part in relative.parts):
        return True
    return relative.suffix.lower() in ignored_suffixes


def iter_files(
    root: Path,
    *,
    ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
    ignored_suffixes: frozenset[str] = DEFAULT_IGNORED_SUFFIXES,
) -> Iterator[Path]:
    """Yield repository-relative paths worth indexing, in stable order.

    Prunes ignored directories during the walk rather than filtering after,
    so a large `node_modules` costs nothing instead of costing a full traversal.
    """
    root = root.resolve()
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            continue

        for entry in entries:
            # Symlinks are not followed: a link into a sibling checkout or a
            # self-referential link turns a scan into an unbounded walk.
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in ignored_dirs:
                    stack.append(entry)
                continue
            if not entry.is_file():
                continue

            relative = entry.relative_to(root)
            if is_ignored(relative, ignored_dirs=ignored_dirs, ignored_suffixes=ignored_suffixes):
                continue
            yield relative


def git_listed_files(root: Path, *, timeout: float = 30.0) -> list[Path] | None:
    """Files git considers part of the project, or None if git cannot answer.

    `--cached --others --exclude-standard` means tracked files plus untracked
    ones that are not ignored -- so a file the agent just created is indexed,
    while build output and vendored trees are not.

    This is strictly better than a hand-maintained ignore list, because the
    project already declares what belongs to it in `.gitignore`. A vendored
    reference checkout sitting in the working tree is exactly the kind of thing
    that would otherwise produce hundreds of confidently wrong artifacts about
    code that is not the project's.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if result.returncode != 0:
        return None

    # -z output is NUL-separated, which survives paths containing newlines.
    names = [name for name in result.stdout.decode("utf-8", "replace").split("\0") if name]
    return [Path(name) for name in names]


def scan_repository(
    root: Path,
    *,
    ignored_dirs: frozenset[str] = DEFAULT_IGNORED_DIRS,
    ignored_suffixes: frozenset[str] = DEFAULT_IGNORED_SUFFIXES,
    use_git: bool = True,
) -> dict[str, str]:
    """Map every indexable file to its content hash.

    Prefers git's view of the project and falls back to a filesystem walk when
    the root is not a repository or git is unavailable. Either way the ignored
    suffixes still apply, so a tracked binary asset does not get hashed.

    Keys are POSIX-style repository-relative paths, so an artifact built on
    Windows stays comparable to one built on Linux.
    """
    root = root.resolve()

    listed = git_listed_files(root) if use_git else None
    if listed is None:
        candidates: Iterable[Path] = iter_files(
            root, ignored_dirs=ignored_dirs, ignored_suffixes=ignored_suffixes
        )
    else:
        candidates = (
            relative
            for relative in listed
            if not is_ignored(
                relative, ignored_dirs=ignored_dirs, ignored_suffixes=ignored_suffixes
            )
        )

    return {
        relative.as_posix(): hash_file(root / relative)
        for relative in sorted(candidates, key=lambda p: p.as_posix())
    }


def hash_paths(root: Path, paths: Iterable[str]) -> dict[str, str]:
    """Hash a specific set of repository-relative paths."""
    root = root.resolve()
    return {path: hash_file(root / path) for path in paths}


def changed_paths(
    previous: Mapping[str, str], current: Mapping[str, str]
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Compare two scans.

    Returns ``(added, modified, removed)``. Kept separate because they mean
    different things for invalidation: a modified source makes an artifact
    stale, a removed one makes it invalid.
    """
    previous_keys, current_keys = set(previous), set(current)
    added = current_keys - previous_keys
    removed = previous_keys - current_keys
    modified = {path for path in previous_keys & current_keys if previous[path] != current[path]}
    return frozenset(added), frozenset(modified), frozenset(removed)


__all__ = [
    "DEFAULT_IGNORED_DIRS",
    "DEFAULT_IGNORED_SUFFIXES",
    "HASH_PREFIX",
    "LARGE_FILE_BYTES",
    "changed_paths",
    "git_listed_files",
    "hash_bytes",
    "hash_file",
    "hash_paths",
    "hash_text",
    "is_ignored",
    "iter_files",
    "scan_repository",
]
