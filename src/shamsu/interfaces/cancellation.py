"""Cancellation primitives.

This module exists because of the single worst defect in v1: `run_control.py`
implemented `register_run`, `cancel_run`, `add_feedback` and in-flight model
cancellation, and *the live loop never observed any of it*. `cancel_run` could
be called and nothing happened. See legacy-code/LEGACY_README.md.

The fix is structural, not a bigger implementation: cancellation is a parameter
threaded through every long-running call, so a component that can block is
statically obliged to accept a token. A component that does not take a
`CancellationToken` is one that is not allowed to block.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable


class Cancelled(Exception):
    """Raised when a cancelled token is observed at a checkpoint.

    Distinct from `asyncio.CancelledError` on purpose. v1 had to disambiguate
    "the user cancelled the run" from "a feedback interrupt cancelled the
    current model task" by inspecting whether an event happened to be set,
    which is fragile. Here they are different types.
    """

    def __init__(self, reason: str = "run cancelled") -> None:
        super().__init__(reason)
        self.reason = reason


class FeedbackInterrupt(Exception):
    """Raised when a model call is interrupted to inject user feedback.

    The run continues. Only the in-flight call is abandoned.
    """

    def __init__(self, feedback: str) -> None:
        super().__init__("interrupted for user feedback")
        self.feedback = feedback


@runtime_checkable
class CancellationToken(Protocol):
    """Observed at every checkpoint in a long-running operation.

    Implementations must be safe to poll from any thread and cheap enough to
    check in a tight loop.
    """

    @property
    def cancelled(self) -> bool:
        """Whether cancellation has been requested."""
        ...

    @property
    def reason(self) -> str | None:
        """Why cancellation was requested, if it was."""
        ...

    def raise_if_cancelled(self) -> None:
        """Raise `Cancelled` if cancellation has been requested.

        Call this before starting any unit of work that cannot itself be
        interrupted -- a subprocess spawn, a file write, a model request.
        """
        ...

    async def wait_cancelled(self) -> str:
        """Block until cancellation is requested, then return the reason.

        Lets a caller race a model or tool call against cancellation rather
        than only polling between calls, which is what made v1's long-running
        mode effectively uninterruptible for minutes at a time.
        """
        ...


class NullCancellationToken:
    """A token that is never cancelled.

    For synchronous, short-lived call sites and for tests. Deliberately a real
    implementation rather than an `Optional[CancellationToken]` default, so no
    call site has to branch on `None`.
    """

    __slots__ = ()

    @property
    def cancelled(self) -> bool:
        return False

    @property
    def reason(self) -> str | None:
        return None

    def raise_if_cancelled(self) -> None:
        return None

    async def wait_cancelled(self) -> str:
        """Never resolves, because this token is never cancelled.

        An earlier version raised instead, on the reasoning that a coroutine
        which hangs forever is worse than a clear error. That was wrong: the
        whole point of this method is to be *raced* against real work, and a
        raising implementation completes instantly, which a race reads as
        "cancelled". Never resolving is the semantically correct answer, and
        the caller's timeout is what bounds the wait.
        """
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover


__all__ = [
    "Cancelled",
    "CancellationToken",
    "FeedbackInterrupt",
    "NullCancellationToken",
]
