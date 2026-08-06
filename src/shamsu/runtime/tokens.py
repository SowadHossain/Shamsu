"""The concrete cancellation token.

This is the implementation behind `interfaces.CancellationToken`, and the
reason it is worth its own module is that v1 had all of this machinery and it
was never reachable. Getting it right means getting three things right at once:

1. **Polling works from any thread.** A tool running in a worker thread must be
   able to check `cancelled` without touching an event loop. Hence a
   `threading.Event` for the boolean state.
2. **Awaiting works on the event loop.** A model call must be able to *race*
   cancellation instead of only checking between calls -- otherwise a run stays
   uninterruptible for as long as one generation takes, which is precisely
   v1's long-running behaviour.
3. **Requesting cancellation works from either side.** `request()` is safe to
   call from a signal handler, another thread, or the loop itself.

Feedback interruption uses the same machinery with different semantics: the run
continues, only the in-flight call is abandoned.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading

from shamsu.interfaces.cancellation import Cancelled, FeedbackInterrupt


class RunToken:
    """A cancellation token bound to one run."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._reason: str | None = None
        self._feedback: list[str] = []
        self._lock = threading.Lock()

        # Created lazily: a token may be constructed outside a running loop
        # (e.g. by a synchronous CLI path) and only later awaited.
        self._async_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- observation -------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def raise_if_cancelled(self) -> None:
        """Raise `Cancelled` if cancellation was requested.

        Call before any unit of work that cannot itself be interrupted: a
        subprocess spawn, a file write, a model request.
        """
        if self._cancelled.is_set():
            raise Cancelled(self._reason or "run cancelled")

    async def wait_cancelled(self) -> str:
        """Block until cancellation is requested, then return the reason.

        Race this against a long call so cancellation lands mid-flight:

            done, pending = await asyncio.wait(
                [asyncio.create_task(model.generate(...)),
                 asyncio.create_task(token.wait_cancelled())],
                return_when=asyncio.FIRST_COMPLETED,
            )
        """
        await self._ensure_async_event().wait()
        return self._reason or "run cancelled"

    # -- control -----------------------------------------------------------

    def request(self, reason: str = "run cancelled") -> None:
        """Request cancellation. Safe from any thread, idempotent.

        The first reason wins: a later cancel must not overwrite the
        explanation the user will actually see.
        """
        with self._lock:
            if self._cancelled.is_set():
                return
            self._reason = reason
            self._cancelled.set()
        self._wake_waiters()

    # -- feedback ----------------------------------------------------------

    def submit_feedback(self, feedback: str) -> None:
        """Queue feedback and interrupt the current call.

        Distinct from cancellation: the run continues with the new information.
        """
        with self._lock:
            self._feedback.append(feedback)
        self._wake_waiters()

    @property
    def has_feedback(self) -> bool:
        with self._lock:
            return bool(self._feedback)

    def take_feedback(self) -> list[str]:
        """Drain queued feedback."""
        with self._lock:
            pending, self._feedback = self._feedback, []
        return pending

    def raise_if_interrupted(self) -> None:
        """Raise for cancellation or feedback, whichever is pending.

        Cancellation is checked first: if the user both cancelled and left
        feedback, they wanted the run to stop.
        """
        self.raise_if_cancelled()
        pending = self.take_feedback()
        if pending:
            raise FeedbackInterrupt("\n".join(pending))

    # -- internals ---------------------------------------------------------

    def _ensure_async_event(self) -> asyncio.Event:
        if self._async_event is None:
            self._async_event = asyncio.Event()
            self._loop = asyncio.get_running_loop()
            # A cancel that arrived before anyone awaited must not be lost.
            if self._cancelled.is_set():
                self._async_event.set()
        return self._async_event

    def _wake_waiters(self) -> None:
        """Wake the async waiter, whichever thread we were called from."""
        event, loop = self._async_event, self._loop
        if event is None or loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None

        if running is loop:
            event.set()
        else:
            # Another thread, or no loop at all: hop onto the owning loop.
            # A closed loop means the run is already gone; nothing to wake.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(event.set)


__all__ = ["RunToken"]
