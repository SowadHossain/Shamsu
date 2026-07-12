"""Verify gate — the one place a set of changed files is turned into an honest
verified / failed / unverifiable verdict.

The reliability design's contract: **never report success unless the change was
verified, or is explicitly unverifiable.** Small local models routinely
hallucinate success, so the loop must not trust a tool's `ok` or the model's own
summary. This module reuses the machinery that already exists — the deterministic
verifier-selection first written for `FreeformGenerator`, the strict
`RepairLoop`, `CommandVerifier`, and `LLMProposer` — behind a single entry point
callers can plug at the end of any write-producing flow.

Two entry points:
  * ``verify_and_repair`` — run the verifier and, on failure, drive the strict
    fix loop (needs a synchronous ``generate`` callable). Used by autonomous
    flows that can bridge async→sync in a worker thread.
  * ``verify_only`` — run the verifier once, no repair. Cheap and dependency-light
    (no model call), for surfacing an honest verdict on an interactive turn.

``default_verify_command`` is the single source of truth for *which* command
verifies a change; ``FreeformGenerator._default_verify`` delegates to it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.repair.loop import RepairLoop
from shamsu.repair.proposer_llm import GenerateJSON, LLMProposer
from shamsu.repair.types import RepairResult
from shamsu.repair.verifiers import CommandVerifier
from shamsu.session.manager import SessionLogger


class CommandRunnerLike(Protocol):
    def run(self, command: str, cwd: Path) -> tuple[int, str, str]: ...


@dataclass(frozen=True)
class VerifyOutcome:
    """The verdict on a set of changes.

    Exactly one of ``verified`` / ``unverifiable`` / (neither = failed) holds:
      * ``verified``      — a verifier ran and the workspace passed it.
      * ``unverifiable``  — no deterministic verifier exists for these changes;
        an honest, NON-failing outcome (report it, don't claim success).
      * neither           — a verifier ran and the workspace did NOT pass.
    """

    verified: bool
    unverifiable: bool = False
    exit_code: int | None = None
    command: str = ""
    summary: str = ""
    repair_result: RepairResult | None = None

    @property
    def failed(self) -> bool:
        return not self.verified and not self.unverifiable

    def status(self) -> str:
        if self.verified:
            return "verified"
        if self.unverifiable:
            return "unverifiable"
        return "failed"


def default_verify_command(
    changed_files: list[str],
    *,
    stack: str = "",
    stack_hint: str = "",
    python_bin: str | None = None,
    lightweight: bool = False,
) -> str:
    """Pick a trustworthy build/syntax verifier from the changed files (and an
    optional declared stack). Never a model-proposed command. Returns "" when
    nothing can deterministically verify the change.

    ``lightweight=True`` drops network/install steps (``pip install`` /
    ``npm install``) so the command is safe to run automatically on an
    interactive turn — a node build is treated as unverifiable in that mode.
    """
    stack_l = (stack or "").lower()
    hint = (stack_hint or "").lower()
    py_files = [f for f in changed_files if f.endswith(".py")]
    has_package_json = any(f.endswith("package.json") for f in changed_files)
    has_requirements = any(f.endswith("requirements.txt") for f in changed_files)

    is_node = (
        has_package_json
        or "node" in stack_l
        or "node" in hint
        or "vite" in stack_l
    )
    if is_node:
        # A node build needs an install step and can take minutes; too heavy to
        # run automatically on an interactive turn.
        return "" if lightweight else "npm install && npm run build"

    if py_files or "python" in stack_l or "django" in stack_l or hint in {"python", "django"}:
        pybin = python_bin or _default_python_bin()
        if has_requirements:
            # py_compile is a deterministic syntax gate; import-time execution is
            # unsafe to run blindly. Install deps first unless lightweight.
            prefix = "" if lightweight else "pip install -r requirements.txt && "
            return f"{prefix}{pybin} -m py_compile " + " ".join(py_files)
        if py_files:
            return f"{pybin} -m py_compile " + " ".join(py_files)
    return ""


def stack_of(changed_files: list[str]) -> str:
    """A coarse stack label ('node' / 'django' / 'python' / '') inferred from the
    changed files, for reporting and verifier selection."""
    if any(f.endswith("package.json") for f in changed_files) or any(
        f.endswith((".ts", ".tsx", ".jsx")) for f in changed_files
    ):
        return "node"
    if any(f.endswith("manage.py") or f.endswith("settings.py") for f in changed_files):
        return "django"
    if any(f.endswith((".py", "requirements.txt")) for f in changed_files):
        return "python"
    return ""


def verify_only(
    workspace: Path | str,
    changed_files: list[str],
    *,
    command_runner: CommandRunnerLike | None = None,
    stack: str = "",
    stack_hint: str = "",
    lightweight: bool = True,
    session_logger: SessionLogger | None = None,
) -> VerifyOutcome:
    """Run the deterministic verifier once (no repair loop, no model call) and
    return the honest verdict. Defaults to ``lightweight`` so it is safe to call
    automatically after an interactive turn."""
    workspace = Path(workspace).resolve()
    command = default_verify_command(
        changed_files, stack=stack, stack_hint=stack_hint, lightweight=lightweight
    )
    if not command:
        return VerifyOutcome(
            verified=False,
            unverifiable=True,
            summary="No deterministic verifier is available for these changes (UNVERIFIED).",
        )
    runner = command_runner or _default_runner(workspace, session_logger)
    run = CommandVerifier(command, runner, workspace).run()
    verified = run.exit_code == 0
    summary = (
        f"Verification passed: `{command}` (exit 0)."
        if verified
        else f"Verification FAILED: `{command}` (exit {run.exit_code})."
    )
    return VerifyOutcome(
        verified=verified,
        unverifiable=False,
        exit_code=run.exit_code,
        command=command,
        summary=summary,
    )


def verify_and_repair(
    workspace: Path | str,
    changed_files: list[str],
    *,
    generate: GenerateJSON,
    command_runner: CommandRunnerLike | None = None,
    max_attempts: int = 2,
    stack: str = "",
    stack_hint: str = "",
    session_logger: SessionLogger | None = None,
) -> VerifyOutcome:
    """Verify the changes and, on failure, drive the strict fix loop up to
    ``max_attempts`` times. ``generate`` is the synchronous ``(system, user,
    schema) -> str`` model adapter the loop's proposer uses; callers in an async
    context should run this in a worker thread (see ``LLMProposer``)."""
    workspace = Path(workspace).resolve()
    command = default_verify_command(changed_files, stack=stack, stack_hint=stack_hint)
    if not command:
        return VerifyOutcome(
            verified=False,
            unverifiable=True,
            summary="No deterministic verifier is available for these changes (UNVERIFIED).",
        )
    runner = command_runner or _default_runner(workspace, session_logger)
    verifier = CommandVerifier(command, runner, workspace)
    result = RepairLoop(
        workspace,
        verifier,
        LLMProposer(generate),
        max_attempts=max_attempts,
        session_logger=session_logger,
        digest=DiagnosticDigest(workspace),
    ).run()
    verified = result.exit_code == 0 and result.success
    return VerifyOutcome(
        verified=verified,
        unverifiable=False,
        exit_code=result.exit_code,
        command=command,
        summary=result.final_message,
        repair_result=result,
    )


def _default_python_bin() -> str:
    # `python3` is not reliably on PATH on Windows; `python` is. Elsewhere the
    # reverse is common, so prefer `python3`.
    return "python" if os.name == "nt" else "python3"


def _default_runner(workspace: Path, session_logger: SessionLogger | None) -> CommandRunnerLike:
    from shamsu.tools.executor import CommandRunner

    return CommandRunner(
        workspace,
        approval_func=lambda _request: True,
        session_logger=session_logger,
    )
