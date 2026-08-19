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

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._items)


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
