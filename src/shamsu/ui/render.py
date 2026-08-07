"""Turning a `RunView` into terminal lines. A pure function.

`render(view, width, height, now)` returns exactly `height` lines, each at most
`width` visible columns. No terminal, no globals, no clock of its own — the
time comes in as an argument so a test can assert on a rendered frame without
sleeping.

Layout:

    ┌─ SHAMSU ──────────────────────── ~/project ─┐   header
    │                                             │
    │  ▸ fix add() so it sums                     │   request
    │                                             │
    │  ● inspect   file.read calc.py              │   activity, scrolled
    │  ● author    file.patch calc.py             │   to the tail
    │  ✓ verify    pytest — 4 passed              │
    │                                             │
    ├─────────────────────────────────────────────┤
    │ author · step 1/1 · 00:12 · ^C cancel       │   footer
    └─────────────────────────────────────────────┘

Colour is emitted only when asked for. A pipe, a CI log, and a `--no-colour`
run all get the same text without escape codes, which is also what makes the
tests readable.
"""

from __future__ import annotations

from datetime import datetime

from shamsu.interfaces.enums import Phase, RunStatus
from shamsu.ui.view import Activity, Level, RunView

#: Minimum usable window. Below this the layout stops meaning anything, so the
#: renderer degrades to a single status line rather than drawing a broken box.
MIN_WIDTH = 34
MIN_HEIGHT = 6

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"

_COLOURS: dict[str, str] = {
    Level.STEP: "\x1b[36m",  # cyan — a phase boundary
    Level.OK: "\x1b[32m",  # green
    Level.FAIL: "\x1b[31m",  # red
    Level.NOTE: "\x1b[2m",  # dim
    Level.STOP: "\x1b[33m",  # yellow
}

_GLYPHS: dict[str, str] = {
    Level.STEP: "▸",
    Level.OK: "●",
    Level.FAIL: "✗",
    Level.NOTE: "·",
    Level.STOP: "■",
}

#: Phase colours for the footer. Deliberately the same cyan/green/red family as
#: the activity glyphs: two colour vocabularies in one window is one too many.
_PHASE_COLOUR: dict[Phase, str] = {
    Phase.INSPECT: "\x1b[36m",
    Phase.PLAN: "\x1b[35m",
    Phase.AUTHOR: "\x1b[33m",
    Phase.VERIFY: "\x1b[32m",
    Phase.REPAIR: "\x1b[31m",
    Phase.COMPLETE: "\x1b[32m",
}

#: Frames for the "waiting on the model" spinner. A local model can take tens of
#: seconds; a static interface reads as a hang.
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def render(
    view: RunView,
    width: int,
    height: int,
    *,
    now: datetime | None = None,
    colour: bool = True,
    tick: int = 0,
) -> list[str]:
    """The whole window, as exactly `height` lines of at most `width` columns."""
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return _cramped(view, width, height, now=now)

    moment = now or datetime.now(tz=(view.started_at.tzinfo if view.started_at else None))

    header = _header(view, width, colour=colour)
    footer = _footer(view, width, now=moment, colour=colour, tick=tick)
    request = _request(view, width, colour=colour)

    body_height = height - len(header) - len(footer) - len(request)
    body = _activity(view, width, max(0, body_height), colour=colour)

    return [*header, *request, *body, *footer][:height]


# -- sections --------------------------------------------------------------


def _header(view: RunView, width: int, *, colour: bool) -> list[str]:
    """Title bar with the workspace, right-aligned and truncated from the left.

    Left-truncated because the informative end of a path is the far end:
    `…/shamsu/src` tells you where you are, `/home/user/proj…` does not.
    """
    title = "SHAMSU"

    # The line is exactly:  "┌─ " title " "  ──fill──  " " place " ─┐"
    # so the columns not available for `fill` are these, counted rather than
    # guessed — an off-by-one here pushes the frame one column wide and the
    # whole box wraps.
    left_columns = len("┌─ ") + len(title) + len(" ")
    bare_right = len("─┐")
    placed_right = len(" ") + len(" ─┐")

    place = ""
    if view.workspace:
        room = width - left_columns - placed_right - 1  # keep at least one fill
        place = _tail(view.workspace, max(0, room))

    if place:
        fill = width - left_columns - placed_right - len(place)
    else:
        fill = width - left_columns - bare_right

    if fill < 1:
        place, fill = "", max(1, width - left_columns - bare_right)

    left = f"┌─ {_paint(title, _BOLD, colour)} "
    right = f" {_paint(place, _DIM, colour)} ─┐" if place else "─┐"
    return [left + "─" * fill + right]


def _request(view: RunView, width: int, *, colour: bool) -> list[str]:
    if not view.request:
        return ["│" + " " * (width - 2) + "│"]
    text = _fit(view.request, width - 8)
    body = f"  {_paint('▸', _BOLD, colour)} {text}"
    return ["│" + _pad(body, width - 2, colour=colour) + "│", "│" + " " * (width - 2) + "│"]


def _activity(view: RunView, width: int, height: int, *, colour: bool) -> list[str]:
    """The tail of the activity log, padded to fill the pane.

    The *tail*, always. A run that scrolled past its interesting moment is
    annoying; a run whose latest line is off-screen is unusable.
    """
    if height <= 0:
        return []

    visible = view.activity[-height:]
    lines = [
        f"│{_pad(_activity_line(item, width - 4, colour=colour), width - 2, colour=colour)}│"
        for item in visible
    ]

    blank = "│" + " " * (width - 2) + "│"
    return [*lines, *([blank] * (height - len(lines)))]


def _activity_line(item: Activity, width: int, *, colour: bool) -> str:
    glyph = _GLYPHS.get(item.level, "·")
    label = item.label[:10].ljust(10)
    detail = _fit(item.detail, max(0, width - 14))

    painted = _paint(glyph, _COLOURS.get(item.level, ""), colour)
    body = f"  {painted} {label} {detail}".rstrip()
    return body


def _footer(view: RunView, width: int, *, now: datetime, colour: bool, tick: int) -> list[str]:
    """The status bar, dropped from the middle when it does not fit.

    Phase and the key hint are pinned. Everything between them is optional and
    is shed widest-first — truncating the right-hand end instead would cut off
    "^C cancel", which is the one thing on screen a user may urgently need.
    """
    phase = _paint(view.phase.value, _PHASE_COLOUR.get(view.phase, ""), colour)
    hint = _hint(view)

    optional: list[str] = []
    if view.step_total:
        optional.append(f"step {view.step_index}/{view.step_total}")
    optional.append(_clock(view.elapsed(now)))
    if view.evidence:
        optional.append(f"{len(view.evidence)} evidence")
    if view.waiting_on and view.running:
        optional.append(f"{SPINNER[tick % len(SPINNER)]} {view.waiting_on}")

    budget = width - 3  # one leading space, two border columns
    while True:
        body = " · ".join([phase, *optional, hint])
        if _visible_length(body) <= budget or not optional:
            break
        optional.pop()  # shed the least essential remaining item

    body = body if _visible_length(body) <= budget else hint

    return [
        "├" + "─" * (width - 2) + "┤",
        "│" + _pad(f" {body}", width - 2, colour=colour) + "│",
        "└" + "─" * (width - 2) + "┘",
    ]


def _hint(view: RunView) -> str:
    if view.cancelling:
        return "stopping…"
    if not view.running:
        return {
            RunStatus.COMPLETED: "done",
            RunStatus.CANCELLED: "cancelled",
            RunStatus.FAILED: "failed",
            RunStatus.TIMED_OUT: "timed out",
        }.get(view.status, "finished") + "  ·  q quit"
    return "^C cancel"


def _cramped(view: RunView, width: int, height: int, *, now: datetime | None) -> list[str]:
    """A window too small for the layout still has to say something true."""
    moment = now or datetime.now(tz=(view.started_at.tzinfo if view.started_at else None))
    status = "done" if not view.running else view.phase.value
    line = _fit(f"shamsu {status} {_clock(view.elapsed(moment))}", max(1, width))
    return [line, *([""] * max(0, height - 1))][: max(1, height)]


# -- text helpers ----------------------------------------------------------


def _fit(text: str, width: int) -> str:
    """Truncate to `width` columns with an ellipsis, never past the end."""
    flat = " ".join(text.split())
    if width <= 0:
        return ""
    if len(flat) <= width:
        return flat
    return flat[: max(0, width - 1)] + "…"


def _tail(text: str, width: int) -> str:
    """Keep the *end* of a string — for paths, where the end is the answer."""
    if width <= 0:
        return ""
    return text if len(text) <= width else "…" + text[-(width - 1) :]


def _pad(text: str, width: int, *, colour: bool) -> str:
    """Pad to `width` visible columns, ignoring escape sequences."""
    visible = _visible_length(text) if colour else len(text)
    if visible > width:
        # Only reachable when a caller mis-sizes a section; truncating keeps
        # the frame rectangular rather than letting one line break the box.
        return text[:width] if not colour else text
    return text + " " * (width - visible)


def _visible_length(text: str) -> int:
    """Length excluding ANSI escape sequences."""
    length = 0
    index = 0
    while index < len(text):
        if text[index] == "\x1b":
            end = text.find("m", index)
            index = len(text) if end == -1 else end + 1
            continue
        length += 1
        index += 1
    return length


def _paint(text: str, code: str, colour: bool) -> str:
    return f"{code}{text}{_RESET}" if colour and code else text


def _clock(seconds: float) -> str:
    minutes, remainder = divmod(int(seconds), 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{remainder:02d}"
    return f"{minutes:02d}:{remainder:02d}"


__all__ = ["MIN_HEIGHT", "MIN_WIDTH", "SPINNER", "render"]
