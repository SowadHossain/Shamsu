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
from shamsu.tools.documents import describe_unreadable, is_extractable
from shamsu.verification.probe import probe_syntax

#: How much diff to show back. A patch result is an observation competing for
#: hot context; a 400-line diff would evict the task itself.
MAX_DIFF_LINES = 120

#: How much of a file to quote back when an anchor missed. Smaller than the
#: diff budget: this rides on a *failure*, and the retry still needs room for
#: the file's real content alongside it.
MAX_ANCHOR_LINES = 80


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

    #: The line ending the file had. `previous_content` holds `\n` throughout,
    #: so restoring without this would undo the edit and silently reformat the
    #: file in the same move.
    newline: str = "\n"

    def apply(self, workspace: Path) -> None:
        """Restore the file to its pre-patch state."""
        target = workspace / self.path
        if not self.existed:
            target.unlink(missing_ok=True)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.previous_content, encoding="utf-8", newline=self.newline)


def _newline_of(raw: bytes) -> str:
    """The line ending a file already uses, so an edit does not change it.

    Text mode translates `\\n` to `os.linesep` on write, so on Windows every
    file the agent touched came out CRLF — including files in an LF repository,
    where the result is a diff on every line of a one-line edit. Writing LF
    unconditionally has the same fault in reverse, so the file's own convention
    decides, and only a genuinely new file gets the LF default.

    Ties go to LF: a file with no newline at all has no convention to preserve,
    and LF is what source is written in.
    """
    crlf = raw.count(b"\r\n")
    return "\r\n" if crlf > raw.count(b"\n") - crlf else "\n"


def _excerpt(content: str) -> str:
    """The file as an anchor source, numbered and bounded.

    Line numbers are for the reader, not for addressing — the tool still only
    accepts text anchors — so they are separated by a tab, making the anchor
    easy to copy without them.
    """
    lines = content.splitlines()
    shown = "\n".join(f"{n:>4}\t{line}" for n, line in enumerate(lines[:MAX_ANCHOR_LINES], 1))
    if len(lines) > MAX_ANCHOR_LINES:
        return (
            f"{shown}\n… ({len(lines) - MAX_ANCHOR_LINES} more line(s); use file.read to see them)"
        )
    return shown


#: A document is a *source*, not a target.
#:
#: A live PRD build made this concrete: asked to build the system described in
#: `OpenBazaar_Marketplace_PRD.docx`, the plan named the `.docx` as the file
#: every step would change, and `file.patch` spent the whole run failing with
#: `cannot read: 'utf-8' codec` on a zip archive. The model had no other file to
#: name, because the specification was the only thing in the workspace.
#:
#: Refused here, with a message that says what the file is *for*, so the model
#: is redirected rather than merely stopped.
def _refuse_document(path: str, target: Path) -> str | None:
    """Why this path may not be written, or None if it may."""
    if is_extractable(target):
        return (
            f"{path} is a document, not source code. It is the specification to "
            "read — call file.read on it — and the code you write from it goes "
            "in new source files. Editing it would destroy the requirements."
        )
    described = describe_unreadable(target)
    if described is not None:
        return f"{path} is {described} and cannot be edited as text."
    return None


class FilePatchInput(BaseModel):
    path: str = Field(min_length=1)
    mode: Literal["replace_text", "create", "replace_file", "append"] = "replace_text"

    find: str = Field(
        default="",
        description="Exact text to replace. Required for replace_text. Must be unique.",
    )
    replace: str = Field(default="", description="Replacement text for replace_text.")
    content: str = Field(default="", description="Full content for create/replace_file.")

    replace_all: bool = Field(
        default=False,
        description=(
            "Replace every occurrence of 'find' instead of requiring it to be "
            "unique. Use when the same text must change in several places."
        ),
    )

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
            # Adding to a file has no anchor, which is not a malformed call —
            # it is the single most common thing a small model wants to do, and
            # `replace_text` is merely the mode it defaulted into. The §31.1
            # suite lost three tasks here: the model had the right content
            # ("def subtract(a, b): return a - b", an Installation section) and
            # was refused for having nothing to anchor it to.
            #
            # `replace_text` with no `find` has no other possible reading, so
            # it is treated as the append it plainly is. The rewrite is
            # reported in the result, so the model learns the mode exists.
            if self.replace or self.content:
                return self.model_copy(
                    update={
                        "mode": "append",
                        "content": self.content or self.replace,
                        "replace": "",
                    }
                )
            raise ValueError(
                "replace_text needs 'find' (the exact text to replace). To add "
                "new text to the end of a file, use mode='append' with 'content'."
            )
        if self.mode == "append" and not self.content:
            raise ValueError("append requires 'content' — the text to add")
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
            "Edit one file. mode='replace_text' with an exact, unique 'find' "
            "anchor changes existing text; add replace_all=true to change every "
            "occurrence instead of requiring one. mode='append' with 'content' "
            "adds text to the end — use it to add a function, class or section. "
            "mode='create' with 'content' makes a new file."
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
        """Editing existing content requires having seen it. Adding does not.

        `replace_text` matches an anchor the model supplies, and a small model
        will supply one it never saw — which fails the exact match at best and
        silently hits the wrong span at worst. `replace_file` discards content
        wholesale, so it earns the same requirement.

        `create` writes a file that does not exist yet, and demanding a read of
        it would be asking for the impossible. `append` is exempt for the
        related reason: it names no anchor and destroys nothing, so there is no
        prior content it could be wrong about. Making it wait for a read would
        put the one turn back that having an append mode exists to save.
        """
        return () if arguments.mode in ("create", "append") else (arguments.path,)

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

        # A trailing separator names a *directory*, and this tool writes files.
        # Without this the path is silently truncated and a file is created with
        # the directory's name — which then blocks the real file: a live build
        # wrote `config/core/`, got a file called `config/core`, and every
        # subsequent `config/core/models.py` failed with "cannot create a file
        # when that file already exists". The same bug produced an empty file
        # named `marketplace/templates/marketplace` in a PRD run, and that empty
        # file still earned `file_changed`.
        if arguments.path.rstrip().endswith(("/", "\\")):
            return self.failed(
                f"{arguments.path} ends with a path separator, so it names a "
                "directory. Directories are created automatically for whatever "
                "file you write — give the full file path instead.",
                started=started,
            )

        refusal = _refuse_document(arguments.path, target)
        if refusal is not None:
            return self.failed(refusal, started=started)

        existed = target.exists()
        previous = ""
        newline = "\n"
        if existed:
            try:
                raw = target.read_bytes()
                previous = raw.decode("utf-8").replace("\r\n", "\n")
                newline = _newline_of(raw)
            except (OSError, UnicodeDecodeError) as exc:
                return self.failed(f"{arguments.path}: cannot read: {exc}", started=started)

        updated, error = self._compute(
            arguments, previous, existed, authored=self._authored(arguments.path)
        )
        if error is not None:
            return self.failed(error, started=started)

        # Before the bytes land, not after: a file that does not parse is not a
        # change, and writing it anyway would register FILE_CHANGED for
        # something broken. Refusing here also keeps the retry clean — the file
        # still does not exist, so `mode="create"` works the second time.
        broken = probe_syntax(arguments.path, updated, workspace=self._workspace)
        if broken is not None:
            return self.failed(broken, started=started)

        if updated == previous and existed:
            # Not a failure, but not a change either. Reporting it as success
            # would let a no-op patch register FILE_CHANGED evidence.
            return self.failed(
                f"{arguments.path} is already in the requested state; nothing was written",
                started=started,
            )

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(updated, encoding="utf-8", newline=newline)
        except OSError as exc:
            return self.failed(f"{arguments.path}: cannot write: {exc}", started=started)

        relative = self._sandbox.relative(target)
        self._undo.append(
            PatchUndo(path=relative, previous_content=previous, existed=existed, newline=newline)
        )

        return self.ok(self._render(relative, previous, updated, existed), started=started)

    # -- internals ---------------------------------------------------------

    def _authored(self, path: str) -> bool:
        """Did this run create that file from nothing?

        Answered from the undo stack, which already records `existed` per
        patch — the tool knows which files it brought into being, so nothing
        new has to be tracked to find out.
        """
        try:
            resolved = self._sandbox.relative(self._sandbox.resolve(path))
        except PathEscape:
            return False
        return any(undo.path == resolved and not undo.existed for undo in self._undo)

    @staticmethod
    def _compute(
        arguments: FilePatchInput, previous: str, existed: bool, *, authored: bool = False
    ) -> tuple[str, str | None]:
        """Produce the new content, or an error explaining why not."""
        if arguments.mode == "create":
            # Refusing this outright cost a live build its whole task. The
            # model wrote `tasks.py` with stubbed methods, then sent `create`
            # again carrying the *finished* implementation — correct content,
            # wrong mode — and was told to use `replace_text` instead. Three
            # steps failed behind that one refusal.
            #
            # Overwriting the agent's own draft is not the data loss this rule
            # guards against. v1 lost *the user's* work to blind whole-file
            # writes; a file this run created from nothing has no prior work in
            # it to lose, and the patch stays reversible either way. So the
            # refusal now turns on provenance rather than on mere existence.
            if existed and not authored:
                return "", (
                    f"{arguments.path} already exists; use mode='replace_text' to edit it "
                    "or mode='replace_file' to overwrite it"
                )
            return arguments.content, None

        if arguments.mode == "append":
            # Appending to a file that is not there is a create, not an error:
            # "add this function to pkg/calc.py" means the same thing whether
            # or not the file exists yet, and refusing costs a turn to learn a
            # distinction the caller did not care about.
            if not existed:
                return arguments.content, None
            separator = "" if previous.endswith("\n") or not previous else "\n"
            return f"{previous}{separator}{arguments.content}", None

        if arguments.mode == "replace_file":
            return arguments.content, None

        if not existed:
            return "", f"{arguments.path}: no such file; use mode='create' to make it"

        occurrences = previous.count(arguments.find)
        if occurrences == 0:
            # The old message said "read the file and copy the exact text",
            # which is correct and costs a turn — and a small model often spent
            # that turn guessing a second anchor rather than reading. Handing
            # back the real content makes the retry a rewrite of the anchor
            # instead of another guess, which is the difference between
            # recovering on the next call and burning the step's attempts.
            return "", (
                f"the 'find' text does not appear in {arguments.path}. "
                "Copy an anchor from the current content below, exactly, "
                "including indentation:\n"
                f"{_excerpt(previous)}"
            )
        if occurrences > 1 and not arguments.replace_all:
            # Ambiguity is refused rather than resolved by picking the first.
            # "It edited the wrong one" is far worse than "it asked again".
            #
            # But refusing without offering a way to say "all of them" strands
            # a task whose whole point is a repeated edit. Asked to correct
            # three identical `DEFAULT now` columns in one schema, the model
            # was told to disambiguate an edit it did not want disambiguated,
            # could not build three unique anchors, and burned the step. That
            # is this repository's oldest recurring failure — a *correct*
            # intention refused on a technicality — so the message now names
            # the flag that expresses it.
            return "", (
                f"the 'find' text appears {occurrences} times in {arguments.path}. "
                "Include more surrounding context so it matches exactly once, "
                f"or set replace_all=true to change all {occurrences}."
            )

        if arguments.replace_all:
            return previous.replace(arguments.find, arguments.replace), None
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


class FileRemoveInput(BaseModel):
    path: str = Field(min_length=1, description="File to delete, or to move away from.")

    destination: str = Field(
        default="",
        description="Set this to move/rename instead of deleting: the new path.",
    )

    acknowledge_delete: bool = Field(
        default=False,
        description="Required for deletion (not for a move): confirms the file is removed.",
    )

    @model_validator(mode="after")
    def _check_removal(self) -> FileRemoveInput:
        """A deletion must be asked for; a move need not be.

        Moving preserves the content and deleting does not, so only deleting
        earns the extra word. Same reasoning as `replace_file`, and the same
        reason a move is exempt: a confirmation that fires on every harmless
        call teaches the caller to set the flag by reflex.
        """
        if not self.destination and not self.acknowledge_delete:
            raise ValueError(
                "deleting removes the file; set acknowledge_delete=true, or set "
                "'destination' to move it instead"
            )
        if self.destination and self.destination == self.path:
            raise ValueError("'destination' is the same as 'path'; nothing to do")
        return self


class FileRemoveTool(Tool[FileRemoveInput]):
    """Delete a file, or move it to a new path.

    The gap that made "create, edit, delete" only two thirds true: `file.patch`
    creates, replaces and appends, and nothing could remove anything. A refactor
    that splits a module cannot finish without deleting the original, and an
    agent that cannot delete works around it by emptying the file — which leaves
    a zero-byte import target that still resolves and fails much later.

    **A move is a delete and a create, offered as one call anyway.** Renaming
    through two tools leaves a window where the code exists twice or not at all,
    and costs a turn the model routinely will not spend.

    Reversible on the same terms as a patch: `PatchUndo` already distinguishes
    "restore this content" from "delete it again", which is exactly what undoing
    a removal and undoing a move respectively require.
    """

    input_model = FileRemoveInput

    contract = ToolContract(
        name="file.remove",
        purpose=(
            "Delete a file (set acknowledge_delete=true), or move/rename it by "
            "setting 'destination'. Use after splitting or replacing a module."
        ),
        allowed_phases=frozenset({Phase.AUTHOR, Phase.REPAIR}),
        # Higher than a patch: a bad edit leaves something to read, a bad
        # deletion leaves nothing.
        risk=Risk.HIGH,
        reversible=True,
        timeout_seconds=20.0,
        max_output_bytes=2_000,
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

    def write_targets(self, arguments: FileRemoveInput) -> tuple[str, ...]:
        """Both ends of a move are writes; a `WriteScope` must cover each."""
        if arguments.destination:
            return (arguments.path, arguments.destination)
        return (arguments.path,)

    def requires_prior_read(self, arguments: FileRemoveInput) -> tuple[str, ...]:
        """Removing requires having seen what is being removed.

        Stricter than `file.patch`, which exempts `create` and `append` because
        they add rather than take away. There is no such case here: every call
        destroys the file at `path` or moves it out from under whatever imports
        it, and neither should happen on the strength of a filename alone.
        """
        return (arguments.path,)

    @property
    def undo_stack(self) -> list[PatchUndo]:
        """Applied removals, oldest first."""
        return list(self._undo)

    def rollback_all(self) -> list[PatchUndo]:
        """Undo every removal, newest first."""
        undone: list[PatchUndo] = []
        while self._undo:
            undo = self._undo.pop()
            undo.apply(self._workspace)
            undone.append(undo)
        return undone

    async def run(self, arguments: FileRemoveInput, cancel: CancellationToken) -> ToolResult:
        started = time.monotonic()
        cancel.raise_if_cancelled()

        try:
            target = self._sandbox.resolve(arguments.path)
            destination = (
                self._sandbox.resolve(arguments.destination) if arguments.destination else None
            )
        except PathEscape as exc:
            return self.failed(str(exc), started=started)

        if target.is_dir():
            return self.failed(
                f"{arguments.path} is a directory; this removes one file at a time",
                started=started,
            )
        if not target.exists():
            return self.failed(f"{arguments.path}: no such file", started=started)

        refusal = _refuse_document(arguments.path, target)
        if refusal is not None:
            return self.failed(refusal, started=started)
        if destination is not None and destination.exists():
            return self.failed(
                f"{arguments.destination} already exists; remove it first or choose another name",
                started=started,
            )

        try:
            raw = target.read_bytes()
            # Normalised on the way in, exactly as `file.patch` does, because
            # `PatchUndo` re-applies the file's newline on the way out. Keeping
            # the CRLF here and translating again on restore writes `\r\r\n`,
            # which reads back as a blank line between every line of the file.
            previous = raw.decode("utf-8").replace("\r\n", "\n")
        except (OSError, UnicodeDecodeError) as exc:
            # Refused rather than removed blind: the undo record holds text, so
            # a file that cannot be decoded is one this tool cannot put back,
            # and `reversible=True` on the contract has to stay true.
            return self.failed(
                f"{arguments.path}: cannot be read, so it cannot be removed reversibly ({exc})",
                started=started,
            )

        relative = self._sandbox.relative(target)
        newline = _newline_of(raw)

        try:
            if destination is not None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                target.replace(destination)
            else:
                target.unlink()
        except OSError as exc:
            return self.failed(f"{arguments.path}: cannot remove: {exc}", started=started)

        self._undo.append(
            PatchUndo(path=relative, previous_content=previous, existed=True, newline=newline)
        )
        if destination is not None:
            moved = self._sandbox.relative(destination)
            # Undoing the *new* file means deleting it, which `existed=False`
            # says. Appended after the restore record so `rollback_all`, which
            # unwinds newest first, removes the copy before restoring the
            # original — the other order leaves both.
            self._undo.append(PatchUndo(path=moved, previous_content="", existed=False))
            return self.ok(f"Moved {relative} to {moved}", started=started)

        return self.ok(
            f"Deleted {relative} ({len(previous.splitlines())} line(s))", started=started
        )


__all__ = [
    "MAX_ANCHOR_LINES",
    "MAX_DIFF_LINES",
    "FilePatchInput",
    "FilePatchTool",
    "FileRemoveInput",
    "FileRemoveTool",
    "PatchUndo",
]
