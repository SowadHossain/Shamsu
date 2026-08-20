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


def tail_events(
    path: Path,
    *,
    since_seq: int = -1,
    should_stop=lambda: False,
    poll_seconds: float = POLL_SECONDS,
    keepalive_seconds: float = KEEPALIVE_SECONDS,
) -> Iterator[bytes]:
    """Yield SSE frames for every event after `since_seq`, then follow the file.

    Reads by line offset rather than holding a handle open: the file is only
    ever appended to, and reopening each pass means a portal survives the
    session being archived or the file being rotated underneath it.
    """
    delivered = int(since_seq)
    consumed_lines = 0
    last_beat = time.monotonic()
    while not should_stop():
        records, consumed_lines = _read_from(path, consumed_lines)
        sent_any = False
        for record in records:
            seq = int(record.get("seq") or 0)
            if seq <= delivered:
                continue
            delivered = seq
            sent_any = True
            yield frame(seq, record)
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
