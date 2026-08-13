"""Mutating tools: `file.patch`.

The first tools in v2 that change anything. Two decisions shape them.

**Anchored replacement, not line numbers.** A patch says "replace this exact
text with that text". Line numbers drift the moment anything above them
changes, so a line-addressed edit computed from a stale read silently
corrupts the wrong region. An anchor that no longer matches simply fails,
which is the honest outcome.

**Every edit is reversible.** The tool captures the file's prior content and
returns it in a `PatchUndo`, so a rollback needs no git and works on untracked
files. `reversible=True` on the contract is a claim the runtime relies on when
deciding whether a step can be retried; it has to be true.

Whole-file overwrite is available but deliberately awkward to reach: it needs
`mode="replace_file"` and an explicit acknowledgement that the previous content
is discarded. v1 defaulted to whole-file writes and lost work that way.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.enums import ArtifactKind, EvidenceKind, Phase, Risk
from shamsu.interfaces.tools import ToolContract, ToolResult
from shamsu.security.paths import PathEscape, PathSandbox
from shamsu.tools.base import Tool

#: How much diff to show back. A patch result is an observation competing for
#: hot context; a 400-line diff would evict the task itself.
MAX_DIFF_LINES = 120


@dataclass(frozen=True)
class PatchUndo:
    """Everything needed to put a file back exactly as it was.

    `existed` distinguishes "restore this content" from "delete it again",
    which a content string alone cannot express — and getting that wrong leaves
    an empty file behind where there was none.
    """

    path: str
    previous_content: str
    existed: bool

    def apply(self, workspace: Path) -> None:
        """Restore the file to its pre-patch state."""
        target = workspace / self.path
        if not self.existed:
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.previous_content, encoding="utf-8")


class FilePatchInput(BaseModel):
    path: str = Field(min_length=1)
    mode: Literal["replace_text", "create", "replace_file"] = "replace_text"

    find: str = Field(
        default="",
        description="Exact text to replace. Required for replace_text. Must be unique.",
    )
    replace: str = Field(default="", description="Replacement text for replace_text.")
    content: str = Field(default="", description="Full content for create/replace_file.")

    acknowledge_overwrite: bool = Field(
        default=False,
        description="Required for replace_file: confirms the previous content is discarded.",
    )

    @model_validator(mode="after")
    def _check_mode(self) -> FilePatchInput:
        """Reject combinations the schema alone cannot express.

        Caught here rather than in `run`, so the gateway refuses before any
        side effect and the mutation budget is not spent on a malformed call.
        """
        if self.mode == "replace_text" and not self.find:
            raise ValueError("replace_text requires a non-empty 'find'")
        if self.mode == "replace_file" and not self.acknowledge_overwrite:
            raise ValueError(
                "replace_file discards the file's previous content; set "
                "acknowledge_overwrite=true, or use replace_text for a targeted edit"
            )
        return self


class FilePatchTool(Tool[FilePatchInput]):
    """Apply a targeted, reversible edit to one file."""

    input_model = FilePatchInput

    contract = ToolContract(
        name="file.patch",
        purpose=(
            "Edit one file. Prefer mode='replace_text' with an exact, unique "
            "'find' anchor. Use 'create' for a new file."
        ),
        allowed_phases=frozenset({Phase.AUTHOR, Phase.REPAIR}),
        risk=Risk.MEDIUM,
        reversible=True,
        timeout_seconds=20.0,
        max_output_bytes=8_000,
        produces_evidence=frozenset({EvidenceKind.FILE_CHANGED}),
        invalidates=frozenset(
            {ArtifactKind.MODULE_CARD, ArtifactKind.SYMBOL_CARD, ArtifactKind.TEST_MAP}
        ),
        mutating=True,
    )

    def __init__(self, workspace: Path) -> None:
        self._workspace = Path(workspace).resolve()
        self._sandbox = PathSandbox(workspace)
        self._undo: list[PatchUndo] = []

    def write_targets(self, arguments: FilePatchInput) -> tuple[str, ...]:
        """The one file this patch would change.

        Reported as given, not as resolved: a `WriteScope` compares against
        workspace-relative paths, and `run` refuses anything that escapes the
        sandbox before it can be written anyway.
        """
        return (arguments.path,)

    def requires_prior_read(self, arguments: FilePatchInput) -> tuple[str, ...]:
        """Editing existing content requires having seen it. Creating does not.

        `replace_text` matches an anchor the model supplies, and a small model
        will supply one it never saw — which fails the exact match at best and
        silently hits the wrong span at worst. `replace_file` discards content
        wholesale, so it earns the same requirement. `create` writes a file
        that does not exist yet, and demanding a read of it would be asking for
        the impossible.
        """
        return () if arguments.mode == "create" else (arguments.path,)

    @property
    def undo_stack(self) -> list[PatchUndo]:
        """Applied patches, oldest first. The runtime uses this to roll back."""
        return list(self._undo)

    def rollback_last(self) -> PatchUndo | None:
        """Undo the most recent patch."""
        if not self._undo:
            return None
        undo = self._undo.pop()
        undo.apply(self._workspace)
        return undo

    def rollback_all(self) -> list[PatchUndo]:
        """Undo every patch, newest first.

        Reverse order matters: two patches to the same file must unwind in the
        order they were applied or the earlier snapshot wins and the later one
        is lost.
        """
        undone: list[PatchUndo] = []
        while self._undo:
            undo = self._undo.pop()
            undo.apply(self._workspace)
            undone.append(undo)
        return undone

    async def run(self, arguments: FilePatchInput, cancel: CancellationToken) -> ToolResult:
        started = time.monotonic()
        cancel.raise_if_cancelled()

        try:
            target = self._sandbox.resolve(arguments.path)
        except PathEscape as exc:
            return self.failed(str(exc), started=started)

        if target.is_dir():
            return self.failed(f"{arguments.path} is a directory", started=started)

        existed = target.exists()
        previous = ""
        if existed:
            try:
                previous = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return self.failed(f"{arguments.path}: cannot read: {exc}", started=started)

        updated, error = self._compute(arguments, previous, existed)
        if error is not None:
            return self.failed(error, started=started)

        if updated == previous and existed:
            # Not a failure, but not a change either. Reporting it as success
            # would let a no-op patch register FILE_CHANGED evidence.
            return self.failed(
                f"{arguments.path} is already in the requested state; nothing was written",
                started=started,
            )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return self.failed(f"{arguments.path}: cannot write: {exc}", started=started)

        relative = self._sandbox.relative(target)
        self._undo.append(PatchUndo(path=relative, previous_content=previous, existed=existed))

        return self.ok(self._render(relative, previous, updated, existed), started=started)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _compute(arguments: FilePatchInput, previous: str, existed: bool) -> tuple[str, str | None]:
        """Produce the new content, or an error explaining why not."""
        if arguments.mode == "create":
            if existed:
                return "", (
                    f"{arguments.path} already exists; use mode='replace_text' to edit it "
                    "or mode='replace_file' to overwrite it"
                )
            return arguments.content, None

        if arguments.mode == "replace_file":
            return arguments.content, None

        if not existed:
            return "", f"{arguments.path}: no such file; use mode='create' to make it"

        occurrences = previous.count(arguments.find)
        if occurrences == 0:
            return "", (
                f"the 'find' text does not appear in {arguments.path}. "
                "Read the file and copy the exact text, including indentation."
            )
        if occurrences > 1:
            # Ambiguity is refused rather than resolved by picking the first.
            # "It edited the wrong one" is far worse than "it asked again".
            return "", (
                f"the 'find' text appears {occurrences} times in {arguments.path}. "
                "Include more surrounding context so it matches exactly once."
            )

        return previous.replace(arguments.find, arguments.replace, 1), None

    @staticmethod
    def _render(relative: str, previous: str, updated: str, existed: bool) -> str:
        """A unified diff of what changed, truncated to stay promptable."""
        if not existed:
            lines = updated.splitlines()
            body = "\n".join(f"+{line}" for line in lines[:MAX_DIFF_LINES])
            suffix = (
                f"\n… (+{len(lines) - MAX_DIFF_LINES} more lines)"
                if len(lines) > MAX_DIFF_LINES
                else ""
            )
            return f"Created {relative} ({len(lines)} line(s)):\n{body}{suffix}"

        diff = list(
            unified_diff(
                previous.splitlines(),
                updated.splitlines(),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
                lineterm="",
                n=3,
            )
        )
        shown = diff[:MAX_DIFF_LINES]
        suffix = (
            f"\n… ({len(diff) - MAX_DIFF_LINES} more diff line(s))"
            if len(diff) > MAX_DIFF_LINES
            else ""
        )
        return f"Patched {relative}:\n" + "\n".join(shown) + suffix


__all__ = ["MAX_DIFF_LINES", "FilePatchInput", "FilePatchTool", "PatchUndo"]
