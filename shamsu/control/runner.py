"""Run one queued prompt, wherever the prompt came from.

The rule this enforces, in one place so all three surfaces get it identically:

1. **One turn per thread.** Two turns on one session interleave
   `messages.jsonl` and orphan `tool_call_id`s - a corrupted conversation, not
   just confusing output.
2. **One turn per machine.** There is one Ollama and one GPU. Serialising per
   thread would still let two projects contend for it, which is an OOM or a
   thrash, not a queue.

Both are leases, which is why they are the same mechanism at two scopes. A
caller asks to run; it either gets the go-ahead or its prompt sits in the queue
until whoever is running finishes and drains it.

The turn itself is the ordinary `SimpleChatLoop`, emitting to the ordinary
`TurnStream`. Nothing here changes what a turn is - only who is allowed to
start one, and when.
"""
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shamsu.control.approvals import SharedApprovalBroker
from shamsu.control.store import (
    CANCELLED,
    DONE,
    LEASE_HEARTBEAT_SECONDS,
    MACHINE_LEASE_KEY,
    ControlStore,
    QueuedPrompt,
)

#: The whole machine's run slot, expressed as a lease so the install-wide gate
#: and the per-thread gate are the same code path.
MACHINE_SESSION = MACHINE_LEASE_KEY
MACHINE_WORKSPACE = MACHINE_LEASE_KEY

#: What `_start_worker` decided.
STARTED = "started"
ALREADY_DRAINING = "already-draining"
BUSY = "busy"


@dataclass(frozen=True)
class RunOutcome:
    accepted: bool
    queued: bool
    queue_id: int = 0
    reason: str = ""
    final: str = ""


class QueuedRunner:
    """Accepts prompts from any surface and runs them one at a time."""

    def __init__(
        self,
        store: ControlStore | None = None,
        *,
        surface: str = "web",
        broker: SharedApprovalBroker | None = None,
    ) -> None:
        self.store = store or ControlStore()
        self.surface = surface
        self.broker = broker or SharedApprovalBroker(self.store)
        self._workers: dict[tuple[str, str], threading.Thread] = {}
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    # -- accepting -------------------------------------------------------

    def submit(
        self,
        workspace: Path | str,
        session_id: str,
        text: str,
        *,
        on_event: Callable[[str], None] | None = None,
    ) -> RunOutcome:
        """Take a prompt. Run it now if nothing is busy, else queue it.

        Always enqueues first. Deciding to run and *then* recording it would
        leave a turn nobody could see in the queue, and a crash between the two
        would lose the prompt entirely - the same durable-first rule the turn
        stream follows.
        """
        text = (text or "").strip()
        if not text:
            return RunOutcome(accepted=False, queued=False, reason="empty prompt")

        queue_id = self.store.enqueue(workspace, session_id, text, self.surface)
        # Only "I started the turn" counts as running now. A worker that was
        # already draining this thread means this prompt waits its turn behind
        # the one in flight - reporting that as running would have the surface
        # claim work had begun while it sat in the queue.
        if self._start_worker(workspace, session_id, on_event=on_event) == STARTED:
            return RunOutcome(accepted=True, queued=False, queue_id=queue_id)
        depth = self.store.queue_depth(workspace, session_id)
        return RunOutcome(
            accepted=True,
            queued=True,
            queue_id=queue_id,
            reason=self._busy_reason(workspace, session_id, depth),
        )

    def _busy_reason(self, workspace: Path | str, session_id: str, depth: int) -> str:
        holder = self.store.lease_holder(workspace, session_id)
        if holder is not None:
            return f"queued - this thread is running on {holder.owner_surface} ({depth} waiting)"
        machine = self.store.lease_holder(MACHINE_WORKSPACE, MACHINE_SESSION)
        if machine is not None:
            return f"queued - another run is using the model ({depth} waiting)"
        return f"queued ({depth} waiting)"

    # -- running ---------------------------------------------------------

    def _start_worker(
        self,
        workspace: Path | str,
        session_id: str,
        *,
        on_event: Callable[[str], None] | None = None,
    ) -> str:
        key = (str(Path(workspace).resolve()), session_id)
        with self._lock:
            existing = self._workers.get(key)
            if existing is not None and existing.is_alive():
                # Already draining this thread: the prompt will be picked up by
                # the loop that is running, but it is queued, not started.
                return ALREADY_DRAINING
            if not self.store.acquire_lease(workspace, session_id, self.surface):
                return BUSY
            if not self.store.acquire_lease(
                MACHINE_WORKSPACE, MACHINE_SESSION, self.surface
            ):
                self.store.release_lease(workspace, session_id)
                return BUSY
            worker = threading.Thread(
                target=self._drain,
                args=(Path(workspace).resolve(), session_id, on_event),
                name=f"shamsu-run-{session_id[:12]}",
                daemon=True,
            )
            self._workers[key] = worker
            worker.start()
            return STARTED

    def _drain(
        self,
        workspace: Path,
        session_id: str,
        on_event: Callable[[str], None] | None,
    ) -> None:
        """Run every queued prompt for this thread, then let the lease go."""
        beat = _Heartbeat(self.store, workspace, session_id)
        beat.start()
        try:
            while not self._stopping.is_set():
                item = self.store.claim_next(workspace, session_id)
                if item is None:
                    break
                try:
                    self._run_one(workspace, session_id, item)
                    self.store.finish(item.queue_id, DONE)
                except Exception as exc:  # noqa: BLE001 - one bad turn is not the queue
                    self.store.finish(item.queue_id, CANCELLED)
                    self._announce_failure(workspace, session_id, exc)
                    if on_event is not None:
                        with_suppressed(on_event, f"run failed: {item.text[:80]}")
        finally:
            beat.stop()
            self.store.release_lease(workspace, session_id)
            self.store.release_lease(MACHINE_WORKSPACE, MACHINE_SESSION)
            with self._lock:
                self._workers.pop((str(workspace), session_id), None)

    def _announce_failure(
        self, workspace: Path, session_id: str, exc: BaseException
    ) -> None:
        """Put a failed turn ON THE STREAM, so every surface stops waiting.

        The queue row was marked cancelled and nothing else was said. Surfaces
        render from the turn stream, so the browser saw `turn.start` and then
        silence for ever - a bubble that never resolves, which is what "the
        chat is out of sync" looks like from the outside. A turn that dies has
        to end as visibly as one that succeeds.
        """
        from shamsu.runtime.turn_stream import TurnEvent, TurnStream

        try:
            stream = TurnStream(workspace, session_id)
            detail = f"{type(exc).__name__}: {exc}"[:400]
            common = {
                "session_id": session_id,
                "workspace": str(workspace),
                "source": self.surface,
            }
            stream.publish(TurnEvent(seq=1, kind="error", text=detail, **common))
            stream.publish(
                TurnEvent(
                    seq=2,
                    kind="turn.end",
                    text="failed",
                    data={"status": "failed"},
                    **common,
                )
            )
        except Exception:  # noqa: BLE001 - saying so must not be a second failure
            return

    def _run_one(self, workspace: Path, session_id: str, item: QueuedPrompt) -> str:
        from shamsu.agents.simple_chat import SimpleChatLoop, build_simple_tools
        from shamsu.llm.ollama_client import default_ollama_client
        from shamsu.runtime.timeouts import TimeoutConfig
        from shamsu.runtime.turn_stream import TurnStream
        from shamsu.session.manager import SessionManager

        manager = SessionManager(workspace)
        logger = manager.resume_session(session_id)
        stream = TurnStream(workspace, session_id)
        approve = self.broker.approval_func(
            workspace=workspace,
            session_id=session_id,
            should_stop=self._stopping.is_set,
        )
        tools = build_simple_tools(
            workspace,
            main_loop=None,
            console_approval=approve,
            session_logger=logger,
        )
        loop = SimpleChatLoop(
            workspace,
            client=default_ollama_client(timeout_config=TimeoutConfig.from_env()),
            tools=tools,
            session_logger=logger,
            emit=stream.publish,
            source=self.surface,
        )
        result = asyncio.run(loop.run(item.text))
        return result.final

    # -- lifecycle -------------------------------------------------------

    def stop(self, timeout: float = 10.0) -> None:
        self._stopping.set()
        with self._lock:
            workers = list(self._workers.values())
        for worker in workers:
            worker.join(timeout=timeout)

    def busy(self) -> bool:
        with self._lock:
            return any(worker.is_alive() for worker in self._workers.values())


class _Heartbeat(threading.Thread):
    """Keeps a lease alive while a turn runs.

    Without it a turn longer than the stale window would look abandoned and a
    second surface would take the thread out from under it - which is the exact
    failure the lease exists to prevent, arrived at from the other direction.
    """

    def __init__(self, store: ControlStore, workspace: Path, session_id: str) -> None:
        super().__init__(name="shamsu-lease-beat", daemon=True)
        self.store = store
        self.workspace = workspace
        self.session_id = session_id
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(LEASE_HEARTBEAT_SECONDS):
            self.store.renew_lease(self.workspace, self.session_id)
            self.store.renew_lease(MACHINE_WORKSPACE, MACHINE_SESSION)

    def stop(self) -> None:
        self._stop.set()


def with_suppressed(callback: Callable[[Any], None], value: Any) -> None:
    try:
        callback(value)
    except Exception:  # noqa: BLE001
        pass
