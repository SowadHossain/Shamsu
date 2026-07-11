"""
Unified diff validation for SHAMSU patch workflows.
"""
from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.interfaces import IPatchEngine
from shamsu.patch import git_apply
from shamsu.patch.file_mutations import FileMutationOps
from shamsu.patch.formatter import run_formatter
from shamsu.patch.journal import MutationJournal
from shamsu.patch.safety import (
    MutationSafetyError,
    is_secret_file,
    validate_mutation_path,
)
from shamsu.patch.trash import TrashWorkspace
from shamsu.patch.transactions import TransactionWorkspace
from shamsu.patch.types import (
    ChangeRequestError,
    MutationResult,
    Operation,
    VerificationOutcome,
    parse_change_request,
)
from shamsu.patch.verifier import is_stalled, run_verification
from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.audit import AuditLogger
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.session.manager import SessionLogger
from shamsu.tools.codebase_memory import CodebaseMemoryAdapter
from shamsu.tools.executor import CommandRunner
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
        session_logger: SessionLogger | None = None,
        approval_manager: ApprovalManager | None = None,
        command_runner: CommandRunner | None = None,
        memory_adapter: CodebaseMemoryAdapter | None = None,
        action_ledger: ActionLedger | None = None,
    ) -> None:
        self.workspace_root = (workspace_root or Path.cwd()).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.approval_func = approval_func
        self.approval_manager = approval_manager or ApprovalManager(approval_func, session_logger)
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self.audit_logger = AuditLogger(self.workspace_root)
        self.memory_adapter = memory_adapter
        self.command_runner = command_runner or CommandRunner(
            self.workspace_root,
            approval_func=approval_func,
            session_logger=session_logger,
            approval_manager=self.approval_manager,
            action_ledger=action_ledger,
        )
        self.transactions = TransactionWorkspace(self.workspace_root)
        self.trash = TrashWorkspace(self.workspace_root)
        self.journal = MutationJournal(self.workspace_root)

    def validate_diff(self, diff_text: str) -> tuple[bool, str | None]:
        try:
            parse_unified_diff(diff_text, self.sandbox)
        except DiffValidationError as exc:
            return False, str(exc)
        return True, None

    def apply(self, diff_text: str, workspace_root: Path) -> bool:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.audit_logger = AuditLogger(self.workspace_root)
        try:
            patches = parse_file_patches(diff_text, self.sandbox)
        except DiffValidationError:
            self._log("patch.failed", {"error": "Diff validation failed"}, "Patch validation failed")
            self.audit_logger.log("patch_apply", "error", details={"error": "invalid diff"})
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
        self._log("patch.preview", {"files": [patch.display_path for patch in patches]}, request.description)
        if self.action_ledger:
            self.action_ledger.log_patch_planned(_patch_paths(patches))
        self.approval_manager.session_logger = self.session_logger
        if not self.approval_manager.ask(request):
            self._log("patch.denied", {"files": [patch.display_path for patch in patches]}, "Patch denied")
            self.audit_logger.log(
                "approval",
                "denied",
                affected_paths=_patch_paths(patches),
                details={"action_type": request.action_type, "preview": request.preview},
            )
            self.audit_logger.log("patch_apply", "denied", affected_paths=_patch_paths(patches))
            return False
        self.audit_logger.log(
            "approval",
            "approved",
            affected_paths=_patch_paths(patches),
            details={"action_type": request.action_type, "preview": request.preview},
        )

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
            self._log("patch.failed", {"files": [patch.display_path for patch in patches]}, "Patch failed")
            self.audit_logger.log("patch_apply", "error", affected_paths=_patch_paths(patches))
            return False
        _queue_code_memory_refresh(self.workspace_root)
        self._log("patch.applied", {"files": [patch.display_path for patch in patches]}, "Patch applied")
        self.audit_logger.log("patch_apply", "success", affected_paths=_patch_paths(patches))
        if self.action_ledger:
            self.action_ledger.log_patch_applied(_patch_paths(patches))
        return True

    def rollback(self, file_path: Path) -> bool:
        try:
            target = self.sandbox.validate(file_path)
        except SecurityError:
            return False
        backup = _backup_path(target)
        if not backup.exists():
            if self.action_ledger:
                self.action_ledger.log_rollback(str(file_path), False, "No backup available")
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(backup), str(target))
        if self.action_ledger:
            self.action_ledger.log_rollback(str(file_path), True)
        return True

    # -- structured model contract: transaction + verification pipeline ---------
    #
    # model proposes change -> validate -> transaction snapshot -> apply safely
    # -> formatter -> verification -> diagnostics on failure -> rollback
    # available -> success reported only after verification passes.

    def execute_change_request(self, payload: dict) -> MutationResult:
        """Entry point for the structured model output contract:
        {"change_plan": {...}, "patch": "unified diff"}. Never trusts the
        payload: every field is validated before anything touches disk."""
        try:
            request = parse_change_request(payload)
        except ChangeRequestError as exc:
            return MutationResult(ok=False, error=f"Rejected change request: {exc}")
        change_plan = request.change_plan
        patch_text = request.patch

        try:
            for path in change_plan.touched_paths:
                validate_mutation_path(self.sandbox, path)
        except MutationSafetyError as exc:
            return MutationResult(ok=False, error=str(exc))

        if patch_text.strip():
            ok, error = self.validate_diff(patch_text)
            if not ok:
                return MutationResult(ok=False, error=f"Invalid diff: {error}")
            if git_apply.available(self.workspace_root):
                git_ok, git_message = git_apply.check(patch_text, self.workspace_root)
                if not git_ok:
                    return MutationResult(ok=False, error=f"git apply --check rejected the patch: {git_message}")

        approval_request = self._build_change_plan_approval(change_plan, patch_text)
        if not self.approval_manager.ask(approval_request):
            self.audit_logger.log(
                "approval", "denied",
                affected_paths=change_plan.touched_paths,
                details={"action_type": approval_request.action_type, "reason": change_plan.reason},
            )
            return MutationResult(ok=False, error="Change request denied by user.")
        self.audit_logger.log(
            "approval", "approved",
            affected_paths=change_plan.touched_paths,
            details={"action_type": approval_request.action_type, "reason": change_plan.reason},
        )

        transaction_id = self.transactions.begin(
            change_plan.reason,
            [op.to_dict() for op in change_plan.operations],
            change_plan.destructive,
        )
        if self.action_ledger:
            self.action_ledger.log_mutation_started(transaction_id, change_plan.reason)
        mutation_ops = FileMutationOps(
            self.workspace_root, self.transactions, self.trash, memory_adapter=self.memory_adapter
        )

        try:
            touched_files: list[str] = []
            diff_paths: set[str] = set()
            if patch_text.strip():
                diff_paths = {patch.display_path for patch in parse_file_patches(patch_text, self.sandbox)}

            for op in change_plan.operations:
                if op.op == "apply_patch" and op.path not in diff_paths:
                    raise DiffValidationError(
                        f"apply_patch operation declares path {op.path} but the supplied patch has no hunk for it."
                    )
                if op.op == "edit_file" and op.path not in diff_paths and not op.content.strip():
                    raise DiffValidationError(
                        f"edit_file for {op.path} needs either inline 'content' or a matching patch hunk."
                    )

            if patch_text.strip():
                touched_files.extend(
                    _apply_patch_with_transaction(self.sandbox, self.transactions, transaction_id, patch_text)
                )

            for op in change_plan.operations:
                if op.op == "apply_patch":
                    continue  # already applied via the unified diff above
                if op.op in {"create_file", "edit_file"} and op.path in diff_paths:
                    continue  # this file's content already came from the unified diff
                outcome = self._run_file_operation(mutation_ops, transaction_id, op)
                if not outcome.ok:
                    raise DiffValidationError(outcome.error)
                touched_files.append(outcome.path)
                if outcome.dest_path:
                    touched_files.append(outcome.dest_path)
        except (DiffValidationError, MutationSafetyError, OSError) as exc:
            manifest = self.transactions.finalize(transaction_id, "failed", error=str(exc))
            self.audit_logger.log("patch_apply", "error", affected_paths=manifest.get("touched_files", []))
            if self.action_ledger:
                self.action_ledger.log_mutation_finished(
                    transaction_id, "failed", manifest.get("touched_files", []),
                    rollback_available=True, error=str(exc),
                )
            return MutationResult(
                ok=False, transaction_id=transaction_id, reason=change_plan.reason,
                touched_files=manifest.get("touched_files", []),
                error=str(exc), rollback_available=True,
            )

        formatter_result = run_formatter(self.command_runner, self.workspace_root, touched_files)
        self._log("patch.formatter", formatter_result, "Formatter step finished.")

        verification = VerificationOutcome(ran=False)
        if change_plan.verification_command.strip():
            verification = run_verification(self.command_runner, self.workspace_root, change_plan.verification_command)
            previous_entry = self.journal.last() or {}
            previous_verification = previous_entry.get("verification") or {}
            verification.stalled = is_stalled(previous_verification.get("signature", ""), verification)
        self.transactions.save_patch(transaction_id, patch_text)
        self.transactions.save_verification(transaction_id, verification.to_dict())

        verification_ok = (not change_plan.verification_command.strip()) or verification.passed
        status = "applied" if verification_ok else "verification_failed"
        manifest = self.transactions.finalize(transaction_id, status, error="" if verification_ok else "Verification failed.")
        self.audit_logger.log(
            "patch_apply", "success" if verification_ok else "error",
            affected_paths=manifest.get("touched_files", []),
        )

        if verification_ok:
            _queue_code_memory_refresh(self.workspace_root)
            self._log("patch.applied", {"transaction_id": transaction_id, "files": touched_files}, "Change applied and verified.")
            if self.action_ledger:
                self.action_ledger.log_mutation_finished(
                    transaction_id, "applied", manifest.get("touched_files", []), rollback_available=True
                )
            return MutationResult(
                ok=True, transaction_id=transaction_id, reason=change_plan.reason,
                touched_files=manifest.get("touched_files", []), verification=verification,
                rollback_available=True,
            )

        self._log("patch.failed", {"transaction_id": transaction_id, "files": touched_files}, "Verification failed.")
        if self.action_ledger:
            self.action_ledger.log_mutation_finished(
                transaction_id, "verification_failed", manifest.get("touched_files", []),
                rollback_available=True, error="Verification failed.",
            )
        return MutationResult(
            ok=False, transaction_id=transaction_id, reason=change_plan.reason,
            touched_files=manifest.get("touched_files", []),
            error="Verification command exited non-zero; change was applied but is not reported as successful.",
            verification=verification, rollback_available=True,
        )

    def rollback_transaction(self, transaction_id: str) -> tuple[bool, str]:
        from shamsu.patch.rollback import rollback_transaction

        ok, message = rollback_transaction(self.workspace_root, transaction_id)
        if self.action_ledger:
            self.action_ledger.log_rollback(transaction_id, ok, message)
        return ok, message

    def _run_file_operation(self, mutation_ops: FileMutationOps, transaction_id: str, op: Operation):
        if op.op == "create_file":
            return mutation_ops.create_file(transaction_id, op.path, op.content)
        if op.op == "edit_file":
            return mutation_ops.edit_file(transaction_id, op.path, op.content)
        if op.op == "rename_file":
            return mutation_ops.rename_file(transaction_id, op.path, op.dest_path)
        if op.op == "move_file":
            return mutation_ops.move_file(transaction_id, op.path, op.dest_path)
        if op.op == "delete_file":
            return mutation_ops.delete_file(transaction_id, op.path)
        if op.op == "create_directory":
            return mutation_ops.create_directory(transaction_id, op.path)
        raise DiffValidationError(f"Unsupported operation: {op.op}")

    def _build_change_plan_approval(self, change_plan, patch_text: str) -> ApprovalRequest:
        secret_touched = [path for path in change_plan.touched_paths if is_secret_file(path)]
        description = change_plan.reason
        if secret_touched:
            description += f" (WARNING: touches secret-like file(s): {', '.join(secret_touched)})"
        return ApprovalRequest(
            action_type="file_delete" if change_plan.destructive else "file_edit",
            description=description,
            risk_level="high" if (change_plan.destructive or secret_touched) else "medium",
            preview=patch_text or "\n".join(str(op.to_dict()) for op in change_plan.operations),
            working_dir=str(self.workspace_root),
            reason="Structured change request modifies files inside the selected workspace.",
        )

    def _log(self, event_type: str, payload: dict, summary: str) -> None:
        if self.session_logger:
            self.session_logger.log(event_type, payload, summary, workflow_id="patch")

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


def _patch_paths(patches: list[FilePatch]) -> list[str]:
    return [patch.display_path for patch in patches]


def _queue_code_memory_refresh(workspace_root: Path) -> None:
    """Best-effort: never let code-memory bookkeeping break a successful patch."""
    try:
        from shamsu.abstract.service import AbstractService

        AbstractService(workspace_root).queue_refresh()
    except Exception:
        pass


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

    # Trust the body, not the @@ header counts. Local models routinely emit a
    # wrong line count (e.g. "@@ -3,7 +3,7 @@" for a one-line change), and
    # rejecting on that threw away otherwise-correct patches - it was a leading
    # cause of the "invalid diff / diff not match" failures. Recompute the
    # counts from the lines actually present (what `git apply --recount` does)
    # and keep only the declared start positions, which _apply_hunks anyway
    # re-locates by context.
    return (
        HunkPatch(
            old_start=int(match.group("old_start")),
            old_count=old_seen,
            new_start=int(match.group("new_start")),
            new_count=new_seen,
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
        # The "old side" of the hunk: context and deleted lines, in order. This
        # is what must be found in the file - the +lines are new and aren't
        # matched against anything.
        old_block = [
            line[1:] if line and line[0] in {" ", "-"} else ""
            for line in hunk.lines
            if not line.startswith("\\") and (line == "" or line[0] in {" ", "-"})
        ]
        start = _locate_hunk(original_lines, old_block, hunk.old_start - 1, cursor)
        if start is None:
            raise DiffValidationError("Patch context does not match target file.")
        output.extend(original_lines[cursor:start])
        cursor = start
        for line in hunk.lines:
            if line.startswith("\\"):
                continue
            marker = " " if line == "" else line[0]
            content = "" if line == "" else line[1:]
            if marker == " ":
                # Emit the file's own line, not the diff's copy of it, so a
                # context line that differed only in trailing whitespace keeps
                # the real bytes instead of being rewritten by the patch.
                output.append(original_lines[cursor] if cursor < len(original_lines) else content)
                cursor += 1
            elif marker == "-":
                cursor += 1
            elif marker == "+":
                output.append(content)
            else:
                raise DiffValidationError(f"Invalid hunk line marker: {line}")
    output.extend(original_lines[cursor:])
    return output


def _locate_hunk(
    original_lines: list[str], old_block: list[str], hint: int, min_start: int
) -> int | None:
    """Find where a hunk's old side sits in the file.

    LLM-produced diffs routinely carry wrong ``@@`` line numbers and slight
    trailing-whitespace drift on context lines, which made a strict
    anchor-at-old_start applier reject otherwise-correct patches. Instead we
    search the file (at or after ``min_start`` so hunks stay ordered) for the
    block, comparing on ``rstrip()`` so trailing-whitespace/line-ending drift
    is tolerated, and pick the occurrence closest to the declared position.
    Leading indentation is still matched exactly, so a patch is never applied
    at a location whose code merely looks similar. Returns None when there is
    no match, which makes the caller reject the patch (and roll back)."""
    span = len(old_block)
    anchor = max(hint, min_start)
    if span == 0:
        # Pure insertion: nothing to match, anchor at the declared position
        # (clamped into the still-unconsumed region of the file).
        return min(max(anchor, min_start), len(original_lines))
    target = [line.rstrip() for line in old_block]
    candidates = [
        start
        for start in range(min_start, len(original_lines) - span + 1)
        if [line.rstrip() for line in original_lines[start:start + span]] == target
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda start: abs(start - anchor))


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


def _apply_patch_with_transaction(
    sandbox: Sandbox,
    transactions: TransactionWorkspace,
    transaction_id: str,
    diff_text: str,
) -> list[str]:
    """Apply a unified diff's hunks (reusing the same tested `_apply_hunks`
    transform as the legacy `.apply()` path) while recording every touched
    file's before/after state in the transaction store instead of a
    throwaway `.bak` sibling file."""
    patches = parse_file_patches(diff_text, sandbox)
    touched: list[str] = []
    for patch in patches:
        old_target = None if patch.old_path == "/dev/null" else sandbox.validate(patch.old_path)
        new_target = None if patch.new_path == "/dev/null" else sandbox.validate(patch.new_path)

        if patch.is_create:
            if new_target is None:
                raise DiffValidationError("Create patch is missing target path.")
            if new_target.exists():
                raise DiffValidationError(f"Cannot create existing file: {new_target}")
            transactions.backup_file(transaction_id, patch.new_path)
            new_lines = _apply_hunks([], patch.hunks)
            new_target.parent.mkdir(parents=True, exist_ok=True)
            _write_lines(new_target, new_lines)
            transactions.record_after(transaction_id, patch.new_path)
            touched.append(patch.new_path)
            continue

        if old_target is None or not old_target.is_file():
            raise DiffValidationError(f"Patch target does not exist: {old_target}")
        transactions.backup_file(transaction_id, patch.old_path)
        original_lines = old_target.read_text(encoding="utf-8").splitlines()
        new_lines = _apply_hunks(original_lines, patch.hunks)

        if patch.is_delete:
            old_target.unlink()
            transactions.record_after(transaction_id, patch.old_path)
            touched.append(patch.old_path)
            continue

        if new_target is None:
            raise DiffValidationError("Patch is missing output path.")
        renamed = old_target != new_target
        if renamed:
            transactions.backup_file(transaction_id, patch.new_path)
            new_target.parent.mkdir(parents=True, exist_ok=True)
            old_target.unlink()
        _write_lines(new_target, new_lines)
        transactions.record_after(transaction_id, patch.old_path)
        if renamed:
            transactions.record_after(transaction_id, patch.new_path)
        touched.append(patch.display_path)
    return touched
