"""Running a subprocess so that cancellation actually reaches it.

Extracted from `tools/testing.py` when `check.run` needed the same thing.
Sharing it is not tidiness: the hard part is the *kill path*, and a second
hand-written copy is where the second copy forgets to escalate from
`terminate()` to `kill()`. An abandoned pytest or build process keeps writing
to the workspace long after the run was "stopped", which is precisely the
behaviour v2 exists to prevent.

Two details worth keeping in view:

* **Bytecode caching is redirected per call.** CPython validates a cached
  `.pyc` against *(mtime in whole seconds, size)* — and an agent patching a
  file often changes neither. `return a - b` becoming `return a + b` is the
  same size, and a repair lands within a second of the run that motivated it.
  Python then executes the previous bytecode, the check fails against code that
  is already correct, and the agent repairs a bug that no longer exists. A
  fresh cache directory per call makes every call compile from source, and
  keeps `__pycache__` out of the workspace so a checkpoint diff shows only real
  changes.

* **Cancellation is raced, not polled.** `wait_cancelled()` runs alongside
  `communicate()`, so a token that fires mid-build is acted on immediately
  rather than at the next convenient boundary.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from shamsu.interfaces.cancellation import CancellationToken, Cancelled

#: How long a terminated process is given to exit before it is killed.
TERMINATE_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class Completed:
    """What a finished subprocess produced."""

    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def combined(self) -> str:
        """stdout and stderr together, in that order, with blank parts dropped.

        Tools differ about which stream they report on — ruff writes findings to
        stdout, mypy's summary lands on stdout, and a crashing build reports on
        stderr — so a caller that reads only one of them will eventually read
        the empty one and report a silent failure.
        """
        return "\n".join(part for part in (self.stdout.strip(), self.stderr.strip()) if part)


async def run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    cancel: CancellationToken,
    fresh_bytecode_cache: bool = True,
) -> Completed:
    """Run `argv` to completion, killing it if cancellation arrives.

    Raises:
        Cancelled: the token fired. The process was terminated, then killed if
            it did not exit within `TERMINATE_GRACE_SECONDS`.
        FileNotFoundError: `argv[0]` is not on PATH. Left to the caller, which
            knows how to name the missing tool usefully.
        OSError: the process could not be started.
    """
    if not fresh_bytecode_cache:
        return await _spawn(argv, cwd=cwd, cancel=cancel, environment=dict(os.environ))

    with tempfile.TemporaryDirectory(prefix="shamsu-pycache-") as cache:
        environment = dict(os.environ)
        environment["PYTHONPYCACHEPREFIX"] = cache
        return await _spawn(argv, cwd=cwd, cancel=cancel, environment=environment)


async def _spawn(
    argv: Sequence[str],
    *,
    cwd: Path,
    cancel: CancellationToken,
    environment: dict[str, str],
) -> Completed:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )

    communicate = asyncio.ensure_future(process.communicate())
    watcher = asyncio.ensure_future(cancel.wait_cancelled())

    done, _ = await asyncio.wait({communicate, watcher}, return_when=asyncio.FIRST_COMPLETED)

    if communicate in done:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        out, err = communicate.result()
        return Completed(
            exit_code=process.returncode or 0,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
        )

    # Cancelled: terminate, then escalate if it does not go quietly.
    with suppress_process_errors():
        process.terminate()
    try:
        await asyncio.wait_for(communicate, timeout=TERMINATE_GRACE_SECONDS)
    except (TimeoutError, asyncio.CancelledError):
        with suppress_process_errors():
            process.kill()
    finally:
        communicate.cancel()
        await asyncio.gather(communicate, return_exceptions=True)

    raise Cancelled(cancel.reason or "run cancelled")


class suppress_process_errors:
    """Terminating an already-dead process is not an error worth propagating."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, (ProcessLookupError, OSError))


__all__ = [
    "TERMINATE_GRACE_SECONDS",
    "Completed",
    "run_process",
    "suppress_process_errors",
]
