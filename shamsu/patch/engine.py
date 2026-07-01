"""
Unified diff validation for SHAMSU patch workflows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from shamsu.interfaces import IPatchEngine
from shamsu.safety.sandbox import Sandbox, SecurityError

HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?: .*)?$"
)


@dataclass(frozen=True)
class HunkSummary:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class FilePatchSummary:
    old_path: str
    new_path: str
    hunks: list[HunkSummary] = field(default_factory=list)

    @property
    def display_path(self) -> str:
        return self.new_path if self.new_path != "/dev/null" else self.old_path

    @property
    def additions(self) -> int:
        return sum(hunk.additions for hunk in self.hunks)

    @property
    def deletions(self) -> int:
        return sum(hunk.deletions for hunk in self.hunks)


class DiffValidationError(ValueError):
    pass


class PatchEngine(IPatchEngine):
    def __init__(self, workspace_root: Path | None = None) -> None:
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.sandbox = Sandbox(self.workspace_root)

    def validate_diff(self, diff_text: str) -> tuple[bool, str | None]:
        try:
            parse_unified_diff(diff_text, self.sandbox)
        except DiffValidationError as exc:
            return False, str(exc)
        return True, None

    def apply(self, diff_text: str, workspace_root: Path) -> bool:
        return False

    def rollback(self, file_path: Path) -> bool:
        return False


def parse_unified_diff(
    diff_text: str,
    sandbox: Sandbox | None = None,
) -> list[FilePatchSummary]:
    lines = diff_text.splitlines()
    if not lines or not diff_text.strip():
        raise DiffValidationError("Diff is empty.")

    patches: list[FilePatchSummary] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("--- "):
            index += 1
            continue

        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise DiffValidationError("Missing +++ header after --- header.")

        old_path = _clean_header_path(lines[index][4:])
        new_path = _clean_header_path(lines[index + 1][4:])
        _validate_patch_paths(old_path, new_path, sandbox)

        index += 2
        hunks: list[HunkSummary] = []
        while index < len(lines):
            current = lines[index]
            if current.startswith("--- "):
                break
            if current.startswith("@@ "):
                hunk, index = _parse_hunk(lines, index)
                hunks.append(hunk)
                continue
            if current.startswith(("diff --git ", "index ", "new file mode ", "deleted file mode ")):
                index += 1
                continue
            if current.strip() == "":
                index += 1
                continue
            raise DiffValidationError(f"Unexpected line outside hunk: {current}")

        if not hunks:
            raise DiffValidationError(f"File patch has no hunks: {_display_path(old_path, new_path)}")
        patches.append(FilePatchSummary(old_path=old_path, new_path=new_path, hunks=hunks))

    if not patches:
        raise DiffValidationError("No unified diff file headers found.")
    return patches


def _parse_hunk(lines: list[str], start_index: int) -> tuple[HunkSummary, int]:
    header = lines[start_index]
    match = HUNK_HEADER_RE.match(header)
    if not match:
        raise DiffValidationError(f"Malformed hunk header: {header}")

    old_count = _count_from_header(match.group("old_count"))
    new_count = _count_from_header(match.group("new_count"))
    old_seen = 0
    new_seen = 0
    additions = 0
    deletions = 0
    index = start_index + 1

    while index < len(lines):
        line = lines[index]
        if line.startswith("@@ ") or line.startswith("--- "):
            break
        if line.startswith("\\"):
            index += 1
            continue
        if line == "":
            marker = " "
        else:
            marker = line[0]
        if marker not in {" ", "+", "-"}:
            raise DiffValidationError(f"Invalid hunk line marker: {line}")
        if marker in {" ", "-"}:
            old_seen += 1
        if marker in {" ", "+"}:
            new_seen += 1
        if marker == "+":
            additions += 1
        if marker == "-":
            deletions += 1
        index += 1

    if old_seen != old_count or new_seen != new_count:
        raise DiffValidationError(
            f"Hunk line count mismatch: expected -{old_count}/+{new_count}, "
            f"got -{old_seen}/+{new_seen}."
        )

    return (
        HunkSummary(
            old_start=int(match.group("old_start")),
            old_count=old_count,
            new_start=int(match.group("new_start")),
            new_count=new_count,
            additions=additions,
            deletions=deletions,
        ),
        index,
    )


def _count_from_header(value: str | None) -> int:
    return 1 if value is None else int(value)


def _clean_header_path(raw: str) -> str:
    path = raw.strip()
    if "\t" in path:
        path = path.split("\t", 1)[0]
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path in {"/dev/null", "dev/null"}:
        return "/dev/null"
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path.replace("\\", "/")


def _validate_patch_paths(old_path: str, new_path: str, sandbox: Sandbox | None) -> None:
    if old_path == "/dev/null" and new_path == "/dev/null":
        raise DiffValidationError("Patch cannot use /dev/null for both file paths.")
    for patch_path in {old_path, new_path} - {"/dev/null"}:
        _reject_unsafe_path(patch_path)
        if sandbox is not None:
            try:
                sandbox.validate(patch_path)
            except SecurityError as exc:
                raise DiffValidationError(str(exc)) from exc


def _reject_unsafe_path(patch_path: str) -> None:
    if not patch_path:
        raise DiffValidationError("Patch path is empty.")
    posix_path = PurePosixPath(patch_path)
    windows_path = PureWindowsPath(patch_path)
    if posix_path.is_absolute() or windows_path.is_absolute():
        raise DiffValidationError(f"Patch path must be relative: {patch_path}")
    if ".." in posix_path.parts or ".." in windows_path.parts:
        raise DiffValidationError(f"Patch path escapes workspace: {patch_path}")


def _display_path(old_path: str, new_path: str) -> str:
    return new_path if new_path != "/dev/null" else old_path
