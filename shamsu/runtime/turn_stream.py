"""One turn stream, three renderers.

A turn already produced exactly the lines a user wants to see - the CLI printed
all of them and Telegram threw most of them away, because each surface had its
own ad-hoc wiring and its own idea of what was worth sending. The strings were
never the problem; delivery was.

So there is one event, one bus, and renderers that differ only in how they
paint it. `TurnEvent.text` is deliberately the CLI's own string: a renderer that
wants parity prints `text`, a renderer that wants a richer view reads `data`.
That is what makes "every line reaches every surface" provable rather than
aspirational - `tests/test_turn_stream_parity.py` compares the two lists.

`activity.jsonl` sits beside `messages.jsonl` and is deliberately NOT the same
file: messages are the model's context and must stay lossless and clean, while
activity is high-frequency UI telemetry. Mixing them would put status ticks into
the model's memory.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from shamsu.safety.commands import redact

Kind = Literal[
    "turn.start",
    "status",
    "activity",
    "tool.call",
    "tool.result",
    "approval",
    "assistant",
    "turn.end",
    "error",
]

KINDS: frozenset[str] = frozenset(
    {
        "turn.start",
        "status",
        "activity",
        "tool.call",
        "tool.result",
        "approval",
        "assistant",
        "turn.end",
        "error",
    }
)

#: Kinds a bounded queue may never drop. A missed status tick costs nothing -
#: the next one replaces it anyway - but a missed `tool.call` or `turn.end`
#: leaves a surface permanently out of step with what actually happened.
NEVER_DROPPED: frozenset[str] = frozenset(
    {"turn.start", "tool.call", "tool.result", "approval", "assistant", "turn.end", "error"}
)

#: What a full queue sheds, in order. Derived from `NEVER_DROPPED` rather than
#: written out beside it, so the two cannot drift: adding a kind to the
#: protected set removes it from here by construction.
DROP_ORDER: tuple[str, ...] = tuple(
    kind for kind in ("status", "activity") if kind not in NEVER_DROPPED
)

DEFAULT_QUEUE_SIZE = 1000


def body_kinds(verbosity: str = "normal") -> frozenset[str]:
    """Which kinds become a visible LINE, at each verbosity.

    One definition, shared by every renderer. That is the point: parity between
    the CLI and the phone is only meaningful if both are reading the same rule,
    and a test that compares two lists built from two different rules proves
    nothing.
    """
    level = (verbosity or "normal").strip().lower()
    if level == "quiet":
        return frozenset({"tool.call"})
    if level == "verbose":
        return frozenset({"activity", "tool.call", "tool.result", "status"})
    return frozenset({"activity", "tool.call"})


@dataclass(frozen=True)
class TurnEvent:
    """One thing that happened during a turn.

    `seq` is monotonic per turn, so a renderer that reconnects can dedupe and
    reorder without guessing, and a subscriber can ask for "everything after
    N" instead of replaying a whole session.
    """

    seq: int
    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    turn_id: str = ""
    session_id: str = ""
    workspace: str = ""
    source: str = "cli"

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "text": self.text,
            "data": self.data,
            "ts": self.ts,
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "workspace": self.workspace,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TurnEvent:
        data = raw.get("data")
        return cls(
            seq=int(raw.get("seq") or 0),
            kind=str(raw.get("kind") or ""),
            text=str(raw.get("text") or ""),
            data=dict(data) if isinstance(data, dict) else {},
            ts=float(raw.get("ts") or 0.0),
            turn_id=str(raw.get("turn_id") or ""),
            session_id=str(raw.get("session_id") or ""),
            workspace=str(raw.get("workspace") or ""),
            source=str(raw.get("source") or ""),
        )


_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._-]")


def activity_path(workspace: Path | str, session_id: str) -> Path:
    """Where a session's turn stream is written.

    The id is sanitised rather than trusted: it reaches here from a Telegram
    payload on one path, and `..` in a session id would otherwise choose the
    file.
    """
    safe = _UNSAFE_ID.sub("-", (session_id or "").strip()) or "session"
    return Path(workspace) / ".shamsu" / "sessions" / safe / "activity.jsonl"


class Subscription:
    """A bounded, catch-up-able view of one session's turn stream.

    Bounded because a subscriber that stops reading - a phone that lost signal,
    a browser tab that was backgrounded - must not grow the producer's memory
    without limit. The bound is applied by DROPPING, and what it drops is
    chosen rather than arbitrary: status ticks first, then the oldest activity
    line, and never anything in `NEVER_DROPPED`.
    """

    def __init__(
        self,
        *,
        since_seq: int = -1,
        since_turn: str = "",
        max_queue: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        self.since_seq = int(since_seq)
        # `seq` restarts at 1 for every turn, so a bare "everything after 12"
        # would silently swallow the first twelve events of the NEXT turn.
        # `since_seq` therefore means "after 12 OF THIS TURN": the turn is
        # named, or taken from the first event seen, and the filter stops
        # applying the moment the turn changes.
        self.since_turn = since_turn
        self.max_queue = max(1, int(max_queue))
        self.dropped = 0
        self.closed = False
        self._resume_turn: str | None = since_turn or None
        self._queue: deque[TurnEvent] = deque()
        self._lock = threading.Lock()
        self._ready = threading.Event()

    def offer(self, event: TurnEvent) -> None:
        if self.closed:
            return
        if self._resume_turn is None:
            self._resume_turn = event.turn_id
        if event.turn_id == self._resume_turn and event.seq <= self.since_seq:
            return
        with self._lock:
            self._queue.append(event)
            while len(self._queue) > self.max_queue and self._make_room():
                pass
        self._ready.set()

    def _make_room(self) -> bool:
        """Discard one droppable event. False when everything left must stay."""
        for droppable in DROP_ORDER:
            for index, event in enumerate(self._queue):
                if event.kind == droppable:
                    del self._queue[index]
                    self.dropped += 1
                    return True
        return False

    def drain(self) -> list[TurnEvent]:
        with self._lock:
            events = list(self._queue)
            self._queue.clear()
        self._ready.clear()
        return events

    def wait(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def close(self) -> None:
        self.closed = True
        self._ready.set()


class TurnStream:
    """Per-session fan-out: the durable file, then every live renderer.

    Ordering is deliberate and matches the rule the rest of the storage design
    follows - durable first, projection second. A process that dies between the
    two leaves the file complete and a renderer one line short, which is
    recoverable. The reverse would show a surface a line that was never
    recorded.
    """

    def __init__(
        self,
        workspace: Path | str,
        session_id: str,
        *,
        path: Path | None = None,
        max_queue: int = DEFAULT_QUEUE_SIZE,
        persist: bool = True,
    ) -> None:
        self.workspace = Path(workspace)
        self.session_id = str(session_id or "")
        self.max_queue = max(1, int(max_queue))
        self.persist = bool(persist)
        self.path = path or activity_path(self.workspace, self.session_id)
        self._renderers: list[Callable[[TurnEvent], None]] = []
        self._subscriptions: list[Subscription] = []
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    # -- wiring ----------------------------------------------------------

    def add_renderer(self, renderer: Callable[[TurnEvent], None]) -> Callable[[], None]:
        """Attach a synchronous sink. Returns the function that detaches it."""
        with self._lock:
            self._renderers.append(renderer)

        def remove() -> None:
            with self._lock:
                if renderer in self._renderers:
                    self._renderers.remove(renderer)

        return remove

    def subscribe(
        self,
        since_seq: int = -1,
        *,
        since_turn: str = "",
        max_queue: int | None = None,
    ) -> Subscription:
        """Replay what is already on disk after `since_seq`, then tail live.

        Replay and registration happen under the SAME lock `publish` writes
        under, so an event landing at this exact moment is delivered once:
        either it is already in the file and replayed, or it arrives live -
        never both.
        """
        with self._lock:
            recorded = self._replay_unlocked()
            # Resolve the turn `since_seq` refers to HERE, from the file, not
            # from the first live event. Left to the first live event, a
            # subscriber that caught up at the end of one turn would pin itself
            # to the NEXT turn and then discard its opening events as already
            # seen - the exact hazard a per-turn sequence creates.
            resume = since_turn or (recorded[-1].turn_id if recorded else "")
            subscription = Subscription(
                since_seq=since_seq,
                since_turn=resume,
                max_queue=max_queue or self.max_queue,
            )
            for event in _after(recorded, since_seq, resume):
                subscription.offer(event)
            self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        subscription.close()
        with self._lock:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)

    # -- the bus ---------------------------------------------------------

    def publish(self, event: TurnEvent) -> None:
        with self._lock:
            self._write(event)
            renderers = list(self._renderers)
            subscriptions = list(self._subscriptions)
        for renderer in renderers:
            try:
                renderer(event)
            except Exception:  # noqa: BLE001 - a renderer must never fail a turn
                pass
        for subscription in subscriptions:
            try:
                subscription.offer(event)
            except Exception:  # noqa: BLE001
                pass

    def _write(self, event: TurnEvent) -> None:
        if not self.persist:
            return
        # Redacted on the way to disk, exactly as the Telegram transport
        # redacts on the way out. Renderers still see the raw string, so the
        # CLI prints what it has always printed.
        record = event.to_dict()
        record["text"] = redact(event.text)
        try:
            with self._write_lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=True, default=str) + "\n")
        except OSError:
            # Telemetry is not worth failing a turn over.
            pass

    def replay(self, since_seq: int = -1, since_turn: str = "") -> list[TurnEvent]:
        """Everything recorded after `since_seq`, skipping unparseable lines.

        Skipping rather than raising is the lesson from a `messages.jsonl` that
        an editor reformatted: one bad line must not cost the whole file.
        """
        with self._lock:
            return self._replay_unlocked(since_seq, since_turn)

    def _replay_unlocked(self, since_seq: int = -1, since_turn: str = "") -> list[TurnEvent]:
        if not self.path.exists():
            return []
        events: list[TurnEvent] = []
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            event = TurnEvent.from_dict(record)
            events.append(event)
        return _after(events, since_seq, since_turn)


def _after(events: list[TurnEvent], since_seq: int, since_turn: str) -> list[TurnEvent]:
    """Drop what a caller has already seen - see `Subscription.since_turn`."""
    if since_seq < 0:
        return events
    resume = since_turn or (events[-1].turn_id if events else "")
    return [
        event
        for event in events
        if not (event.turn_id == resume and event.seq <= since_seq)
    ]
