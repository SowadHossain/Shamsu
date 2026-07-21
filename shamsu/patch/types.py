"""
Structured types for SHAMSU's Patch/File Mutation Engine.

`ChangePlan` is the model output contract (see shamsu/patch/engine.py's
`PatchEngine.execute_change_request`): the model proposes a change_plan +
an optional unified diff, PatchEngine validates every field itself before
touching disk. Nothing here is ever trusted blindly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

OPERATION_KINDS = (
    "create_file",
    "edit_file",
    "apply_patch",
    "rename_file",
    "move_file",
    "delete_file",
    "create_directory",
)

DESTRUCTIVE_OPS = {"delete_file", "rename_file", "move_file"}


class ChangeRequestError(ValueError):
    """Raised when a model's structured change request is malformed."""


@dataclass(frozen=True)
class Operation:
    op: str
    path: str
    dest_path: str = ""
    reason: str = ""
    content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "path": self.path,
            "dest_path": self.dest_path,
            "reason": self.reason,
            "content": self.content,
        }


@dataclass(frozen=True)
class ChangePlan:
    reason: str
    operations: list[Operation] = field(default_factory=list)
    verification_command: str = ""
    destructive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "operations": [op.to_dict() for op in self.operations],
            "verification_command": self.verification_command,
            "destructive": self.destructive,
        }

    @property
    def touched_paths(self) -> list[str]:
        paths: list[str] = []
        for op in self.operations:
            if op.path and op.path not in paths:
                paths.append(op.path)
            if op.dest_path and op.dest_path not in paths:
                paths.append(op.dest_path)
        return paths


@dataclass(frozen=True)
class ChangeRequest:
    change_plan: ChangePlan
    patch: str = ""


@dataclass
class VerificationOutcome:
    ran: bool
    command: str = ""
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error_packet: dict[str, Any] | None = None
    signature: str = ""
    stalled: bool = False

    @property
    def passed(self) -> bool:
        return self.ran and self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "command": self.command,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error_packet": self.error_packet,
            "signature": self.signature,
            "stalled": self.stalled,
        }


@dataclass
class MutationResult:
    ok: bool
    status: str = ""
    transaction_id: str = ""
    reason: str = ""
    touched_files: list[str] = field(default_factory=list)
    error: str = ""
    verification: VerificationOutcome | None = None
    rollback_available: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "transaction_id": self.transaction_id,
            "reason": self.reason,
            "touched_files": self.touched_files,
            "error": self.error,
            "verification": self.verification.to_dict() if self.verification else None,
            "rollback_available": self.rollback_available,
        }

    def __bool__(self) -> bool:
        return self.ok


def apply_diff_with_result(
    patch_engine: Any,
    diff_text: str,
    workspace_root: Any,
) -> MutationResult:
    """Apply a diff without discarding a structured engine result.

    Third-party and test patch engines may still implement the original boolean
    interface, so keep that path working while preferring the richer contract.
    """
    apply_result = getattr(patch_engine, "apply_result", None)
    if callable(apply_result):
        result = apply_result(diff_text, workspace_root)
        if isinstance(result, MutationResult):
            return result

    applied = bool(patch_engine.apply(diff_text, workspace_root))
    last_result = getattr(patch_engine, "last_apply_result", None)
    if isinstance(last_result, MutationResult):
        return last_result
    return MutationResult(
        ok=applied,
        status="applied" if applied else "apply_failed",
        error="" if applied else "Patch engine returned failure without details.",
    )


def _require_str(payload: dict[str, Any], key: str, *, required: bool = True) -> str:
    value = payload.get(key, "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ChangeRequestError(f"'{key}' must be a string, got {type(value).__name__}.")
    if required and not value.strip():
        raise ChangeRequestError(f"'{key}' is required and cannot be empty.")
    return value


def parse_operation(raw: Any) -> Operation:
    if not isinstance(raw, dict):
        raise ChangeRequestError(f"Each operation must be an object, got {type(raw).__name__}.")
    op = _require_str(raw, "op")
    if op not in OPERATION_KINDS:
        raise ChangeRequestError(f"Unknown operation '{op}'. Must be one of {OPERATION_KINDS}.")
    path = _require_str(raw, "path")
    dest_path = _require_str(raw, "dest_path", required=False)
    if op in {"rename_file", "move_file"} and not dest_path.strip():
        raise ChangeRequestError(f"Operation '{op}' requires 'dest_path'.")
    reason = _require_str(raw, "reason", required=False)
    content = _require_str(raw, "content", required=False)
    return Operation(op=op, path=path, dest_path=dest_path, reason=reason, content=content)


def parse_change_plan(raw: Any) -> ChangePlan:
    if not isinstance(raw, dict):
        raise ChangeRequestError(f"'change_plan' must be an object, got {type(raw).__name__}.")
    reason = _require_str(raw, "reason")
    raw_operations = raw.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations:
        raise ChangeRequestError("'change_plan.operations' must be a non-empty list.")
    operations = [parse_operation(item) for item in raw_operations]
    verification_command = _require_str(raw, "verification_command", required=False)
    destructive = raw.get("destructive", False)
    if not isinstance(destructive, bool):
        raise ChangeRequestError("'destructive' must be a boolean.")
    implied_destructive = any(op.op in DESTRUCTIVE_OPS for op in operations)
    return ChangePlan(
        reason=reason,
        operations=operations,
        verification_command=verification_command,
        destructive=destructive or implied_destructive,
    )


def parse_change_request(raw: Any) -> ChangeRequest:
    """Never trust model output blindly: validate the full structured
    contract before PatchEngine acts on any of it."""
    if not isinstance(raw, dict):
        raise ChangeRequestError(f"Change request must be an object, got {type(raw).__name__}.")
    if "change_plan" not in raw:
        raise ChangeRequestError("Change request is missing 'change_plan'.")
    change_plan = parse_change_plan(raw["change_plan"])
    patch = raw.get("patch", "")
    if patch is None:
        patch = ""
    if not isinstance(patch, str):
        raise ChangeRequestError(f"'patch' must be a string, got {type(patch).__name__}.")
    # apply_patch has no content fallback - it always needs the diff. create_file/
    # edit_file may instead carry inline 'content' (checked against the diff's
    # actual file list by the engine, since that's the only place both are known).
    needs_patch = any(op.op == "apply_patch" for op in change_plan.operations)
    if needs_patch and not patch.strip():
        raise ChangeRequestError("Change plan includes apply_patch but 'patch' is empty.")
    return ChangeRequest(change_plan=change_plan, patch=patch)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
