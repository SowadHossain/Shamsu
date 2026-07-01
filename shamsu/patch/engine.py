"""
Unified diff validation for SHAMSU patch workflows.
"""
from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from shamsu.indexer.walker import FileWalker
from shamsu.interfaces import IPatchEngine
from shamsu.safety.approval import ask_approval
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.types import ApprovalRequest

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


@dataclass(frozen=True)
class HunkPatch:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]


@dataclass(frozen=True)
class FilePatch:
    old_path: str
    new_path: str
    hunks: list[HunkPatch]

    @property
    def display_path(self) -> str:
        return self.new_path if self.new_path != "/dev/null" else self.old_path

    @property
    def is_create(self) -> bool:
        return self.old_path == "/dev/null"

    @property
    def is_delete(self) -> bool:
        return self.new_path == "/dev/null"


class PatchEngine(IPatchEngine):
    def __init__(
        self,
        workspace_root: Path | None = None,
        approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
    ) -> None:
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.approval_func = approval_func

    def validate_diff(self, diff_text: str) -> tuple[bool, str | None]:
        try:
            parse_unified_diff(diff_text, self.sandbox)
        except DiffValidationError as exc:
            return False, str(exc)
        return True, None

    def apply(self, diff_text: str, workspace_root: Path) -> bool:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        try:
            patches = parse_file_patches(diff_text, self.sandbox)
        except DiffValidationError:
            return False

        from shamsu.patch.preview import print_diff_preview

        print_diff_preview(diff_text, sandbox=self.sandbox)
        request = ApprovalRequest(
            action_type="file_delete" if any(patch.is_delete for patch in patches) else "file_edit",
            description=f"Apply patch touching {len(patches)} file(s).",
            risk_level="medium",
            preview=diff_text,
            working_dir=str(self.workspace_root),
            reason="Patch application modifies files inside the selected workspace.",
        )
        if not self.approval_func(request):
            return False

        backups: dict[Path, Path] = {}
        created_files: list[Path] = []
        try:
            for patch in patches:
                self._apply_file_patch(patch, backups, created_files)
        except (DiffValidationError, OSError):
            self._restore_backups(backups)
            for created in created_files:
                if created.exists():
                    created.unlink()
            return False
        FileWalker(self.workspace_root).index()
        return True

    def rollback(self, file_path: Path) -> bool:
        try:
            target = self.sandbox.validate(file_path)
        except SecurityError:
            return False
        backup = _backup_path(target)
        if not backup.exists():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup), str(target))
        return True

    def _apply_file_patch(
        self,
        patch: FilePatch,
        backups: dict[Path, Path],
        created_files: list[Path],
    ) -> None:
        old_target = None if patch.old_path == "/dev/null" else self.sandbox.validate(patch.old_path)
        new_target = None if patch.new_path == "/dev/null" else self.sandbox.validate(patch.new_path)

        if patch.is_create:
            if new_target is None:
                raise DiffValidationError("Create patch is missing target path.")
            if new_target.exists():
                raise DiffValidationError(f"Cannot create existing file: {new_target}")
            new_lines = _apply_hunks([], patch.hunks)
            new_target.parent.mkdir(parents=True, exist_ok=True)
            _write_lines(new_target, new_lines)
            created_files.append(new_target)
            return

        if old_target is None or not old_target.exists() or not old_target.is_file():
            raise DiffValidationError(f"Patch target does not exist: {old_target}")
        _backup_file(old_target, backups)
        original_lines = old_target.read_text(encoding="utf-8").splitlines()
        new_lines = _apply_hunks(original_lines, patch.hunks)

        if patch.is_delete:
            old_target.unlink()
            return

        if new_target is None:
            raise DiffValidationError("Patch is missing output path.")
        if old_target != new_target:
            _backup_file(new_target, backups)
            new_target.parent.mkdir(parents=True, exist_ok=True)
            old_target.unlink()
        _write_lines(new_target, new_lines)

    def _restore_backups(self, backups: dict[Path, Path]) -> None:
        for target, backup in reversed(list(backups.items())):
            if backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(backup), str(target))


def parse_unified_diff(
    diff_text: str,
    sandbox: Sandbox | None = None,
) -> list[FilePatchSummary]:
    return [
        FilePatchSummary(
            old_path=patch.old_path,
            new_path=patch.new_path,
            hunks=[
                HunkSummary(
                    old_start=hunk.old_start,
                    old_count=hunk.old_count,
                    new_start=hunk.new_start,
                    new_count=hunk.new_count,
                    additions=sum(1 for line in hunk.lines if line.startswith("+")),
                    deletions=sum(1 for line in hunk.lines if line.startswith("-")),
                )
                for hunk in patch.hunks
            ],
        )
        for patch in parse_file_patches(diff_text, sandbox)
    ]


def parse_file_patches(
    diff_text: str,
    sandbox: Sandbox | None = None,
) -> list[FilePatch]:
    lines = diff_text.splitlines()
    if not lines or not diff_text.strip():
        raise DiffValidationError("Diff is empty.")

    patches: list[FilePatch] = []
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
        hunks: list[HunkPatch] = []
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
        patches.append(FilePatch(old_path=old_path, new_path=new_path, hunks=hunks))

    if not patches:
        raise DiffValidationError("No unified diff file headers found.")
    return patches


def _parse_hunk(lines: list[str], start_index: int) -> tuple[HunkPatch, int]:
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
    hunk_lines: list[str] = []
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
        hunk_lines.append(line)
        index += 1

    if old_seen != old_count or new_seen != new_count:
        raise DiffValidationError(
            f"Hunk line count mismatch: expected -{old_count}/+{new_count}, "
            f"got -{old_seen}/+{new_seen}."
        )

    return (
        HunkPatch(
            old_start=int(match.group("old_start")),
            old_count=old_count,
            new_start=int(match.group("new_start")),
            new_count=new_count,
            lines=hunk_lines,
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


def _apply_hunks(original_lines: list[str], hunks: list[HunkPatch]) -> list[str]:
    output: list[str] = []
    cursor = 0
    for hunk in hunks:
        hunk_start = max(hunk.old_start - 1, 0)
        if hunk_start < cursor or hunk_start > len(original_lines):
            raise DiffValidationError("Hunk location is outside target file.")
        output.extend(original_lines[cursor:hunk_start])
        cursor = hunk_start
        for line in hunk.lines:
            if line.startswith("\\"):
                continue
            marker = " " if line == "" else line[0]
            content = "" if line == "" else line[1:]
            if marker == " ":
                _assert_source_line(original_lines, cursor, content)
                output.append(content)
                cursor += 1
            elif marker == "-":
                _assert_source_line(original_lines, cursor, content)
                cursor += 1
            elif marker == "+":
                output.append(content)
            else:
                raise DiffValidationError(f"Invalid hunk line marker: {line}")
    output.extend(original_lines[cursor:])
    return output


def _assert_source_line(lines: list[str], index: int, expected: str) -> None:
    if index >= len(lines) or lines[index] != expected:
        raise DiffValidationError("Patch context does not match target file.")


def _backup_file(target: Path, backups: dict[Path, Path]) -> None:
    if target in backups:
        return
    backup = _backup_path(target)
    if target.exists():
        shutil.copy2(target, backup)
    backups[target] = backup


def _backup_path(target: Path) -> Path:
    return Path(f"{target}.bak")


def _write_lines(target: Path, lines: list[str]) -> None:
    text = "\n".join(lines)
    if lines:
        text += "\n"
    target.write_text(text, encoding="utf-8")
