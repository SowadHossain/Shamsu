"""Let the user speak while the agent is still working.

Inspired by smallcode's TUI, which runs a raw-stdin event loop: keystrokes are
handled whether or not the agent is mid-turn, so typing never blocks and never
waits its turn.

The problem it solves is specific to this harness. A turn here can run 24
rounds; live sessions have taken 18 minutes over 18 whole-file writes and 25
minutes across 17 mutations that changed nothing. For every second of that the
user could see it going wrong and had exactly two options: watch, or Ctrl-C and
lose the turn. "You are editing the wrong file" is a sentence that would have
saved twenty minutes, and there was nowhere to type it.

Two decisions make this safe rather than merely possible:

**Injected at a round boundary, never mid-round.** A message appended while a
tool call is in flight lands between the assistant turn and its own tool result
and orphans the `tool_call_id`. Waiting for the boundary costs at most one
round and keeps the transcript well-formed.

**Recorded, not whispered.** Feedback is appended as an ordinary user message,
so it is in the transcript, in the archive, and in `history_search` afterwards.
A steer that changed the course of a session and left no trace is the kind of
thing that makes a log impossible to read six weeks later.

Steering the RUNNING turn is the difference from smallcode, whose TUI queues
the input as the next turn once the current one finishes. Mid-flight is when
the correction is worth something.
"""
from __future__ import annotations

import threading
from collections import deque


class FeedbackQueue:
    """Thread-safe hand-off from whoever reads the keyboard to the agent loop.

    Deliberately tiny and free of I/O. The REPL owns the console and knows how
    to read a key on this platform without fighting the approval prompt; the
    loop only needs to ask "has anything been said?". Keeping the two apart is
    what makes the loop testable without a terminal.
    """

    def __init__(self, limit: int = 8) -> None:
        self._items: deque[str] = deque(maxlen=max(1, limit))
        self._lock = threading.Lock()

    def push(self, text: str) -> bool:
        """Record something the user typed. Returns whether it was kept."""
        message = " ".join((text or "").split())
        if not message:
            return False
        with self._lock:
            self._items.append(message)
        return True

    def drain(self) -> list[str]:
        """Take everything said since the last check, oldest first."""
        with self._lock:
            items = list(self._items)
            self._items.clear()
        return items

    def __len__(self) -> int:
        """How much is waiting. The toolbar shows this live, so a steer you
        typed is visibly queued rather than apparently swallowed."""
        with self._lock:
            return len(self._items)

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._items)


class TaskQueue:
    """Work the user lined up while a turn was running, to run after it ends.

    The second half of a distinction the single feedback queue could not make.
    "Also log a warning" and "next, write the tests" are different requests:
    the first is a correction to the job in flight and is worthless if it
    arrives late; the second is a new job and is actively harmful if it arrives
    early, because it lands mid-task as an interruption and the model abandons
    what it was doing. One queue serving both intentions means one of them is
    always delivered at the wrong moment.

    So this one deliberately does NOT interrupt. It waits for the turn to end -
    and, once history compaction lands, for the window to be reclaimed - and
    then runs as an ordinary prompt of its own.

    FIFO, and bounded for the same reason the feedback queue is: a queue that
    grows without limit is a way to discover at 3am that you have scheduled
    forty tasks.
    """

    def __init__(self, limit: int = 32) -> None:
        self._items: deque[str] = deque(maxlen=max(1, limit))
        self._lock = threading.Lock()

    def push(self, text: str) -> bool:
        """Line up a task. Returns whether it was kept."""
        message = " ".join((text or "").split())
        if not message:
            return False
        with self._lock:
            self._items.append(message)
        return True

    def pop(self) -> str:
        """Take the next task, or "" when there is nothing waiting."""
        with self._lock:
            return self._items.popleft() if self._items else ""

    def peek_all(self) -> list[str]:
        """Everything waiting, oldest first, without consuming it."""
        with self._lock:
            return list(self._items)

    def clear(self) -> int:
        """Drop everything waiting. Returns how many were dropped."""
        with self._lock:
            count = len(self._items)
            self._items.clear()
        return count

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __bool__(self) -> bool:
        return len(self) > 0


def render_interjection(messages: list[str]) -> str:
    """How the steer reaches the model.

    Framed as an interruption on purpose. Dropped in as a bare user message it
    reads like the next request, and a model that has just been told "no, the
    other file" will finish the current job first and then start a new one.
    Saying plainly that this arrived mid-task, and that it takes precedence, is
    what turns it into a correction instead of a queue entry.
    """
    if not messages:
        return ""
    body = "\n".join(f"- {message}" for message in messages)
    if len(messages) == 1:
        body = messages[0]
    return (
        "[The user interrupted while you were working. This takes precedence "
        "over what you were doing - adjust now rather than finishing first.]\n"
        + body
    )
