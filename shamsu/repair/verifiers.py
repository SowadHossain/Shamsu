"""Verifier adapters for the strict repair loop.

The RepairLoop treats the verifier's exit code + diagnostic set as the only
ground truth. `CommandVerifier` wraps the shared `CommandRunner` so any
stack-detected command (`npm run build`, `tsc --noEmit`, `pytest`,
`manage.py test`, ...) becomes a Verifier without a second execution path.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from shamsu.repair.loop import VerifyRun


class CommandRunnerLike(Protocol):
    def run(self, command: str, cwd: Path) -> tuple[int, str, str]: ...


class TestRunnerLike(Protocol):
    def run(self, project_cwd: Path | str = ".") -> "TestRunResultLike": ...


class TestRunResultLike(Protocol):
    failed: int
    raw_output: str


class CommandVerifier:
    """Runs a fixed shell command in a fixed cwd and reports (exit, out, err).

    Deliberately stateless beyond its command/cwd: the RepairLoop calls
    `run()` once per iteration and re-digests the output itself, so the same
    verifier instance is reused for before/after comparisons.
    """

    def __init__(self, command: str, runner: CommandRunnerLike, cwd: Path | str) -> None:
        self._command = command
        self._runner = runner
        self._cwd = Path(cwd)

    @property
    def command(self) -> str:
        return self._command

    def run(self) -> VerifyRun:
        exit_code, stdout, stderr = self._runner.run(self._command, self._cwd)
        return VerifyRun(
            command=self._command,
            exit_code=exit_code,
            stdout=stdout or "",
            stderr=stderr or "",
        )


class DjangoTestVerifier:
    """Adapts a Django-style test runner to the Verifier protocol so the general
    RepairLoop can drive the Django fix path with rollback + one-error-at-a-time,
    the same way it drives `npm run build`. Exit code is synthesized from the
    parsed failure count (0 = all passing)."""

    def __init__(self, command: str, test_runner: TestRunnerLike, project_cwd: Path | str) -> None:
        self._command = command
        self._runner = test_runner
        self._cwd = project_cwd

    @property
    def command(self) -> str:
        return self._command

    def run(self) -> VerifyRun:
        result = self._runner.run(self._cwd)
        exit_code = 0 if getattr(result, "failed", 1) == 0 else 1
        return VerifyRun(
            command=self._command,
            exit_code=exit_code,
            stdout=getattr(result, "raw_output", "") or "",
        )
