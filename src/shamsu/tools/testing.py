"""The `test.run` tool.

Structured commands, not shell strings (plan §24.3). The model chooses a
*command key* from a small allowlist derived from the project manifest — it
never supplies a command line. That removes an entire class of injection and
mistake: there is no string for a model to smuggle `; rm -rf /` into, because
there is no string.

The trade is flexibility, and it is the right trade for now. A project whose
tests cannot be run by any allowlisted command is a project this tool reports
honestly that it cannot test.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.enums import EvidenceKind, Phase, Risk
from shamsu.interfaces.tools import ToolContract, ToolResult
from shamsu.security.paths import PathEscape, PathSandbox
from shamsu.tools.base import Tool
from shamsu.verification.digest import TestDigest, digest_test_output

#: Command key -> fixed argv. Nothing here interpolates model input; a target
#: path, when given, is sandbox-resolved and appended as a separate argument.
DEFAULT_COMMANDS: Mapping[str, tuple[str, ...]] = {
    "pytest": ("python3", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"),
    "pytest-verbose": ("python3", "-m", "pytest", "-v", "--tb=long", "-p", "no:cacheprovider"),
    "unittest": ("python3", "-m", "unittest", "discover"),
    "npm-test": ("npm", "test", "--silent"),
    "cargo-test": ("cargo", "test", "--quiet"),
    "go-test": ("go", "test", "./..."),
}


class TestRunInput(BaseModel):
    command: str = Field(
        default="pytest",
        description="Which allowlisted test command to run. Not a shell command line.",
    )
    target: str = Field(
        default="",
        description="Optional file or directory to narrow the run to. Workspace-relative.",
    )


class TestRunTool(Tool[TestRunInput]):
    """Run the project's tests and report a digested result."""

    input_model = TestRunInput

    contract = ToolContract(
        name="test.run",
        purpose=(
            "Run the project's tests. Choose a command key (e.g. 'pytest') and "
            "optionally narrow to a file or directory. Returns a compact digest."
        ),
        # VERIFY and REPAIR only. Running tests during AUTHOR would let a step
        # claim verification before its edit is finished.
        allowed_phases=frozenset({Phase.VERIFY, Phase.REPAIR}),
        risk=Risk.MEDIUM,
        reversible=True,
        timeout_seconds=600.0,
        max_output_bytes=12_000,
        produces_evidence=frozenset({EvidenceKind.TESTS_PASSED}),
    )

    def __init__(
        self,
        workspace: Path,
        commands: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._workspace = Path(workspace).resolve()
        self._sandbox = PathSandbox(workspace)
        self._commands: dict[str, tuple[str, ...]] = {
            key: tuple(value) for key, value in (commands or DEFAULT_COMMANDS).items()
        }
        self.last_digest: TestDigest | None = None

    @property
    def commands(self) -> Sequence[str]:
        return tuple(sorted(self._commands))

    async def run(self, arguments: TestRunInput, cancel: CancellationToken) -> ToolResult:
        started = time.monotonic()
        cancel.raise_if_cancelled()

        argv = self._commands.get(arguments.command)
        if argv is None:
            return self.failed(
                f"unknown test command {arguments.command!r}; available: "
                f"{', '.join(self.commands)}",
                started=started,
            )

        if arguments.target:
            try:
                resolved = self._sandbox.resolve(arguments.target)
            except PathEscape as exc:
                return self.failed(str(exc), started=started)
            if not resolved.exists():
                return self.failed(
                    f"{arguments.target}: no such file or directory", started=started
                )
            argv = (*argv, self._sandbox.relative(resolved))

        try:
            code, stdout, stderr = await self._spawn(argv, cancel)
        except FileNotFoundError:
            return self.failed(f"{argv[0]} is not installed or not on PATH", started=started)
        except OSError as exc:
            return self.failed(f"could not run tests: {exc}", started=started)

        digest = digest_test_output(stdout, stderr, exit_code=code)
        self.last_digest = digest

        rendered = digest.render()
        if digest.truncated_from:
            rendered += (
                f"\n\n[output was {digest.truncated_from} characters; the middle was omitted]"
            )

        if digest.passed:
            return self.ok(rendered, started=started)

        # A failing test run is a successful *tool* call that produced a
        # negative result — but it must not register TESTS_PASSED. `failed`
        # attaches no evidence, which is exactly the required behaviour.
        return self.failed(rendered, started=started)

    async def _spawn(
        self, argv: tuple[str, ...], cancel: CancellationToken
    ) -> tuple[int, str, str]:
        """Run the command, killing it if cancellation arrives.

        The kill matters: an abandoned pytest process keeps writing to the
        workspace long after the run was 'stopped', which is precisely the
        behaviour v2 exists to prevent.
        """
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        communicate = asyncio.ensure_future(process.communicate())
        watcher = asyncio.ensure_future(cancel.wait_cancelled())

        done, _ = await asyncio.wait({communicate, watcher}, return_when=asyncio.FIRST_COMPLETED)

        if communicate in done:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            out, err = communicate.result()
            return (
                process.returncode or 0,
                out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"),
            )

        # Cancelled: terminate, then wait briefly before escalating.
        with _suppress_process_errors():
            process.terminate()
        try:
            await asyncio.wait_for(communicate, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            with _suppress_process_errors():
                process.kill()
        finally:
            communicate.cancel()
            await asyncio.gather(communicate, return_exceptions=True)

        from shamsu.interfaces.cancellation import Cancelled

        raise Cancelled(cancel.reason or "run cancelled")


class _suppress_process_errors:
    """Terminating an already-dead process is not an error worth propagating."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, (ProcessLookupError, OSError))


__all__ = ["DEFAULT_COMMANDS", "TestRunInput", "TestRunTool"]
