"""The read-only tool surface: `project.inspect`, `code.search`, `file.list`,
`file.read`.

These are deliberately *logical* rather than thin wrappers over syscalls. Plan
§22 makes the point about git: the model should not be choosing among low-level
commands for ordinary work. The same applies here — `project.inspect` answers
"what is this project?" in one call instead of making the model read six
manifest files and reason about the difference.

`file.list` is the exception that proves the rule, and it was added late. It
*is* close to a syscall, and it is here because the logical tools left no way
to answer "what files exist here?" — an agent had to guess a path, call
`file.read`, and learn from the failure. That is a consumed action and a
consecutive-failure tick spent on a question a directory listing answers.

Every one of them is non-mutating, allowed only in read-only phases, and capped.
None of them can address anything outside the workspace, because every path
argument goes through `PathSandbox`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from shamsu.artifacts.generators import (
    RepositoryContext,
    RepositoryManifestGenerator,
)
from shamsu.artifacts.hashing import is_ignored
from shamsu.interfaces.artifacts import ArtifactGenerationError
from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.enums import Phase, Risk
from shamsu.interfaces.tools import ToolContract, ToolResult
from shamsu.security.paths import PathEscape, PathSandbox
from shamsu.tools.base import Tool
from shamsu.tools.documents import (
    ExtractionFailed,
    describe_unreadable,
    extract,
    is_extractable,
)

#: Phases in which reading is legitimate. Notably includes AUTHOR and REPAIR:
#: you cannot write a correct patch without reading the file first.
_READ_PHASES = frozenset({Phase.INSPECT, Phase.PLAN, Phase.AUTHOR, Phase.VERIFY, Phase.REPAIR})


# ---------------------------------------------------------------------------
# project.inspect
# ---------------------------------------------------------------------------


class ProjectInspectInput(BaseModel):
    """No arguments. The tool answers one fixed question about the workspace."""


class ProjectInspectTool(Tool[ProjectInspectInput]):
    """What kind of project is this, and how is it built, run, and tested?

    Reuses the artifact manifest generator rather than reimplementing manifest
    parsing, so the model's answer and the stored artifact cannot disagree.
    """

    input_model = ProjectInspectInput

    contract = ToolContract(
        name="project.inspect",
        purpose=(
            "Identify the project: languages, package managers, entry points, "
            "test framework, build and run commands, and major directories."
        ),
        allowed_phases=_READ_PHASES,
        risk=Risk.LOW,
        reversible=True,
        timeout_seconds=60.0,
        max_output_bytes=8_000,
    )

    def __init__(self, workspace: Path, *, use_git: bool = True) -> None:
        self._workspace = Path(workspace)
        self._use_git = use_git

    async def run(self, arguments: ProjectInspectInput, cancel: CancellationToken) -> ToolResult:
        started = time.monotonic()
        cancel.raise_if_cancelled()

        try:
            context = RepositoryContext(self._workspace, use_git=self._use_git)
            generated = RepositoryManifestGenerator(context).generate(
                RepositoryManifestGenerator.KEY
            )
        except ArtifactGenerationError as exc:
            return self.failed(f"could not inspect the project: {exc}", started=started)

        return self.ok(generated.content, started=started)


# ---------------------------------------------------------------------------
# code.search
# ---------------------------------------------------------------------------


class CodeSearchInput(BaseModel):
    query: str = Field(min_length=1, description="Literal text to find. Not a regex.")
    path: str = Field(
        default=".", description="Directory or file to search within, workspace-relative."
    )
    max_results: int = Field(default=20, ge=1, le=100)
    case_sensitive: bool = False


class CodeSearchTool(Tool[CodeSearchInput]):
    """Exact text search — stage 2 of the retrieval order in plan §18.

    Literal, not regex, on purpose. A small model writing a regex produces
    catastrophic backtracking and silent zero-hit results far more often than
    it produces a useful pattern, and "no matches" is indistinguishable from
    "no such code" unless the search is predictable.
    """

    input_model = CodeSearchInput

    contract = ToolContract(
        name="code.search",
        purpose=(
            "Find literal text in the repository. Returns file, line number, and "
            "the matching line. Use before reading a file, to find where to look."
        ),
        allowed_phases=_READ_PHASES,
        risk=Risk.LOW,
        reversible=True,
        timeout_seconds=30.0,
        max_output_bytes=16_000,
    )

    #: Lines longer than this are almost always minified or generated; showing
    #: one would spend the whole output budget on a single hit.
    MAX_LINE = 400

    def __init__(self, workspace: Path) -> None:
        self._sandbox = PathSandbox(workspace)

    async def run(self, arguments: CodeSearchInput, cancel: CancellationToken) -> ToolResult:
        started = time.monotonic()

        try:
            root = self._sandbox.resolve(arguments.path)
        except PathEscape as exc:
            return self.failed(str(exc), started=started)

        if not root.exists():
            return self.failed(f"{arguments.path}: no such file or directory", started=started)

        needle = arguments.query if arguments.case_sensitive else arguments.query.casefold()
        hits: list[str] = []
        scanned = 0

        for candidate in self._candidates(root):
            cancel.raise_if_cancelled()
            scanned += 1
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            relative = self._sandbox.relative(candidate)
            for number, line in enumerate(text.splitlines(), 1):
                haystack = line if arguments.case_sensitive else line.casefold()
                if needle not in haystack:
                    continue
                shown = line.strip()
                if len(shown) > self.MAX_LINE:
                    shown = shown[: self.MAX_LINE] + " …"
                hits.append(f"{relative}:{number}: {shown}")
                if len(hits) >= arguments.max_results:
                    break
            if len(hits) >= arguments.max_results:
                break

        if not hits:
            # An explicit no-hit answer, not an empty string. "I searched and
            # found nothing" and "the tool returned nothing" must not look the
            # same to the model.
            return self.ok(
                f"No matches for {arguments.query!r} in {arguments.path} "
                f"({scanned} file(s) searched).",
                started=started,
            )

        header = f"{len(hits)} match(es) for {arguments.query!r} in {arguments.path}:"
        if len(hits) >= arguments.max_results:
            header += f" (stopped at max_results={arguments.max_results})"
        return self.ok("\n".join([header, *hits]), started=started)

    def _candidates(self, root: Path) -> list[Path]:
        if root.is_file():
            return [root]
        found = [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file()
            and not path.is_symlink()
            and not is_ignored(path.relative_to(self._sandbox.workspace))
        ]
        return found


# ---------------------------------------------------------------------------
# file.read
# ---------------------------------------------------------------------------


class FileReadInput(BaseModel):
    path: str = Field(min_length=1, description="Workspace-relative or absolute path.")
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(
        default=None, ge=1, description="Inclusive. Omit to read to the end."
    )


class FileReadTool(Tool[FileReadInput]):
    """Read a file, or a line range of one.

    Output is line-numbered because everything downstream — a patch, a symbol
    card, a failure capsule — refers to code by line, and a model that has to
    count lines itself gets it wrong.
    """

    input_model = FileReadInput

    contract = ToolContract(
        name="file.read",
        purpose=(
            "Read a file's contents, optionally a line range. Output is "
            "line-numbered. Prefer a range for large files."
        ),
        allowed_phases=_READ_PHASES,
        risk=Risk.LOW,
        reversible=True,
        timeout_seconds=15.0,
        max_output_bytes=24_000,
    )

    def __init__(self, workspace: Path) -> None:
        self._sandbox = PathSandbox(workspace)

    def read_targets(self, arguments: FileReadInput) -> tuple[str, ...]:
        """This file is now known, which is what lets `file.patch` edit it."""
        return (arguments.path,)

    async def run(self, arguments: FileReadInput, cancel: CancellationToken) -> ToolResult:
        started = time.monotonic()
        cancel.raise_if_cancelled()

        try:
            target = self._sandbox.resolve(arguments.path)
        except PathEscape as exc:
            return self.failed(str(exc), started=started)

        if target.is_dir():
            return self.failed(
                f"{arguments.path} is a directory. Use code.search to look inside it.",
                started=started,
            )
        if not target.exists():
            return self.failed(f"{arguments.path}: no such file", started=started)

        # A `.docx` is a zip of XML. Decoded as UTF-8 it is kilobytes of
        # replacement characters, and a model handed that will describe the
        # document anyway — a live run reported what a PRD was "about" having
        # read nothing but noise.
        if is_extractable(target):
            try:
                document = extract(target)
            except ExtractionFailed as exc:
                return self.failed(str(exc), started=started)
            return self.ok(document.render(self._sandbox.relative(target)), started=started)

        if (kind := describe_unreadable(target)) is not None:
            # Honest failure over fabrication. "I cannot read a PDF" is worth
            # more than a page of mojibake the model will summarise regardless.
            return self.failed(
                f"{arguments.path} is a {kind} file, which cannot be read as text. "
                "Its contents are not available to me.",
                started=started,
            )

        if arguments.end_line is not None and arguments.end_line < arguments.start_line:
            return self.failed(
                f"end_line ({arguments.end_line}) is before start_line ({arguments.start_line})",
                started=started,
            )

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return self.failed(f"{arguments.path}: {exc}", started=started)

        lines = text.splitlines()
        total = len(lines)
        end = min(arguments.end_line or total, total)

        if arguments.start_line > total:
            return self.failed(
                f"{arguments.path} has {total} line(s); start_line "
                f"{arguments.start_line} is past the end",
                started=started,
            )

        selected = lines[arguments.start_line - 1 : end]
        width = len(str(end))
        body = "\n".join(
            f"{number:>{width}}  {line}"
            for number, line in enumerate(selected, arguments.start_line)
        )

        relative = self._sandbox.relative(target)
        header = f"{relative} (lines {arguments.start_line}–{end} of {total})"
        return self.ok(f"{header}\n{body}", started=started)


# ---------------------------------------------------------------------------
# file.list
# ---------------------------------------------------------------------------


class FileListInput(BaseModel):
    path: str = Field(default=".", description="Directory to list, workspace-relative.")
    depth: int = Field(
        default=1,
        ge=1,
        le=3,
        description="How many levels below `path` to descend. 1 lists only its contents.",
    )
    max_entries: int = Field(default=200, ge=1, le=1000)


class FileListTool(Tool[FileListInput]):
    """What is in this directory?

    The gap this fills was structural rather than cosmetic. `project.inspect`
    answers "what kind of project is this" and `code.search` finds text that
    already exists — so an agent that wanted to know whether `tests/` had a
    `conftest.py`, or what lived under `src/`, had no way to ask. It had to
    guess a path and call `file.read` to find out, and a failed read is a
    consumed action and a consecutive-failure tick.

    Directories are marked and listed first; ignored trees (`.git`,
    `node_modules`, `__pycache__`, virtualenvs) are pruned during the walk
    rather than filtered afterwards, so a vendored dependency directory costs
    nothing to skip.
    """

    input_model = FileListInput

    contract = ToolContract(
        name="file.list",
        purpose=(
            "List what is inside a directory. Use it to discover which files "
            "exist before reading one. Set depth to see nested directories."
        ),
        allowed_phases=_READ_PHASES,
        risk=Risk.LOW,
        reversible=True,
        timeout_seconds=15.0,
        max_output_bytes=8_000,
    )

    def __init__(self, workspace: Path) -> None:
        self._sandbox = PathSandbox(workspace)

    async def run(self, arguments: FileListInput, cancel: CancellationToken) -> ToolResult:
        started = time.monotonic()
        cancel.raise_if_cancelled()

        try:
            root = self._sandbox.resolve(arguments.path)
        except PathEscape as exc:
            return self.failed(str(exc), started=started)

        if not root.exists():
            return self.failed(f"{arguments.path}: no such directory", started=started)
        if not root.is_dir():
            return self.failed(
                f"{arguments.path} is a file, not a directory; use file.read", started=started
            )

        try:
            entries = self._walk(root, arguments.depth, arguments.max_entries, cancel)
        except OSError as exc:
            return self.failed(f"{arguments.path}: {exc}", started=started)

        relative = self._sandbox.relative(root)
        if not entries:
            return self.ok(f"{relative} is empty (ignored files excluded)", started=started)

        capped = len(entries) > arguments.max_entries
        shown = entries[: arguments.max_entries]
        body = "\n".join(shown)
        header = f"{relative} — {len(shown)} entr{'y' if len(shown) == 1 else 'ies'}"
        if capped:
            header += f", capped at {arguments.max_entries}; narrow `path` to see the rest"
        return self.ok(f"{header}\n{body}", started=started)

    def _walk(self, root: Path, depth: int, limit: int, cancel: CancellationToken) -> list[str]:
        """Directories first, then files, each alphabetically, at every level."""
        collected: list[str] = []
        self._collect(root, root, depth, limit, collected, cancel)
        return collected

    def _collect(
        self,
        root: Path,
        directory: Path,
        depth: int,
        limit: int,
        collected: list[str],
        cancel: CancellationToken,
    ) -> None:
        if depth <= 0 or len(collected) > limit:
            return
        cancel.raise_if_cancelled()

        children = sorted(directory.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        for child in children:
            if len(collected) > limit:
                return
            if is_ignored(child.relative_to(root)):
                continue

            display = self._sandbox.relative(child)
            if child.is_dir():
                collected.append(f"  {display}/")
                self._collect(root, child, depth - 1, limit, collected, cancel)
            else:
                collected.append(f"  {display}{_size(child)}")


def _size(path: Path) -> str:
    """A file's size, rendered compactly. Absent when it cannot be read."""
    try:
        count = path.stat().st_size
    except OSError:  # pragma: no cover - defensive
        return ""
    if count < 1024:
        return f"  ({count} B)"
    if count < 1024 * 1024:
        return f"  ({count / 1024:.1f} KB)"
    return f"  ({count / (1024 * 1024):.1f} MB)"


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def read_only_tools(workspace: Path, *, use_git: bool = True) -> list[Tool[Any]]:
    """The read-only surface, ready to register with a gateway."""
    return [
        ProjectInspectTool(workspace, use_git=use_git),
        CodeSearchTool(workspace),
        FileListTool(workspace),
        FileReadTool(workspace),
    ]


def summarise_manifest(manifest_json: str) -> str:
    """Compress a repository manifest into a few lines of project facts.

    The manifest is JSON built for machines; a compiled frame has ~900 tokens
    for project facts and artifacts combined. This keeps the fields that change
    what the agent should do and drops the rest.
    """
    try:
        data = json.loads(manifest_json)
    except json.JSONDecodeError:
        return ""

    def listed(key: str, limit: int = 6) -> str:
        values = data.get(key) or []
        if not isinstance(values, list):
            return ""
        shown = [str(value) for value in values[:limit]]
        suffix = f" (+{len(values) - limit} more)" if len(values) > limit else ""
        return ", ".join(shown) + suffix

    rows = [
        ("Name", str(data.get("name", ""))),
        ("Languages", listed("languages")),
        ("Package managers", listed("package_managers")),
        ("Test frameworks", listed("test_frameworks")),
        ("Test commands", listed("test_commands")),
        ("Build commands", listed("build_commands")),
        ("Entry points", listed("entry_points", limit=4)),
        ("Major directories", listed("major_directories", limit=8)),
    ]
    return "\n".join(f"{label}: {value}" for label, value in rows if value)


__all__ = [
    "CodeSearchInput",
    "CodeSearchTool",
    "FileListInput",
    "FileListTool",
    "FileReadInput",
    "FileReadTool",
    "ProjectInspectInput",
    "ProjectInspectTool",
    "read_only_tools",
    "summarise_manifest",
]
