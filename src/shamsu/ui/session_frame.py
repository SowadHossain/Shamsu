"""The interactive session, as a frame.

`render_session(state, width, height)` returns exactly `height` lines. It is a
pure function of its argument — no clock it reads itself, no terminal, no
buffer — so what appears on screen is asserted by comparing strings, which is
the property `ui/__init__.py` says v1 never had.

The layout is OpenCode's, in SHAMSU's vocabulary:

    ┌─ SHAMSU ─────────────────────── ~/project ─┐   header
    │  › the request                             │
    │  ● author  file.patch  calc.py       0.2s  │   transcript, tail-anchored
    ├────────────────────────────────────────────┤
    │ build · qwen2.5-coder:14b · ✓2 evidence    │   status
    └────────────────────────────────────────────┘
     › fix the add function▏                        input, below the box
       /model   show the model, or switch          suggestions, when open

The input sits *below* the frame rather than inside it. A box-drawn input line
has to be redrawn on every keystroke and gets the cursor column wrong the moment
a wide character is typed; a plain line does not, and the terminal's own cursor
lands where it should.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shamsu.ui.commands import Command
from shamsu.ui.theme import AMBER, BLUE, BOLD, GREEN, GREY, RED, VIOLET, paint
from shamsu.ui.view import Activity, Level

MIN_WIDTH = 34
MIN_HEIGHT = 8

_LEVEL_COLOUR = {
    Level.STEP: AMBER,
    Level.OK: GREEN,
    Level.FAIL: RED,
    Level.NOTE: GREY,
    Level.STOP: AMBER,
}

_LEVEL_GLYPH = {
    Level.STEP: "▸",
    Level.OK: "●",
    Level.FAIL: "✗",
    Level.NOTE: "·",
    Level.STOP: "■",
}

#: How many suggestions to show at once. Enough to choose from, few enough that
#: the transcript does not disappear behind the dropdown.
MAX_SUGGESTIONS = 6


@dataclass
class SessionState:
    """Everything the frame draws. Owned by the driver, never by the renderer."""

    workspace: str = ""
    model: str = ""
    mode: str = "build"

    #: The session transcript: requests the user made and activity from runs.
    transcript: list[Activity] = field(default_factory=list)

    #: What is being typed, and where the cursor is within it.
    text: str = ""
    cursor: int = 0

    suggestions: tuple[Command, ...] = ()

    #: Workspace paths offered for an `@…` reference. Only one of this and
    #: `suggestions` is ever populated: the dropdown shows commands or files,
    #: never both, because `@` and `/` cannot both be being typed.
    files: tuple[str, ...] = ()

    selected: int = 0

    #: Past runs, shown before the first request of a session. `(when, status,
    #: request)`, newest first.
    recent: tuple[tuple[str, str, str], ...] = ()

    busy: bool = False
    spinner: str = ""
    status: str = ""
    evidence: int = 0

    def note(self, level: str, label: str, detail: str = "") -> None:
        self.transcript.append(Activity(level=level, label=label, detail=detail))


def render_session(
    state: SessionState,
    width: int,
    height: int,
    *,
    colour: bool = True,
) -> list[str]:
    """The whole window, as exactly `height` lines of at most `width` columns."""
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return _cramped(state, width, height, colour=colour)

    suggestions = _suggestions(state, width, colour=colour)
    input_line = _input(state, colour=colour)

    # The box takes whatever is left after the input and its dropdown.
    box_height = height - len(suggestions) - 1
    header = _header(state, width, colour=colour)
    status = _status(state, width, colour=colour)

    body_height = max(0, box_height - len(header) - len(status))

    # Recent runs stand in for a transcript only while there is none: once the
    # session has done something, what it did is more useful than what it did
    # last week.
    recent = _recent(state, width, colour=colour) if state.recent and not state.transcript else []
    body = _transcript(state, width, max(0, body_height - len(recent)), colour=colour)

    return [*header, *body, *recent, *status, input_line, *suggestions][:height]


def _header(state: SessionState, width: int, *, colour: bool) -> list[str]:
    title = paint("SHAMSU", AMBER + BOLD, colour)
    place = _elide_left(state.workspace, max(0, width - 16))

    left = f"┌─ {title} "
    right = f" {paint(place, GREY, colour)} ─┐" if place else "─┐"
    fill = max(0, width - (len("┌─ SHAMSU ") + _visible(right)))
    return [paint("", "", colour) + left + "─" * fill + right]


def _transcript(state: SessionState, width: int, height: int, *, colour: bool) -> list[str]:
    """The tail of the transcript, padded to fill the body.

    Anchored to the end rather than the start: the newest line is the one being
    waited on, and a pane that scrolls off the top is normal where one that
    hides the present is not.
    """
    if height <= 0:
        return []

    inner = width - 2
    visible = state.transcript[-height:]
    lines = [
        f"│{_pad(_activity(item, inner - 2, colour=colour), inner, colour=colour)}│"
        for item in visible
    ]

    blank = "│" + " " * inner + "│"
    return [*[blank] * (height - len(lines)), *lines] if len(lines) < height else lines


def _activity(item: Activity, width: int, *, colour: bool) -> str:
    glyph = paint(_LEVEL_GLYPH.get(item.level, "·"), _LEVEL_COLOUR.get(item.level, ""), colour)
    label = paint(f"{item.label:<9}", _LEVEL_COLOUR.get(item.level, GREY), colour)
    detail = _fit(item.detail, max(0, width - 13))
    return f"  {glyph} {label} {paint(detail, GREY, colour)}".rstrip()


def _status(state: SessionState, width: int, *, colour: bool) -> list[str]:
    mode = paint(state.mode, GREEN if state.mode == "build" else BLUE, colour)
    model = paint(state.model, VIOLET, colour)

    parts = [mode, model]
    if state.evidence:
        parts.append(paint(f"✓{state.evidence} evidence", GREEN, colour))
    if state.busy:
        parts.append(paint(f"{state.spinner} {state.status or 'working'}", AMBER, colour))
        parts.append(paint("^C stop", GREY, colour))
    elif state.status:
        parts.append(paint(state.status, GREY, colour))

    body = paint(" · ", GREY, colour).join(parts)
    while _visible(body) > width - 3 and len(parts) > 2:
        parts.pop()
        body = paint(" · ", GREY, colour).join(parts)

    divider = "├" + "─" * (width - 2) + "┤"
    return [divider, f"│ {_pad(body, width - 3, colour=colour)}│", "└" + "─" * (width - 2) + "┘"]


def _input(state: SessionState, *, colour: bool) -> str:
    """The prompt line. The terminal's own cursor marks the position."""
    return f" {paint('›', AMBER, colour)} {state.text}"


def _suggestions(state: SessionState, width: int, *, colour: bool) -> list[str]:
    """The dropdown: commands for `/`, workspace paths for `@`."""
    if state.files:
        return _dropdown(
            [(path, "") for path in state.files],
            state.selected,
            width,
            total=len(state.files),
            colour=colour,
        )

    if not state.suggestions:
        return []

    return _dropdown(
        [(command.usage, command.summary) for command in state.suggestions],
        state.selected,
        width,
        total=len(state.suggestions),
        colour=colour,
    )


def _dropdown(
    entries: list[tuple[str, str]],
    selected: int,
    width: int,
    *,
    total: int,
    colour: bool,
) -> list[str]:
    shown = entries[:MAX_SUGGESTIONS]
    label_width = max(len(label) for label, _ in shown)

    lines = []
    for index, (label, summary) in enumerate(shown):
        chosen = index == selected
        marker = paint("›", AMBER, colour) if chosen else " "
        name = paint(label.ljust(label_width), AMBER if chosen else BLUE, colour)
        tail = _fit(summary, max(0, width - label_width - 8))
        lines.append(f"   {marker} {name}  {paint(tail, GREY, colour)}".rstrip())

    if total > MAX_SUGGESTIONS:
        lines.append(paint(f"     … {total - MAX_SUGGESTIONS} more", GREY, colour))
    return lines


def _recent(state: SessionState, width: int, *, colour: bool) -> list[str]:
    """Past runs, for a session that has not done anything yet.

    Read from the state database, which has recorded every run since the first
    time the agent was pointed at this workspace -- so an empty transcript does
    not mean an empty history.
    """
    inner = width - 2
    heading = _pad(paint("  recent in this workspace", GREY, colour), inner, colour=colour)
    lines = [f"│{_pad('', inner, colour=colour)}│", f"│{heading}│"]

    for when, status, request in state.recent[:5]:
        good = status.lower() in ("completed", "complete")
        glyph = paint("✓" if good else "✗", GREEN if good else RED, colour)
        body = (
            f"  {glyph} {paint(when, GREY, colour)}  "
            f"{_fit(request, max(0, inner - 24))}  {paint(status, GREY, colour)}"
        )
        lines.append(f"│{_pad(body, inner, colour=colour)}│")

    return lines


def _cramped(state: SessionState, width: int, height: int, *, colour: bool) -> list[str]:
    """Too small for the layout: say something true rather than draw a broken box."""
    line = _fit(f"{state.mode} · {state.status or 'ready'}", max(0, width))
    lines = [paint(line, GREY, colour), f"› {state.text}"]
    return lines[:height] if height else []


# -- text helpers ----------------------------------------------------------


def _visible(text: str) -> int:
    """Length in columns, ignoring escape sequences."""
    total = 0
    index = 0
    while index < len(text):
        if text[index] == "\x1b":
            end = text.find("m", index)
            if end == -1:
                break
            index = end + 1
            continue
        total += 1
        index += 1
    return total


def _pad(text: str, width: int, *, colour: bool) -> str:
    del colour
    return text + " " * max(0, width - _visible(text))


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _elide_left(text: str, width: int) -> str:
    """Truncate a path from the left: the tail identifies it, the root does not."""
    if width <= 0:
        return ""
    return text if len(text) <= width else "…" + text[-(width - 1) :]


__all__ = ["MAX_SUGGESTIONS", "MIN_HEIGHT", "MIN_WIDTH", "SessionState", "render_session"]
