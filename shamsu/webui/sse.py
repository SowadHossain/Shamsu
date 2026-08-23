"""Server-Sent Events over a file that is being appended to.

SSE rather than WebSockets because the stream is one-directional and the
browser reconnects on its own, carrying `Last-Event-ID`. That header maps onto
`TurnEvent.seq` exactly, which is what makes a hard refresh mid-turn lossless:
the client says how far it got, and the server replays from there.

The source is `activity.jsonl` rather than a live in-process subscription, and
that is deliberate. The file is the record; tailing it means a portal started
*after* a turn began still sees the whole turn, and a portal in another process
would work identically the day one exists.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator

#: How often to look for new lines. Fast enough to feel live, slow enough that
#: an idle portal is not a spinning read loop.
POLL_SECONDS = 0.2

#: A comment line every so often, so an idle connection is not reaped by the
#: browser or by anything in between.
KEEPALIVE_SECONDS = 15.0


def frame(event_id: int, payload: dict) -> bytes:
    """One SSE message. `id:` is what comes back as `Last-Event-ID`."""
    body = json.dumps(payload, ensure_ascii=True)
    return f"id: {event_id}\ndata: {body}\n\n".encode("utf-8")


def keepalive() -> bytes:
    return b": keepalive\n\n"


def resume_line(path: Path) -> int:
    """Where a FRESH subscriber should start reading.

    Not the end of the file, and not the start of it. Both are wrong:

    * From line 0, the browser is sent the entire activity log - and it has
      already fetched the conversation from `/api/.../messages` before opening
      this stream, so the whole thread arrives a second time in raw form, every
      `read_file` and "context is filling" underneath the finished chat. Live
      2026-08-23 that was 1,831 records replayed onto 24 chat rows.
    * From the end, a turn that is ALREADY RUNNING when the page loads has its
      opening events skipped - `turn.start` has been written and `turn.end` has
      not, so `/messages` does not carry it either and it is lost from both.

    So: the start of the last turn that has not ended yet, or the end of the
    file when every turn is complete. The in-flight turn arrives whole, and
    nothing a reader already has is repeated.
    """
    records, total = _read_from(path, 0)
    if not records:
        return 0
    open_at: int | None = None
    for index, record in enumerate(records):
        kind = str(record.get("kind") or "")
        if kind == "turn.start":
            open_at = index
        elif kind == "turn.end":
            open_at = None
    return total if open_at is None else open_at


def tail_events(
    path: Path,
    *,
    since_line: int = 0,
    should_stop=lambda: False,
    poll_seconds: float = POLL_SECONDS,
    keepalive_seconds: float = KEEPALIVE_SECONDS,
) -> Iterator[bytes]:
    """Yield SSE frames for every event after line `since_line`, then follow.

    Reads by line offset rather than holding a handle open: the file is only
    ever appended to, and reopening each pass means a portal survives the
    session being archived or the file being rotated underneath it.

    The cursor is the LINE NUMBER, and that is the fix rather than the detail.
    It used to be the event's own `seq`, deduped with `if seq <= delivered`,
    on the assumption that `seq` rises for the length of a session. It does
    not: `SimpleChatLoop.run` resets `_event_seq` to 0 for every turn, on
    purpose - "a counter that carried across turns would make 'everything
    after N' mean different things on different surfaces".

    Measured on one live session 2026-08-23: 1,831 records, max seq 270, and
    **11 resets**. So once a long turn pushed `delivered` up to 270, every
    event of every later turn arrived numbered 1, 2, 3... and was silently
    dropped as already-seen. Live updates stopped after the first long turn,
    and `Last-Event-ID` resumed to a position that meant nothing.

    A line number in an append-only file is monotonic by construction, which
    is the property this needs and `seq` never had.
    """
    consumed_lines = max(0, int(since_line))
    last_beat = time.monotonic()
    while not should_stop():
        records, consumed_lines = _read_from(path, consumed_lines)
        sent_any = False
        for record in records:
            sent_any = True
            # The id a browser echoes back as `Last-Event-ID`, so it must be
            # the same cursor this function resumes from.
            yield frame(consumed_lines - len(records) + records.index(record) + 1, record)
        now = time.monotonic()
        if sent_any:
            last_beat = now
        elif now - last_beat >= keepalive_seconds:
            last_beat = now
            yield keepalive()
        time.sleep(poll_seconds)


def _read_from(path: Path, consumed_lines: int) -> tuple[list[dict], int]:
    """New records since `consumed_lines`, and the new line count.

    A line that does not parse is skipped, not fatal - the same rule the
    transcript reader learned the hard way when an editor reformatted a
    `.jsonl` and 655 of 657 lines stopped parsing at once.
    """
    if not path.exists():
        return [], consumed_lines
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], consumed_lines
    if len(lines) <= consumed_lines:
        # Truncated or replaced: start again rather than serve nothing forever.
        return ([], len(lines)) if len(lines) < consumed_lines else ([], consumed_lines)
    fresh: list[dict] = []
    for line in lines[consumed_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            fresh.append(record)
    return fresh, len(lines)
