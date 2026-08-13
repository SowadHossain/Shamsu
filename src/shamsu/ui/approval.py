"""Asking a human whether a high-risk step may run.

`AgentState.WAIT_APPROVAL` used to return STOPPED with *"a step requires
approval and no approver is configured"*. Honest, but it meant a `HIGH`-risk
step **could not proceed at all** — the runtime had `ApprovalRecord`,
`store.request_approval`, `store.decide_approval`, and a `ToolGateway(approval=)`
hook, and nothing to put a question in front of a person.

Three rules shape what is here, and each one is a way of not accidentally
granting permission:

**Silence is never consent.** `TIMED_OUT` is a distinct decision from
`APPROVED` and blocks exactly like a denial. A prompt that no-one answers, a
closed stdin, an EOF from a pipe — all of them time out.

**Only an explicit yes is a yes.** The reader accepts `y`/`yes` and nothing
else. Every other input, including an empty line, is a denial, so leaning on
the return key is safe.

**A non-interactive session cannot approve.** `DenyingApprover` is the default
wherever there is no terminal — a piped or CI run must not be able to authorise
a destructive step because nobody was watching.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from shamsu.interfaces.cancellation import CancellationToken, Cancelled
from shamsu.interfaces.enums import ApprovalDecision
from shamsu.runtime.session import Approver
from shamsu.state.records import PlanStepRecord
from shamsu.ui.terminal import supports_tui
from shamsu.ui.theme import AMBER, BOLD, GREY, RED, paint

#: Answers that grant permission. Deliberately short and deliberately not
#: including the empty string.
_YES: frozenset[str] = frozenset({"y", "yes"})


def _settle(future: asyncio.Future[str | None], value: str | None) -> None:
    """Resolve a future that may already have been abandoned by a timeout."""
    if not future.done():
        future.set_result(value)


def parse_answer(line: str) -> ApprovalDecision:
    """Read one typed answer. Pure, so the rule is testable without a terminal.

    Anything that is not an explicit yes is a denial. There is no "default to
    approve on enter" — the cost of a wrong yes on a `CRITICAL` step is not
    symmetric with the cost of a wrong no.
    """
    return ApprovalDecision.APPROVED if line.strip().lower() in _YES else ApprovalDecision.DENIED


def describe(step: PlanStepRecord, reason: str) -> tuple[str, ...]:
    """The question, as lines. Says what will happen, not that something might.

    The files are listed because "a high-risk step" is not something a person
    can consent to. What they can consent to is a named step touching named
    files.
    """
    lines = [
        "",
        f"  {paint('approval required', AMBER + BOLD)}",
        f"  {paint(reason, GREY)}",
    ]
    if step.inputs:
        lines.append(f"  {paint('files', GREY)}  {', '.join(step.inputs)}")
    if step.acceptance_criteria:
        lines.append(f"  {paint('done when', GREY)}  {step.acceptance_criteria[0]}")
    lines.append(f"  {paint('anything other than y/yes denies it', GREY)}")
    return tuple(lines)


class DenyingApprover:
    """Refuses everything, without asking anyone.

    The default for any session that cannot reach a human. Distinct from
    passing `approver=None`, which stops the run instead: this one produces a
    real `DENIED` row, so the record shows a policy refusal rather than a
    missing decision.
    """

    async def decide(
        self,
        step: PlanStepRecord,
        *,
        reason: str,
        cancel: CancellationToken,
    ) -> ApprovalDecision:
        del step, reason, cancel
        return ApprovalDecision.DENIED


class AlwaysApprover:
    """Approves everything. Only ever selected by an explicit `--yes`.

    This is the one object in the system that can grant permission without a
    human, so it exists as a named class rather than as a `lambda: APPROVED`
    somewhere in the CLI — a reader looking for "what could authorise a
    destructive step?" finds exactly one answer, and it is this.

    It still writes a real `APPROVED` row per step, so an unattended run's
    record shows what was authorised rather than showing that the gate was
    absent.
    """

    async def decide(
        self,
        step: PlanStepRecord,
        *,
        reason: str,
        cancel: CancellationToken,
    ) -> ApprovalDecision:
        del step, reason
        cancel.raise_if_cancelled()
        return ApprovalDecision.APPROVED


class ConsoleApprover:
    """Asks at the terminal, and times out rather than waiting forever.

    The prompt is read on a worker thread so the event loop keeps running:
    cancellation has to remain responsive while a person decides, and a
    blocking `input()` on the loop thread would freeze the very token that
    stops the run.
    """

    #: How long a question waits before it becomes `TIMED_OUT`. Long enough to
    #: read a diff, short enough that an unattended run ends rather than hangs.
    TIMEOUT_SECONDS = 120.0

    def __init__(
        self,
        *,
        read_line: Callable[[str], str] | None = None,
        stream: TextIO | None = None,
        timeout: float | None = None,
    ) -> None:
        self._read_line = read_line or input
        self._stream = stream
        self._timeout = self.TIMEOUT_SECONDS if timeout is None else timeout

    def _write(self, lines: Sequence[str]) -> None:
        import sys

        target = self._stream or sys.stdout
        for line in lines:
            target.write(line + "\n")
        target.flush()

    async def decide(
        self,
        step: PlanStepRecord,
        *,
        reason: str,
        cancel: CancellationToken,
    ) -> ApprovalDecision:
        cancel.raise_if_cancelled()
        self._write(describe(step, reason))

        loop = asyncio.get_running_loop()
        answer: asyncio.Future[str | None] = loop.create_future()

        def read() -> None:
            """Block on stdin off the event loop, and report back safely."""
            try:
                line: str | None = self._read_line(paint("  approve? [y/N] ", AMBER))
            except (EOFError, KeyboardInterrupt, OSError, ValueError):
                # A closed or detached stdin is not a person saying yes.
                line = None
            loop.call_soon_threadsafe(_settle, answer, line)

        # A plain daemon thread, deliberately *not* `run_in_executor`.
        #
        # `asyncio.run` shuts the default executor down on the way out and
        # waits for its threads — so a question that timed out would leave the
        # process hanging at exit on an `input()` nobody is ever going to
        # answer. The timeout would be honoured and the program would still
        # never end. A daemon thread is abandoned at interpreter exit instead,
        # which is the only correct disposal for a blocked stdin read.
        threading.Thread(target=read, daemon=True, name="shamsu-approval").start()

        watcher = asyncio.ensure_future(cancel.wait_cancelled())
        waiters: set[asyncio.Future[Any]] = {answer, watcher}
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=self._timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

        if watcher in done:
            # The run was cancelled while the question was open. Propagated
            # rather than answered: a cancelled run must not also record a
            # decision nobody made.
            raise Cancelled(cancel.reason or "run cancelled")

        if answer not in done:
            self._write([f"  {paint('no answer in time — denied', RED)}", ""])
            return ApprovalDecision.TIMED_OUT

        line = answer.result()
        if line is None:
            self._write([f"  {paint('no input available — denied', RED)}", ""])
            return ApprovalDecision.TIMED_OUT

        decision = parse_answer(line)
        self._write([f"  {paint(decision.value, GREY)}", ""])
        return decision


class ScreenApprover:
    """Approver for the full-screen session, answered by a keypress.

    The TUI cannot use `ConsoleApprover`: it holds the terminal in raw mode and
    reads keys itself, so a blocking `input()` on a worker thread would fight
    it for stdin. Instead this one *parks* — `decide` returns an unresolved
    future, and the session's existing key loop resolves it.

    That keeps the established direction intact. The runtime asks a question
    and waits; the interface notices it is waiting, draws it, and answers.
    Nothing here drives the agent, and `runtime/` still knows nothing about a
    UI beyond the `Approver` protocol.
    """

    #: Matches `ConsoleApprover`. An unattended full-screen session must end
    #: rather than hold a question open forever.
    TIMEOUT_SECONDS = 120.0

    def __init__(self, *, timeout: float | None = None) -> None:
        self._pending: asyncio.Future[ApprovalDecision] | None = None
        self._timeout = self.TIMEOUT_SECONDS if timeout is None else timeout
        self.question: tuple[str, ...] = ()

    @property
    def waiting(self) -> bool:
        """Whether a question is open. The key loop checks this every frame."""
        return self._pending is not None and not self._pending.done()

    def answer(self, decision: ApprovalDecision) -> bool:
        """Resolve the open question. Returns whether there was one."""
        if self._pending is None or self._pending.done():
            return False
        self._pending.set_result(decision)
        return True

    def answer_key(self, key: str) -> bool:
        """Interpret one keystroke, using the same rule as the typed prompt."""
        if not key:
            return False
        return self.answer(parse_answer(key))

    async def decide(
        self,
        step: PlanStepRecord,
        *,
        reason: str,
        cancel: CancellationToken,
    ) -> ApprovalDecision:
        cancel.raise_if_cancelled()

        loop = asyncio.get_running_loop()
        pending: asyncio.Future[ApprovalDecision] = loop.create_future()
        self._pending = pending
        self.question = describe(step, reason)

        watcher = asyncio.ensure_future(cancel.wait_cancelled())
        waiters: set[asyncio.Future[Any]] = {pending, watcher}
        try:
            done, _ = await asyncio.wait(
                waiters,
                timeout=self._timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if watcher in done:
                raise Cancelled(cancel.reason or "run cancelled")
            if pending in done:
                return pending.result()
            return ApprovalDecision.TIMED_OUT
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            self._pending = None
            self.question = ()


def approver_for(
    *,
    assume_yes: bool = False,
    stream: TextIO | None = None,
) -> Approver:
    """Pick the approver a one-shot run should use.

    The rule is that consent has to come from somewhere real. An explicit
    `--yes` is a decision the user already made; a terminal can be asked; and a
    pipe or a CI job is neither, so it gets `DenyingApprover` rather than a
    prompt nothing will ever answer.
    """
    if assume_yes:
        return AlwaysApprover()
    if supports_tui(stream):
        return ConsoleApprover(stream=stream)
    return DenyingApprover()


__all__ = [
    "AlwaysApprover",
    "ConsoleApprover",
    "DenyingApprover",
    "ScreenApprover",
    "approver_for",
    "describe",
    "parse_answer",
]
