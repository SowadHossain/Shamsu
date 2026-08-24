"""The terminal's half of approve-from-anywhere.

Two directions, and they are different problems:

**Outbound** - an approval the CLI's own run raised. It goes into the shared
store so the browser and the phone can see it, *and* it is asked here. Whoever
answers first wins, so the local prompt has to give up the moment somebody
answers elsewhere. A prompt that kept waiting after the question was settled
would strand the turn behind a keystroke nobody owes anymore.

**Inbound** - an approval raised by a run in another process, on a thread this
REPL happens to have open. Nothing is blocking here, so there is nothing to
cancel: it is printed as it arrives, and `/approve` or `/deny` answers it.

The hard part is the outbound one, and it is hard for a specific reason:
`input()` cannot be interrupted from another thread. So the local read runs
through `prompt_toolkit`'s async prompt, which *is* cancellable, raced against
a watcher on the store. Where prompt_toolkit cannot be used - a piped stdin, a
platform quirk - the fallback is the ordinary blocking read, and remote
answering simply does not cancel it. That is stated rather than hidden: it
degrades to today's behaviour instead of pretending.
"""
from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Awaitable
from typing import Any, Callable

from rich.console import Console
from rich.panel import Panel

from shamsu.control.store import ALLOW, DENY, ControlStore

POLL_SECONDS = 0.25


def run_coroutine_blocking(factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run an async call from synchronous code, from ANY thread.

    Takes a FACTORY rather than a coroutine, and that is the whole point.
    ``asyncio.run(ask_here_or_anywhere(...))`` builds the coroutine first, as
    an argument, and only then discovers a loop is already running in this
    thread - so it raises without ever awaiting what it just created. Live
    2026-08-22, on a user's screen mid-approval::

        RuntimeWarning: coroutine 'ask_here_or_anywhere' was never awaited

    and the `except Exception` around it turned the shared "answer from
    anywhere" prompt into the plain local one every single time. Nothing is
    created here until there is somewhere to run it.

    When a loop already owns this thread the coroutine gets a thread of its
    own. Blocking the caller is correct: it is an approval, the turn is waiting
    on a human either way, and the alternative - reentering the loop - is what
    was broken.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())

    done: list[Any] = []
    failed: list[BaseException] = []

    def run() -> None:
        try:
            done.append(asyncio.run(factory()))
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            failed.append(exc)

    thread = threading.Thread(target=run, name="shamsu-approval-ask", daemon=True)
    thread.start()
    thread.join()
    if failed:
        raise failed[0]
    return done[0]


#: Approvals THIS process raised. `ApprovalWatcher` announces what other
#: processes are asking; without this it also announced our own, so one
#: `run_command` drew the question twice - once by the code asking it and once
#: by the watcher noticing it in the store - and then printed a resolution
#: notice for a question the user had already answered in front of them.
_RAISED_HERE: set[str] = set()


def mark_raised_here(approval_id: str) -> None:
    """Note that this process raised *approval_id*, so nothing announces it."""
    if approval_id:
        _RAISED_HERE.add(str(approval_id))


def raised_here(approval_id: str) -> bool:
    return str(approval_id) in _RAISED_HERE


def forget_raised_here(approval_id: str) -> None:
    _RAISED_HERE.discard(str(approval_id))

_ALLOW_WORDS = {"y", "yes", "a", "allow", "1"}
_DENY_WORDS = {"n", "no", "d", "deny", "2", ""}


def render_request(record: Any, console: Console, *, origin: str = "") -> None:
    """Show the question. Used for both directions, so they look the same."""
    lines = [str(getattr(record, "description", "") or "an action")]
    risk = str(getattr(record, "risk_level", "") or "")
    if risk:
        lines.append(f"\nRisk: {risk}")
    preview = str(getattr(record, "preview", "") or "")
    if preview:
        lines.append(f"\n{preview[:600]}")
    if origin:
        lines.append(f"\nAsked by: {origin}")
    console.print(
        Panel(
            "\n".join(lines),
            title="Approval Required",
            border_style="yellow",
        )
    )


async def ask_here_or_anywhere(
    store: ControlStore,
    approval_id: str,
    console: Console,
    *,
    poll_seconds: float = POLL_SECONDS,
    read_line: Callable[[], Any] | None = None,
) -> str:
    """Ask in this terminal, but settle for an answer from any surface.

    Returns the decision that actually won, which may not be the one typed
    here - if the phone answered first, that is the answer, and saying so is
    more honest than silently overriding it.
    """
    console.print("[dim]Allow? [y/N] - or answer from the web or Telegram[/dim]")

    typed: asyncio.Task[str] = asyncio.ensure_future(_read_choice(read_line))
    watched: asyncio.Task[str] = asyncio.ensure_future(
        _watch_store(store, approval_id, poll_seconds)
    )
    try:
        done, pending = await asyncio.wait(
            {typed, watched}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        if watched in done and not watched.cancelled():
            decision = watched.result()
            console.print(f"[dim]Answered elsewhere: {decision}[/dim]")
            return decision
        answer = typed.result() if typed in done else DENY
        # The local answer still has to go through the store, because it might
        # lose. `resolve_approval` returning False means somebody beat us by a
        # hair, and their answer is the real one.
        if not store.resolve_approval(approval_id, answer, "cli"):
            record = store.approval(approval_id)
            if record is not None and record.decision:
                where = record.decided_by or "a surface that did not name itself"
                if record.decided_by == "timeout":
                    where = "timeout - it expired while the question was open"
                console.print(f"[dim]Already answered on {where}[/dim]")
                return record.decision
        return answer
    finally:
        for task in (typed, watched):
            if not task.done():
                task.cancel()


async def _read_choice(read_line: Callable[[], Any] | None) -> str:
    if read_line is not None:
        raw = read_line()
        if asyncio.iscoroutine(raw):
            raw = await raw
        return _decision_from(str(raw))
    session = _prompt_session()
    if session is not None:
        raw = await session.prompt_async("> ")
        return _decision_from(str(raw))
    # No cancellable reader available. Blocking here is the old behaviour, and
    # the watcher beside it cannot win until this returns - so a remote answer
    # is honoured, just not until a key is pressed.
    raw = await asyncio.to_thread(_blocking_read)
    return _decision_from(raw)


def _prompt_session() -> Any:
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input.defaults import create_input

        create_input()  # raises where prompt_toolkit cannot own the console
        return PromptSession()
    except Exception:  # noqa: BLE001 - piped stdin, no tty, platform quirk
        return None


def _blocking_read() -> str:
    try:
        return input("> ")
    except (EOFError, OSError):
        return ""


def _decision_from(raw: str) -> str:
    """Anything that is not clearly yes is a no."""
    return ALLOW if raw.strip().lower() in _ALLOW_WORDS else DENY


async def _watch_store(store: ControlStore, approval_id: str, poll_seconds: float) -> str:
    while True:
        record = store.approval(approval_id)
        if record is None:
            return DENY
        if record.decision:
            return record.decision
        await asyncio.sleep(poll_seconds)


class ApprovalWatcher:
    """Announce approvals raised by OTHER processes on threads you can see.

    A run driven from the browser can stop and ask a question. If a REPL is
    open, that question should appear there too - otherwise "answer from
    anywhere" means "anywhere except the place you were already looking".

    This only announces. Answering is `/approve` and `/deny`, because the REPL
    is sitting at its own prompt and stealing the line to ask something else
    would be worse than a notification.
    """

    def __init__(
        self,
        store: ControlStore,
        console: Console,
        *,
        poll_seconds: float = 2.0,
    ) -> None:
        self.store = store
        self.console = console
        self.poll_seconds = poll_seconds
        self._seen: dict[str, Any] = {}
        #: Approvals that were ALREADY waiting when this watcher opened. They
        #: belong to some earlier run, very often a process that is no longer
        #: alive, and drawing a full panel for each is noise carrying an
        #: `/approve` the user has no reason to trust. Counted once instead.
        self._inherited: set[str] = set()
        self._primed = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="shamsu-approval-watch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3)

    def pending(self) -> list[Any]:
        return list(self._seen.values())

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self._sweep()
            except Exception:  # noqa: BLE001 - a watcher must not kill the REPL
                pass

    def _sweep(self) -> None:
        # Overdue rows first, so an approval nobody answered is STAMPED as a
        # timeout instead of quietly falling off the pending list underneath
        # us. Without this the disappearance below cannot be told apart from
        # an answer, and the watcher says a human decided when none did.
        with contextlib.suppress(Exception):
            self.store.expire_approvals()

        live = {item.approval_id: item for item in self.store.pending_approvals()}

        if not self._primed:
            # Everything already waiting when this watcher opened was asked by
            # an earlier run - often one that is no longer alive. It is still
            # shown, because a browser question raised a second before you
            # opened this terminal is real, but it is LABELLED: three fresh
            # cards for a session that closed twenty minutes ago read as
            # something waiting on you right now, and each carries an
            # `/approve` for a process that will never see the answer.
            self._primed = True
            self._inherited = set(live)

        for approval_id in list(self._seen):
            if approval_id not in live:
                self._seen.pop(approval_id, None)
                self.console.print(f"[dim]{self._closing_note(approval_id)}[/dim]")
        self._inherited &= set(live)

        for approval_id, record in live.items():
            if approval_id in self._seen:
                continue
            if raised_here(approval_id):
                # Asked by this process, in this terminal, and already on
                # screen. Announcing it again draws the same question a second
                # time under a "/approve <id>" the user does not need, beside
                # the prompt that is waiting for their keypress.
                continue
            self._seen[approval_id] = record
            render_request(record, self.console, origin=_origin(record))
            if approval_id in self._inherited:
                self.console.print(
                    "[dim]Raised before this session opened - the run that asked "
                    "may already be gone. It expires on its own.[/dim]"
                )
            self.console.print(
                f"[dim]/approve {approval_id[:20]}  or  /deny {approval_id[:20]}[/dim]"
            )

    def _closing_note(self, approval_id: str) -> str:
        """Why an approval left the pending list. The three cases differ.

        Saying "resolved on another surface" for all three was a claim the
        user could catch being false: an approval nobody ever answered timed
        out, its row kept `decided_by = ''`, and the terminal announced a
        decision made by a person who did not exist. A watcher that cannot
        tell "answered" from "expired" should say which one it does not know,
        not pick the reassuring one.
        """
        record = self.store.approval(approval_id)
        short = approval_id[:20]
        if record is None:
            return f"Approval {short} is no longer on record."
        decision = str(getattr(record, "decision", "") or "")
        decided_by = str(getattr(record, "decided_by", "") or "")
        verdict = {ALLOW: "allowed", DENY: "denied"}.get(decision, decision)
        if not decision:
            return f"Approval {short} is gone from the queue, unanswered."
        if decided_by == "timeout":
            return f"Approval {short} expired unanswered - denied."
        if not decided_by:
            return f"Approval {short} was {verdict}; the surface is not recorded."
        return f"Approval {short} {verdict} on {decided_by}."

    def resolve(self, approved: bool, approval_id: str = "") -> tuple[bool, str]:
        """Answer the named approval, or the only one waiting."""
        waiting = self.store.pending_approvals()
        if not waiting:
            return False, "Nothing is waiting for approval."
        if not approval_id:
            if len(waiting) > 1:
                names = ", ".join(item.approval_id[:20] for item in waiting)
                return False, f"Several are waiting - name one: {names}"
            target = waiting[0].approval_id
        else:
            matches = [
                item.approval_id
                for item in waiting
                if item.approval_id.startswith(approval_id)
            ]
            if not matches:
                return False, f"No pending approval matches {approval_id!r}."
            target = matches[0]
        won = self.store.resolve_approval(target, ALLOW if approved else DENY, "cli")
        self._seen.pop(target, None)
        if not won:
            return False, self._closing_note(target)
        return True, f"{'Allowed' if approved else 'Denied'}."


def _origin(record: Any) -> str:
    workspace = str(getattr(record, "workspace", "") or "")
    return workspace.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] if workspace else ""
