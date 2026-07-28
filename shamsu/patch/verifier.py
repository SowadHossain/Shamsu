"""
Verification step of the Patch/File Mutation Engine.

Runs the model-supplied verification_command through the existing
CommandRunner (never bypassed - same sandboxing/approval/risk classification
as any other command), lets DiagnosticDigest (already wired into
CommandRunner) turn failures into a compact ErrorPacket, and exposes a
stable failure signature so repeated identical failures can be recognised
as a stall instead of retried blindly forever.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.interfaces import ICommandRunner
from shamsu.patch.types import VerificationOutcome


def run_verification(command_runner: ICommandRunner, workspace_root: Path, command: str) -> VerificationOutcome:
    command = (command or "").strip()
    if not command:
        return VerificationOutcome(ran=False, command="")

    action_ledger = getattr(command_runner, "action_ledger", None)
    verifier_id = ""
    if action_ledger is not None:
        verifier_id = action_ledger.verifier_id_for(command, "patch_verifier")
        action_ledger.log_verification_started(
            command,
            verifier_id=verifier_id,
            source="patch_verifier",
            required=True,
        )
    exit_code, stdout, stderr = command_runner.run(command, workspace_root)
    packet = getattr(command_runner, "last_error_packet", None)
    signature = packet.signature() if packet is not None else f"exit={exit_code}"
    if action_ledger is not None:
        action_ledger.log_verification_result(
            exit_code == 0,
            (packet.summary if packet is not None else ""),
            command=command,
            verifier_id=verifier_id,
            source="patch_verifier",
            required=True,
            exit_code=exit_code,
        )
    return VerificationOutcome(
        ran=True,
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        error_packet=packet.to_dict() if packet is not None else None,
        signature=signature,
    )


def is_stalled(previous_signature: str, outcome: VerificationOutcome) -> bool:
    """A stall guard against blind retry loops: true only when this
    verification failed AND its failure signature exactly matches the
    previous transaction's failure signature."""
    if outcome.passed or not outcome.signature:
        return False
    return bool(previous_signature) and previous_signature == outcome.signature
