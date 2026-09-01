"""The framed TUI: output pane, telemetry sidebar, pinned input, own scrollback.

Opt-in. `/tui` or `SHAMSU_TUI=1`; without it the classic stream mode runs
exactly as before, and that fallback is not a degraded path - it is what a
pipe, a CI runner and a console with no screen buffer still need.

WHY A FRAME AT ALL, when a pinned prompt already exists

`live_console.py` pins an input line under a scrolling log, and that was the
right first step: it fixed the no-echo keystroke reader. But it is still a log.
The sidebar has nowhere to live, so the telemetry is crammed onto two rows at
the bottom and gives up cells as the terminal narrows; output and input share
one scroll region, so nothing can be laid out beside anything else.

The objection to a frame was that it costs the terminal's own scrollback. That
objection is answered by every full-screen tool that has ever shipped: Neovim
and lazygit take the alternate screen and implement scrolling INSIDE the
application. So does this. `LogPane` is the scrollback - bounded, wrapped, and
scrollable with the wheel, PageUp/PageDown, and Home/End - and it does not move
when the sidebar or the input box repaint.

THE THREE THINGS THAT ARE EASY TO GET WRONG

**Follow-tail.** New output must not yank you back to the bottom while you are
reading something forty rounds up. The pane follows the tail only while it is
ALREADY at the tail; scroll up once and it stays where you put it until you
scroll back down to the end, which re-arms it. This is the single detail that
decides whether the scrollback is usable during a live turn.

**The approval prompt.** Two prompt_toolkit `Application`s cannot run at once,
and the approval prompt is one - so inside a frame it cannot simply be called.
`run_in_terminal` is the supported answer: it suspends the frame, hands the
real console back, runs the question exactly as it runs today, and restores.
Tools run on a worker thread, so the bridge in `repl.py` has to cross back to
the app's loop and wait.

**Mouse capture costs text selection.** With `mouse_support` on, the terminal
stops handing click-drag to its own selection; most terminals still allow it
with Shift held, but not all. So it is bound to F2 and can be turned off
without leaving the TUI.
"""
from __future__ import annotations

import contextlib
import os
import re
import time
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

from prompt_toolkit.formatted_text import ANSI, to_formatted_text

#: How much scrollback the pane keeps, in LOGICAL lines (before wrapping).
#: A 24-round turn over big diffs will happily produce tens of thousands; the
#: cap is what stops a long session becoming a memory leak you find at 3am.
MAX_SCROLLBACK_LINES = 5000

#: Rows moved per wheel notch. Three is what most terminals send per notch and
#: what every scrolling application uses.
WHEEL_ROWS = 3

#: How tall the input box may grow before it scrolls internally. Eight lines
#: holds a pasted traceback or a short spec without swallowing the log.
INPUT_MAX_ROWS = 8
VOICE_MIN_RECORD_SECONDS = 0.75

#: The sidebar's fixed width. Below `MIN_WIDTH_FOR_SIDEBAR` it is dropped
#: entirely and the telemetry falls back to the bottom toolbar, because a
#: 28-column pane out of 70 leaves the log unreadable.
SIDEBAR_WIDTH = 30

#: One glyph, then the gap that separates the icon column from the text
#: column. Every kind of line - a prompt, an answer, a tool row, an activity
#: line - puts its mark in column 0 and its text in column 3, so the marks
#: form a column of their own and no text ever lands in it. Widening this
#: widens the gap everywhere at once.
#:
#: `turn_render.ICON_COLUMN` is the same number on the renderer's side, and
#: `test_the_icon_column_is_the_same_width_on_both_sides` holds them equal.
#: Rich pads what it renders to
#: the full console width, so a console given the PANE's width produces lines
#: that are already full before the gutter is prepended - and each one then
#: wraps, putting a near-empty continuation row under every line of every
#: answer. The console is given `content_width()` for that reason.
#: `test_every_gutter_is_the_width_the_console_reserves` keeps the two in step.
GUTTER_WIDTH = 3
MIN_WIDTH_FOR_SIDEBAR = 90


#: The three things in the pane, and how to tell them apart at a glance.
#:
#: A log is what the agent DID, an answer is what it SAID, and a prompt is what
#: a human asked for - three different kinds of thing that all arrived as
#: undifferentiated grey text, so a scrollback of a long session read as one
#: undifferentiated wall.
#:
#: Each carries a gutter mark as well as a colour. Colour alone is not a
#: distinction: terminal palettes vary, some are unreadable, and some people
#: cannot see the difference between the green and the amber. The mark is in
#: the same column on every row, so the shape of the conversation is legible in
#: a screenshot with the colour stripped out.
KIND_LOG = "log"
KIND_ANSWER = "answer"
KIND_NOTICE = "notice"
KIND_APPROVAL = "approval"

#: `surface -> (gutter, gutter style, text style)`. A prompt is coloured by
#: WHERE IT CAME FROM: the same session takes work from this terminal, the web
#: portal and Telegram, and "who asked for this?" is otherwise unanswerable
#: from the log.
PROMPT_SURFACES: dict[str, tuple[str, str, str]] = {
    "cli": ("›  ", "class:kind.cli.mark", "class:kind.cli"),
    "web": ("◈  ", "class:kind.web.mark", "class:kind.web"),
    "telegram": ("✈  ", "class:kind.telegram.mark", "class:kind.telegram"),
}
UNKNOWN_SURFACE = ("?  ", "class:kind.other.mark", "class:kind.other")

#: The non-prompt kinds. `log` is deliberately blank - it is the default, it
#: arrives already coloured by rich, and marking every action row would put a
#: gutter on 95% of the pane.
LINE_KINDS: dict[str, tuple[str, str, str]] = {
    KIND_LOG: ("", "", ""),
    KIND_ANSWER: ("◆  ", "class:kind.answer.mark", "class:kind.answer"),
    KIND_NOTICE: ("·  ", "class:kind.notice", "class:kind.notice"),
    KIND_APPROVAL: ("┃  ", "class:kind.approval.mark", "class:kind.approval"),
}

#: What a kind's SECOND and later lines carry. An answer is a block - twenty
#: lines of it were twenty ◆, which stops reading as a mark and starts reading
#: as noise. The diamond now opens the answer and a thin rule carries it, so
#: the block's extent is still visible with the colour stripped out (which was
#: the reason for marking every row in the first place) without the repetition.
#: A kind absent here repeats its own mark, which is right for the approval
#: bar and costs nothing for the single-line kinds.
CONTINUATION_MARKS: dict[str, str] = {
    KIND_ANSWER: "│  ",
}


def prompt_decoration(surface: str) -> tuple[str, str, str]:
    """How a prompt from `surface` is drawn."""
    return PROMPT_SURFACES.get((surface or "").strip().lower(), UNKNOWN_SURFACE)


def tui_enabled() -> bool:
    """Whether the framed TUI is on by default for this process."""
    return os.environ.get("SHAMSU_TUI", "").strip() == "1"


# -- fragments --------------------------------------------------------------


def coalesce(fragments: Iterable[tuple]) -> list[tuple[str, str]]:
    """Merge neighbouring fragments that share a style.

    `ANSI()` emits ONE FRAGMENT PER CHARACTER - a 5,000 line scrollback would
    be millions of two-tuples. Runs of one style collapse to one fragment,
    which is what the text was to begin with.
    """
    out: list[tuple[str, str]] = []
    for fragment in fragments:
        style, text = fragment[0], fragment[1]
        if out and out[-1][0] == style:
            out[-1] = (style, out[-1][1] + text)
        else:
            out.append((style, text))
    return out


#: Private-mode CSI sequences (`ESC [ ? 25 l` - hide cursor, and friends).
#:
#: These MUST go before `ANSI()` sees them: it does not recognise the `?`
#: form and renders it as the literal text `25l`. Live 2026-08-23 that was
#: visible as garbage in the pane.
_PRIVATE_CSI = re.compile(r"\x1b\[\?[0-9;]*[a-zA-Z]")

#: Anything else that would reach the real terminal as a control character.
#: `ANSI()` consumes the SGR codes it understands and passes the rest through
#: as TEXT - and prompt_toolkit writes fragment text verbatim, so a stray
#: `\x1b[1A` in the pane physically moves the cursor and scrambles the frame,
#: sidebar and all. Nothing that steers a cursor may be stored.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def parse_ansi(text: str) -> list[tuple[str, str]]:
    """ANSI escapes to styled fragments, coalesced and made safe to store.

    `\\r` is deliberately KEPT here - `LogPane.write` needs to see it to know a
    line is being overwritten. Everything else that could steer a cursor is
    dropped.
    """
    cleaned = _PRIVATE_CSI.sub("", text)
    fragments = to_formatted_text(ANSI(cleaned))
    safe: list[tuple[str, str]] = []
    for style, body in ((f[0], f[1]) for f in fragments):
        stripped = _CONTROL_CHARS.sub("", body)
        if stripped:
            safe.append((style, stripped))
    return coalesce(safe)


def parse_ansi_lines(
    text: str,
) -> list[list[tuple[list[tuple[str, str]], bool]]]:
    """Parse a chunk into lines, and within each line into carriage-return runs.

    Returns one entry per NEWLINE-separated line; each entry is a list of
    `(fragments, overwrite)` pieces, where `overwrite` says this piece followed
    a `\\r` and therefore replaces what came before it on that line.
    """
    lines: list[list[tuple[list[tuple[str, str]], bool]]] = [[]]
    for style, body in parse_ansi(text):
        for line_index, line_part in enumerate(body.split("\n")):
            if line_index:
                lines.append([])
            for run_index, run in enumerate(line_part.split("\r")):
                lines[-1].append(([(style, run)] if run else [], run_index > 0))
    return lines


def split_lines(fragments: Iterable[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Break styled fragments at newlines, keeping each line's styling."""
    lines: list[list[tuple[str, str]]] = [[]]
    for style, text in fragments:
        parts = text.split("\n")
        for index, part in enumerate(parts):
            if index:
                lines.append([])
            if part:
                lines[-1].append((style, part))
    return lines


def fragment_width(line: Iterable[tuple[str, str]]) -> int:
    return sum(len(text) for _style, text in line)


def wrap_line(
    line: list[tuple[str, str]], width: int
) -> list[list[tuple[str, str]]]:
    """Hard-wrap one styled line to `width`, splitting fragments as needed.

    Wrapping happens HERE rather than being left to the Window, because the
    pane scrolls by counting rows: if the terminal wrapped a line into three
    rows behind our back, every scroll offset below it would be wrong.
    """
    if width <= 0:
        return [line]
    if fragment_width(line) <= width:
        return [line]

    rows: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    used = 0
    for style, text in line:
        while text:
            room = width - used
            if len(text) <= room:
                current.append((style, text))
                used += len(text)
                break
            current.append((style, text[:room]))
            rows.append(current)
            current, used = [], 0
            text = text[room:]
    rows.append(current)
    return rows


class _Decorated:
    """Sets the pane's line decoration for the length of a `with` block."""

    def __init__(
        self,
        pane: Any,
        gutter: str,
        gutter_style: str,
        style: str,
        continuation: str = "",
    ) -> None:
        self._pane = pane
        self._new = (gutter, gutter_style, style)
        self._continuation = continuation
        self._old = ("", "", "")
        self._old_continuation = ""

    def __enter__(self) -> Any:
        pane = self._pane
        self._old = (pane._gutter, pane._gutter_style, pane._style)
        self._old_continuation = pane._continuation
        # Close whatever half-line is open first, or it inherits the new mark.
        if pane._partial:
            pane._append_logical(pane._partial)
            pane._partial = []
        pane._gutter, pane._gutter_style, pane._style = self._new
        pane._continuation = self._continuation
        # A fresh block always opens with its own mark, never with the rule
        # left over from the last one.
        pane._opened = False
        return pane

    def __exit__(self, *_exc: object) -> None:
        pane = self._pane
        if pane._partial:
            pane._append_logical(pane._partial)
            pane._partial = []
        pane._gutter, pane._gutter_style, pane._style = self._old
        pane._continuation = self._old_continuation
        pane._opened = False


class LogPane:
    """The scrollback. Bounded, wrapped, and scrolled from inside the app.

    Holds LOGICAL lines and the VISUAL rows they wrap to at the current width,
    kept in step so a scroll offset always means the same thing. Re-wrapping
    only happens on a resize; an append wraps just the new line.
    """

    def __init__(self, max_lines: int = MAX_SCROLLBACK_LINES) -> None:
        self.max_lines = max(1, max_lines)
        self.width = 0
        #: Whether new output scrolls the view. True only while the view is
        #: already at the bottom - see the module docstring.
        self.follow = True
        self.offset = 0
        self._logical: deque[list[tuple[str, str]]] = deque(maxlen=self.max_lines)
        self._rowcount: deque[int] = deque(maxlen=self.max_lines)
        self._rows: deque[list[tuple[str, str]]] = deque()
        #: The tail of a chunk that did not end in a newline. Rich writes in
        #: pieces that do not respect line boundaries.
        self._partial: list[tuple[str, str]] = []
        #: How lines written right now are marked. See `decorate_as`.
        self._gutter = ""
        self._gutter_style = ""
        self._style = ""
        #: What the CURRENT block's second and later lines carry, and whether
        #: its first line has been written yet.
        self._continuation = ""
        self._opened = False

    # -- intake ------------------------------------------------------------

    def write(self, text: str) -> None:
        """Append output. Accepts ANSI; partial lines are held until closed.

        A carriage return REPLACES the line being built, as it does on a real
        terminal. Everything that draws a spinner or a progress bar - rich's
        `console.status`, npm, pip, pytest - redraws by emitting `\\r` and the
        line again. Treating that as ordinary text appends a line per frame:
        live 2026-08-23 a single status spinner filled the pane with thousands
        of `Working...` rows and pushed everything else off the screen.
        """
        if not text:
            return
        for index, chunk in enumerate(parse_ansi_lines(text)):
            if index:
                self._append_logical(self._partial)
                self._partial = []
            for piece, overwrite in chunk:
                self._partial = piece if overwrite else self._partial + piece
        if self.follow:
            self.to_end()

    def decorate_as(
        self, gutter: str, gutter_style: str, style: str, continuation: str = ""
    ) -> Any:
        """Mark every line written inside this block as one KIND.

        A context manager rather than an argument on `write`, because the
        writes it has to cover are `console.print` calls made by code that has
        never heard of a pane - the whole point of redirecting the console was
        that a hundred call sites did not have to learn about the frame.
        """
        return _Decorated(self, gutter, gutter_style, style, continuation)

    def _decorate(self, line: list[tuple[str, str]]) -> list[tuple[str, str]]:
        if self._style:
            line = [(style or self._style, text) for style, text in line]
        if self._gutter:
            mark = self._gutter if not self._opened else (self._continuation or self._gutter)
            self._opened = True
            line = [(self._gutter_style, mark), *line]
        return line

    def _append_logical(self, line: list[tuple[str, str]]) -> None:
        line = self._decorate(line)
        evicted = self._rowcount[0] if len(self._logical) == self.max_lines else 0
        self._logical.append(line)
        rows = wrap_line(line, self.width) if self.width else [line]
        self._rowcount.append(len(rows))
        self._rows.extend(rows)
        for _ in range(evicted):
            if self._rows:
                self._rows.popleft()
            self.offset = max(0, self.offset - 1)

    def set_width(self, width: int) -> None:
        """A resize re-wraps everything; nothing else does."""
        width = max(1, width)
        if width == self.width:
            return
        self.width = width
        self._rows.clear()
        self._rowcount.clear()
        for line in self._logical:
            rows = wrap_line(line, width)
            self._rowcount.append(len(rows))
            self._rows.extend(rows)
        self.to_end() if self.follow else self._clamp(0)

    def write_as(self, text: str, kind: str) -> None:
        """Write a block already known to be one kind - a prompt, an answer."""
        gutter, gutter_style, style = LINE_KINDS.get(kind, LINE_KINDS[KIND_LOG])
        with self.decorate_as(
            gutter, gutter_style, style, CONTINUATION_MARKS.get(kind, "")
        ):
            self.write(text if text.endswith(chr(10)) else text + chr(10))

    def write_prompt(self, text: str, surface: str) -> None:
        """Write a human's request, coloured by where it came from."""
        gutter, gutter_style, style = prompt_decoration(surface)
        with self.decorate_as(gutter, gutter_style, style):
            self.write(text if text.endswith(chr(10)) else text + chr(10))

    def separate(self) -> None:
        """One blank row, unless the pane already ends in one.

        Idempotent because the callers are: a turn that printed nothing but
        its answer must not open with two empty rows, and a turn interrupted
        and resumed calls this twice.
        """
        if self._partial:
            self._append_logical(self._partial)
            self._partial = []
        if not self._logical:
            return
        last = self._logical[-1]
        if not "".join(text for _style, text in last).strip():
            return
        self._append_logical([])

    def clear(self) -> None:
        self._logical.clear()
        self._rowcount.clear()
        self._rows.clear()
        self._partial = []
        self.offset = 0
        self.follow = True

    # -- scrolling ---------------------------------------------------------

    @property
    def total_rows(self) -> int:
        return len(self._rows) + (1 if self._partial else 0)

    def max_offset(self, height: int) -> int:
        return max(0, self.total_rows - max(1, height))

    def scroll(self, rows: int, height: int) -> None:
        """Negative scrolls up (towards older output), positive down.

        Reaching the bottom re-arms follow-tail; moving off it disarms.

        The delta is applied to where the view WOULD be at this height, not to
        whatever `offset` happens to hold. Those differ: while following, the
        offset was last set for the height of the previous paint, and a window
        that has since grown leaves it past this height's limit - so a scroll
        up would clamp back down to the bottom and re-arm follow instead of
        moving. That is a wheel that does nothing, which is how this was found.
        """
        height = max(1, height)
        self._last_height = height
        limit = self.max_offset(height)
        base = limit if self.follow else min(self.offset, limit)
        self.offset = max(0, min(limit, base + rows))
        self.follow = self.offset >= limit

    def page(self, direction: int, height: int) -> None:
        self.scroll(direction * max(1, height - 1), height)

    def to_end(self) -> None:
        self.offset = self.max_offset(self._last_height)
        self.follow = True

    def to_start(self) -> None:
        self.offset = 0
        self.follow = False

    def _clamp(self, height: int) -> None:
        self.offset = max(0, min(self.max_offset(height or 1), self.offset))

    #: Remembered so `to_end` works from a key binding, which does not know
    #: how tall the window is.
    _last_height = 1

    # -- output ------------------------------------------------------------

    def visible(
        self, height: int, mouse_handler: Callable[[Any], Any] | None = None
    ) -> list[tuple]:
        """The rows currently on screen, as one formatted-text list."""
        height = max(1, height)
        self._last_height = height
        if self.follow:
            self.offset = self.max_offset(height)
        else:
            self._clamp(height)

        rows = list(self._rows)
        if self._partial:
            rows.append(self._partial)
        window = rows[self.offset : self.offset + height]

        out: list[tuple] = []
        for index, row in enumerate(window):
            if index:
                out.append(("", "\n"))
            for style, text in row:
                out.append((style, text, mouse_handler) if mouse_handler else (style, text))
        if not out and mouse_handler:
            # An empty pane still has to accept the wheel, or scrolling a
            # fresh turn does nothing until the first line lands.
            out.append(("", " ", mouse_handler))
        return out

    def plain(self, height: int) -> str:
        """The visible rows as text. For tests."""
        return "".join(
            text for fragment in self.visible(height) for text in (fragment[1],)
        )

    def scroll_position(self) -> str:
        """`top`, `bot`, or a percentage - the ruler Neovim puts in the corner."""
        if self.follow or self.offset >= self.max_offset(self._last_height):
            return "bot"
        if self.offset == 0:
            return "top"
        limit = self.max_offset(self._last_height) or 1
        return f"{round(self.offset / limit * 100)}%"


# -- the sidebar ------------------------------------------------------------


def render_sidebar(
    telemetry: Any, width: int = SIDEBAR_WIDTH, services: Any = None
) -> list[tuple[str, str]]:
    """`TurnTelemetry` laid out vertically, which is what it was always for.

    The bottom toolbar had to give up cells as the terminal narrowed - the file
    list first, then the token counts, then the bar. A column has room for all
    of it at once, which is the whole argument for the split, and room for the
    numbers that never fitted anywhere: where the turn's time actually went,
    how much of the work failed, and whether one tool is being called over and
    over. A turn that ran 22 minutes and changed nothing reads identically to a
    productive one until those are on screen.
    """
    inner = width - 1
    rows: list[tuple[str, str]] = []

    def line(style: str, text: str) -> None:
        rows.append((style, text[:inner].ljust(inner) + "\n"))

    def blank() -> None:
        line("", "")

    def heading(text: str) -> None:
        line("class:tui.heading", " " + text.upper())

    def stat(key: str, value: str, style: str = "class:tui.val") -> None:
        """` key        value`, with the values in one column.

        The key column is exactly wide enough for the longest label
        (`contracts`) and no wider, because the value column is what runs out:
        `contract_status x8` is 18 characters and it is the most useful string
        on the panel - truncating it to `contract_status x` throws away the
        count, which is the entire point of the row.
        """
        label = f" {key}".ljust(KEY_WIDTH)[:KEY_WIDTH]
        rows.append(("class:tui.key", label))
        room = inner - KEY_WIDTH
        rows.append((style, value[:room].ljust(room) + "\n"))

    active = bool(getattr(telemetry, "active", False))

    line("class:tui.title", " SHAMSU ")
    line("class:tui.rule", " " + "─" * (inner - 2))

    status = str(getattr(telemetry, "status_text", "") or "")
    if active:
        rows.append(("class:tui.spin", f" {telemetry.spinner()} "))
        rows.append(("class:tb.head", status[: inner - 3].ljust(inner - 3) + "\n"))
        line("class:tb.dim", f"   {telemetry.elapsed}")
    else:
        verdict = str(getattr(telemetry, "verdict", "") or "")
        if verdict:
            good = verdict.lower() in {"done", "success", "ok", "complete"}
            line(
                "class:tb.ok" if good else "class:tb.alarm",
                f" {'✓' if good else '✗'} {verdict}",
            )
        else:
            line("class:tb.dim", " idle")
        line("class:tb.dim", f"   {telemetry.elapsed}" if telemetry.elapsed else "")
    blank()

    # -- progress ---------------------------------------------------------
    heading("Progress")
    max_rounds = int(getattr(telemetry, "max_rounds", 0) or 0)
    if max_rounds:
        used = int(getattr(telemetry, "round", 0) or 0)
        stat("rounds", f"{used}/{max_rounds}", _budget_style(used, max_rounds))
        line(_budget_style(used, max_rounds), "  " + _bar(used / max_rounds, inner - 4))
    else:
        stat("rounds", "-", "class:tb.dim")

    pct = getattr(telemetry, "ctx_pct", None)
    if pct is None:
        stat("context", "not measured", "class:tb.dim")
    else:
        stat("context", f"{pct}%", telemetry.context_style())
        line(telemetry.context_style(), "  " + _bar(pct / 100, inner - 4))
        used_t, total_t = getattr(telemetry, "ctx_used", 0), getattr(telemetry, "ctx_total", 0)
        if used_t and total_t:
            line("class:tb.dim", f"  {_short(used_t)} / {_short(total_t)} tokens")
    blank()

    # -- where the time went ----------------------------------------------
    heading("This turn")
    model_calls = int(getattr(telemetry, "model_calls", 0) or 0)
    tool_calls = int(getattr(telemetry, "tool_calls", 0) or 0)
    stat("model", f"{model_calls} · {_clock(getattr(telemetry, 'model_seconds', 0))}")
    speed = float(getattr(telemetry, "tokens_per_second", 0) or 0)
    if speed > 0:
        stat("speed", f"{speed:.0f} tok/s", _speed_style(speed))
    stat("tools", f"{tool_calls} · {_clock(getattr(telemetry, 'tool_seconds', 0))}")

    failures = int(getattr(telemetry, "tool_failures", 0) or 0)
    stat(
        "failed",
        str(failures),
        "class:tb.alarm" if failures else "class:tb.dim",
    )

    busiest, count = _busiest(telemetry)
    if busiest:
        stat(
            "repeated",
            f"{busiest} x{count}",
            "class:tb.warn" if count >= THRASH_AT else "class:tb.dim",
        )
    blank()

    # -- what changed ------------------------------------------------------
    files = list(getattr(telemetry, "files", []) or [])
    heading(f"Files ({len(files)})" if files else "Files")
    if not files:
        line("class:tb.dim", "   none yet")
    for path in files[:MAX_SIDEBAR_FILES]:
        line("class:tui.file", f"   {path}")
    if len(files) > MAX_SIDEBAR_FILES:
        line("class:tb.dim", f"   +{len(files) - MAX_SIDEBAR_FILES} more")
    blank()

    # -- what is outstanding ----------------------------------------------
    heading("Outstanding")
    contracts = int(getattr(telemetry, "contracts", 0) or 0)
    stat("contracts", str(contracts), "class:tb.warn" if contracts else "class:tb.dim")
    feedback = _depth(getattr(telemetry, "feedback_depth", None))
    tasks = _depth(getattr(telemetry, "tasks_depth", None))
    stat("feedback", str(feedback), "class:tui.val" if feedback else "class:tb.dim")
    stat("queued", str(tasks), "class:tui.val" if tasks else "class:tb.dim")

    # -- what is running around this session -------------------------------
    if services is not None:
        blank()
        heading("Services")
        for label, (value, style) in services.read().items():
            stat(label, value, style)
    return rows


#: Width of the sidebar's label column. Sized to `contracts`, the longest
#: label, so the value column keeps every character it can.
KEY_WIDTH = 11

#: How long a services reading is reused before it is taken again.
#:
#: The toolbar repaints five times a second and these answers come from a
#: SQLite lease table and a state DB - asking every frame would be a database
#: query per 200ms for a number that changes when you type a command. Five
#: seconds is far below "I turned the bot on and it still says off".
SERVICES_TTL_SECONDS = 5.0


class Services:
    """What is running around this session, sampled rather than polled.

    Deliberately outside `TurnTelemetry`: everything in there is fed by the
    turn stream and belongs to one turn, whereas these outlive turns and come
    from durable state that other PROCESSES own - the bot's machine lease, the
    web portal's manager, the model tier. Mixing them would make the telemetry
    reach out to a database, which is exactly what a renderer must not do.
    """

    def __init__(self, workspace: Any = None) -> None:
        self.workspace = workspace
        self._taken = 0.0
        self._values: dict[str, tuple[str, str]] = {}

    def read(self) -> dict[str, tuple[str, str]]:
        """`{label: (value, style)}`, at most one real reading per TTL."""
        now = time.monotonic()
        if self._values and now - self._taken < SERVICES_TTL_SECONDS:
            return self._values
        self._taken = now
        self._values = {
            "model": self._model(),
            "vram": self._vram(),
            "ram": self._ram(),
            "telegram": self._telegram(),
            "web": self._web(),
        }
        return self._values

    def _vram(self) -> tuple[str, str]:
        """What Ollama is actually holding, from `/api/ps`.

        The most consequential number on this machine and the one nobody could
        see: Ollama reserves the KV cache for the WHOLE context window up
        front, so a window that does not fit spills to the CPU and the same
        turn takes six times as long. "Loaded, 6.2G" and "not loaded" are
        different worlds and the log said neither.
        """
        try:
            import httpx

            from shamsu.llm.manager import OLLAMA_BASE_URL

            response = httpx.get(f"{OLLAMA_BASE_URL}/api/ps", timeout=1.5)
            models = response.json().get("models") or []
        except Exception:  # noqa: BLE001
            return ("unknown", "class:tb.dim")
        if not models:
            return ("nothing loaded", "class:tb.dim")
        entry = models[0]
        vram = int(entry.get("size_vram") or 0)
        window = int(entry.get("context_length") or 0)
        if not vram:
            return ("on cpu", "class:tb.alarm")
        label = f"{vram / 1e9:.1f}G"
        if window:
            label += f" · {window // 1024}k"
        return (label, "class:tb.ok")

    def _ram(self) -> tuple[str, str]:
        """This process's resident set. SHAMSU's own footprint, not Ollama's."""
        try:
            import psutil

            rss = psutil.Process().memory_info().rss
        except Exception:  # noqa: BLE001
            return ("unknown", "class:tb.dim")
        return (f"{rss / 1e6:.0f} MB", "class:tui.val")

    def _model(self) -> tuple[str, str]:
        try:
            from shamsu.runtime.models import model_for_role

            name = str(model_for_role("coder") or "")
        except Exception:  # noqa: BLE001
            return ("unknown", "class:tb.dim")
        # The tag is the useful half; the quantisation suffix is not worth a
        # column that has to fit `qwen2.5-coder:7b-instruct-q4_K_M`.
        return (name.split("-q4")[0][:18] or "unknown", "class:tui.val")

    def _telegram(self) -> tuple[str, str]:
        try:
            from shamsu.integrations.telegram.service import poller_status

            status = poller_status(self.workspace)
        except Exception:  # noqa: BLE001
            return ("unknown", "class:tb.dim")
        if status.get("running"):
            return ("running", "class:tb.ok")
        if status.get("configured"):
            return ("configured", "class:tb.warn")
        return ("not set up", "class:tb.dim")

    def _web(self) -> tuple[str, str]:
        try:
            from shamsu.webui.local import _MANAGER

            portal = _MANAGER.running
        except Exception:  # noqa: BLE001
            return ("unknown", "class:tb.dim")
        if portal is None:
            return ("off", "class:tb.dim")
        return (str(getattr(portal, "base_url", "running"))[-18:], "class:tb.ok")

#: A tool called this many times in one turn is thrashing, not working.
THRASH_AT = 3

#: Files named in the sidebar before it switches to a count.
MAX_SIDEBAR_FILES = 6


def _busiest(telemetry: Any) -> tuple[str, int]:
    try:
        name, count = telemetry.busiest_tool()
    except Exception:  # noqa: BLE001
        return ("", 0)
    return (name, count) if count > 1 else ("", 0)


#: Generation speed below which the model has almost certainly spilled off the
#: GPU. A 7-9B at q4 on this hardware runs in the tens of tokens a second on
#: the card and in low single digits on the CPU - the gap is not subtle, and
#: the whole point of showing the number is to catch the fall.
SLOW_TOKENS_PER_SECOND = 8.0
HEALTHY_TOKENS_PER_SECOND = 20.0


def _speed_style(speed: float) -> str:
    if speed < SLOW_TOKENS_PER_SECOND:
        return "class:tb.alarm"
    if speed < HEALTHY_TOKENS_PER_SECOND:
        return "class:tb.warn"
    return "class:tb.ok"


def _budget_style(used: int, total: int) -> str:
    """Rounds run out too, and running out is how a turn ends with nothing."""
    if not total:
        return "class:tb.dim"
    ratio = used / total
    if ratio >= 0.8:
        return "class:tb.alarm"
    if ratio >= 0.6:
        return "class:tb.warn"
    return "class:tb.ok"


def _bar(ratio: float, width: int) -> str:
    width = max(4, width)
    filled = max(0, min(width, round(max(0.0, min(1.0, ratio)) * width)))
    return "█" * filled + "░" * (width - filled)


def _clock(seconds: Any) -> str:
    """`8m12s`, `44s`, `-`."""
    try:
        value = float(seconds or 0)
    except Exception:  # noqa: BLE001
        return "-"
    if value <= 0:
        return "-"
    if value < 60:
        return f"{value:.0f}s"
    return f"{int(value // 60)}m{int(value % 60):02d}s"


def _short(count: int) -> str:
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}k".replace(".0k", "k")


def _depth(source: Any) -> int:
    try:
        return int(source())
    except Exception:  # noqa: BLE001
        return 0


# -- the bridge rich writes through ----------------------------------------


class PaneWriter:
    """A file object that puts everything written to it into the pane.

    The REPL hands one `Console` down through every route, so pointing that
    console's `file` at this for the length of a turn captures the whole turn -
    panels, markdown, diffs, the lot - without touching a hundred call sites.
    `isatty()` is True so rich keeps emitting colour; `LogPane` parses it back.
    """

    def __init__(self, pane: LogPane, invalidate: Callable[[], None]) -> None:
        self._pane = pane
        self._invalidate = invalidate

    def write(self, text: str) -> int:
        self._pane.write(text)
        self._invalidate()
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return "utf-8"


# -- the application --------------------------------------------------------

#: One palette, used by the sidebar and the statusline.
#:
#: The colours are load-bearing, not decoration: green/amber/red mean the same
#: thing everywhere - fine, getting tight, out of room - and they are the same
#: thresholds `CliTurnRenderer` uses on the context meter, so one number cannot
#: read as two different warnings on two parts of the screen. Everything that
#: is merely a label is grey, so the coloured things are the ones worth looking
#: at.
TUI_STYLE = {
    # chrome
    "tui.title": "bold #ffffff bg:#005f87",
    "tui.heading": "bold #5fafd7",
    "tui.rule": "#3a3a3a",
    "tui.sidebar": "bg:#1c1c1c",
    "tui.spin": "bold #5fafd7",
    "tui.caret": "bold #5fd75f",
    "tui.input": "bg:#121212",
    # -- the three kinds of line, one colour family each -------------------
    # A prompt is coloured by WHERE IT CAME FROM. Logs keep whatever rich sent
    # them as, so they stay the neutral background the other two stand out
    # against - which only works because the other two are the minority.
    "kind.cli": "bold #5fd75f",
    "kind.cli.mark": "bold #5fd75f",
    "kind.web": "bold #5fafd7",
    "kind.web.mark": "bold #5fafd7",
    "kind.telegram": "bold #d787d7",
    "kind.telegram.mark": "bold #d787d7",
    "kind.other": "bold #d7af5f",
    "kind.other.mark": "bold #d7af5f",
    "kind.answer": "#ffffff",
    "kind.answer.mark": "bold #5fafd7",
    "kind.notice": "#6c6c6c",
    "kind.approval": "bold #ffd75f",
    "kind.approval.mark": "bold #ff5f5f",
    # sidebar rows
    "tui.key": "#8a8a8a",
    "tui.val": "bold #e4e4e4",
    "tui.file": "#87d7af",
    # statusline
    "tui.status": "bg:#005f87 #ffffff",
    "tui.status.key": "bg:#262626 #9e9e9e",
    "tui.status.busy": "bg:#875f00 bold #ffffff",
    "tui.status.idle": "bg:#262626 #87d787",
    "tui.status.ask": "bg:#870000 bold #ffffff",
    # shared with the bottom toolbar, so the two displays agree
    "tb.head": "bold #e4e4e4",
    "tb.label": "#8a8a8a",
    "tb.value": "#d0d0d0",
    "tb.dim": "#6c6c6c",
    "tb.sep": "#3a3a3a",
    "tb.ok": "#5fd75f",
    "tb.warn": "#d7af5f",
    "tb.alarm": "bold #ff5f5f",
}


def approval_lines(record: Any, *, offer_remember: bool = False) -> str:
    """The approval card, as pane text.

    Deliberately the same fields the terminal panel shows. Two renderers for
    one question is how a single `run_command` came out as a thin card from
    `control/console.py` and a fat one from `safety/approval.py`, sometimes
    both, for the same action.
    """
    rows = ["", "APPROVAL REQUIRED"]
    action = str(getattr(record, "action_type", "") or "")
    if action:
        rows.append(f"  action : {action}")
    risk = str(getattr(record, "risk_level", "") or "")
    if risk:
        rows.append(f"  risk   : {risk}")
    rows.append(f"  what   : {getattr(record, 'description', '') or 'an action'}")
    preview = str(getattr(record, "preview", "") or "").strip()
    if preview:
        rows.extend(f"  | {row}" for row in preview[:600].splitlines())
    rows.append(
        "  [y] allow   [a] always allow   [n] deny"
        if offer_remember
        else "  [y] allow   [n] deny"
    )
    rows.append("")
    return chr(10).join(rows)


class TuiApp:
    """Output pane, telemetry sidebar and input box in one frame.

    The turn runs as a task beside `run_async()`; when it finishes the caller
    exits the app. Nothing here knows what a turn IS - it takes text from the
    box and hands it to `on_submit`, and it paints whatever reaches the pane.
    """

    def __init__(
        self,
        *,
        telemetry: Any,
        on_submit: Callable[[str], None],
        history: Any = None,
        completer: Any = None,
        prompt_label: Callable[[], str] = lambda: "shamsu> ",
        on_interrupt: Callable[[], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        workspace: Any = None,
        mouse_support: bool = True,
        voice_service_factory: Callable[[], Any] | None = None,
        voice_recorder_factory: Callable[[], Any] | None = None,
        voice_output_factory: Callable[[], Any] | None = None,
    ) -> None:
        from prompt_toolkit.application import Application
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
        from prompt_toolkit.layout.containers import (
            ConditionalContainer,
            Float,
            FloatContainer,
        )
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.layout.menus import CompletionsMenu
        from prompt_toolkit.layout.processors import BeforeInput
        from prompt_toolkit.styles import Style

        self.telemetry = telemetry
        self.services = Services(workspace)
        self.pane = LogPane()
        self._on_submit = on_submit
        self._on_interrupt = on_interrupt
        self._on_exit = on_exit
        self._prompt_label = prompt_label
        self._mouse = mouse_support
        self.app: Any = None
        self._voice_service_factory = voice_service_factory
        self._voice_recorder_factory = voice_recorder_factory
        self._voice_output_factory = voice_output_factory
        self._voice_service: Any = None
        self._voice_recorder: Any = None
        self._voice_output: Any = None
        self._voice_state = ""
        self._voice_started = 0.0
        #: Set only by `_voice_transcribed`, cleared by anything typed and by
        #: the reply it belongs to. A spoken answer is the answer to something
        #: SAID here - not to a typed prompt, and not to a turn that a phone
        #: or a browser started and this terminal is merely watching.
        self._voice_reply_armed = False
        #: Whether a reply is being spoken RIGHT NOW. Set on the voice thread
        #: and read while painting, so the status bar can offer the key that
        #: stops it only while there is something to stop.
        self._speaking = False
        #: The rich console whose output is redirected into this pane, held so
        #: its width can follow a resize. Set by `_start_frame`.
        self._output_console: Any = None

        #: `None`, or the question this frame is currently holding. While it
        #: is set the input box stops taking text and single keys answer
        #: instead - see `open_approval` for why the frame asks at all.
        self._approval: dict[str, Any] | None = None

        # Multiline on purpose. A one-line box is fine for "fix the tests" and
        # useless for the thing people actually paste - a PRD, a traceback, a
        # spec with eight bullet points - which is most of what starts a real
        # turn. Enter submits, Alt+Enter opens a new line, and the box grows to
        # `INPUT_MAX_ROWS` before it starts scrolling, so a long prompt is
        # visible while it is being written rather than a single sliding line.
        self.buffer = Buffer(
            history=history,
            completer=completer,
            complete_while_typing=bool(completer),
            # Ghost text from what you have typed before. The idle prompt has
            # had history for as long as it has existed; the frame's box had
            # neither this nor a completion menu, so it felt like a downgrade
            # from the thing it replaced.
            auto_suggest=AutoSuggestFromHistory(),
            multiline=True,
            accept_handler=self._accept,
        )

        output = Window(
            content=FormattedTextControl(
                self._output_fragments, focusable=False, show_cursor=False
            ),
            wrap_lines=False,
            width=Dimension(weight=1),
        )
        sidebar = ConditionalContainer(
            Window(
                content=FormattedTextControl(self._sidebar_fragments, focusable=False),
                width=SIDEBAR_WIDTH,
                style="class:tui.sidebar",
            ),
            filter=Condition(self._sidebar_fits),
        )
        # A `PromptSession` builds the completion menu for you. A hand-made
        # `Layout` does not - so the completer ran, produced completions, and
        # had nowhere to draw them. It needs a float anchored to the cursor,
        # which is the only reason `FloatContainer` is here.
        self.layout = Layout(
            FloatContainer(
                content=HSplit(
                    [
                        VSplit([output, sidebar]),
                        Window(
                            height=1, content=FormattedTextControl(self._statusline)
                        ),
                        Window(
                            height=Dimension(min=1, max=INPUT_MAX_ROWS),
                            content=BufferControl(
                                buffer=self.buffer,
                                input_processors=[
                                    BeforeInput("› ", style="class:tui.caret")
                                ],
                            ),
                            wrap_lines=True,
                            style="class:tui.input",
                        ),
                    ]
                ),
                floats=[
                    Float(
                        xcursor=True,
                        ycursor=True,
                        content=CompletionsMenu(max_height=12, scroll_offset=1),
                    )
                ],
            ),
            focused_element=self.buffer,
        )
        self.app = Application(
            layout=self.layout,
            key_bindings=self._bindings(),
            style=Style.from_dict(TUI_STYLE),
            full_screen=True,
            mouse_support=Condition(lambda: self._mouse),
            refresh_interval=0.2,
        )

    # -- painting ----------------------------------------------------------

    def _sidebar_fits(self) -> bool:
        return self._terminal_width() >= MIN_WIDTH_FOR_SIDEBAR

    def _terminal_width(self) -> int:
        try:
            return self.app.output.get_size().columns
        except Exception:  # noqa: BLE001
            return 100

    def output_width(self) -> int:
        """How wide the pane is, gutter included."""
        width = self._terminal_width()
        return max(20, width - (SIDEBAR_WIDTH if self._sidebar_fits() else 0))

    def content_width(self) -> int:
        """How wide a LINE may be. What the rich console must be set to."""
        return max(10, self.output_width() - GUTTER_WIDTH)

    def _sync_console_width(self) -> None:
        """Keep the redirected console in step with the pane.

        Set once when the frame starts, it went stale the first time anyone
        resized the terminal - and a console wider than the pane wraps every
        answer, which is the same defect arriving by a different route.
        """
        console = self._output_console
        if console is None:
            return
        wanted = self.content_width()
        if console.width != wanted:
            console.width = wanted

    def _output_height(self) -> int:
        try:
            return max(1, self.app.output.get_size().rows - 2)
        except Exception:  # noqa: BLE001
            return 20

    def _output_fragments(self) -> list[tuple]:
        self.pane.set_width(self.output_width())
        self._sync_console_width()
        return self.pane.visible(self._output_height(), self._wheel)

    def _sidebar_fragments(self) -> list[tuple[str, str]]:
        try:
            self.telemetry.tick()
            return render_sidebar(self.telemetry, services=self.services)
        except Exception:  # noqa: BLE001 - a sidebar that raises kills the frame
            return [("class:tb.dim", " telemetry unavailable")]

    def _statusline(self) -> list[tuple[str, str]]:
        """Segments, not one grey strip.

        The left block changes colour with what the session is DOING - amber
        while a turn runs, green while it waits for you - because that is the
        one thing you glance down for, and a uniform bar makes you read it.
        """
        width = self._terminal_width()
        busy = bool(getattr(self.telemetry, "active", False))
        mode = " RUNNING " if busy else " READY "
        mode_style = "class:tui.status.busy" if busy else "class:tui.status.idle"

        label = f" {self._prompt_label()} "
        scroll = f" {self.pane.scroll_position()} "
        keys = " PgUp/PgDn scroll · F2 mouse · ^C stop · ^D close "
        if not self._mouse:
            scroll = f" {self.pane.scroll_position()} · MOUSE OFF "
        if self._voice_state == "recording":
            elapsed = max(0, int(time.monotonic() - self._voice_started))
            mode, mode_style = f" LISTENING {elapsed:02d}s ", "class:tui.status.ask"
            keys = " F5 stop recording "
        elif self._voice_state == "transcribing":
            mode, mode_style = " TRANSCRIBING ", "class:tui.status.busy"
            keys = " Whisper is listening back "
        elif self._speaking:
            # Offered only while there is something to stop. A key hint for a
            # key that does nothing is how the approval menu went wrong.
            keys = " F6 skip the voice · PgUp/PgDn scroll · ^C stop "

        pending = self._approval
        if pending is not None:
            # The turn is stopped on a human. Nothing else on this bar matters
            # until it moves, and the keys named here are the ones actually
            # bound right now - the hint and the bindings are built from the
            # same flag on purpose, because the last time they were written in
            # two places they drifted and `a` came to mean deny.
            mode, mode_style = " APPROVAL NEEDED ", "class:tui.status.ask"
            keys = (
                " y allow · a always · n deny · ^C stop "
                if pending.get("offer_remember")
                else " y allow · n deny · ^C stop "
            )
            scroll = ""

        used = len(mode) + len(label) + len(scroll) + len(keys)
        pad = max(1, width - used)
        return [
            (mode_style, mode),
            ("class:tui.status", label + " " * pad),
            ("class:tui.status", scroll),
            ("class:tui.status.key", keys),
        ]

    def invalidate(self) -> None:
        with_app = self.app
        if with_app is None:
            return
        try:
            if with_app.is_running:
                with_app.invalidate()
        except Exception:  # noqa: BLE001
            return

    # -- input -------------------------------------------------------------

    def _accept(self, buffer: Any) -> bool:
        text = buffer.text
        buffer.reset(append_to_history=True)
        self._voice_reply_armed = False
        if text.strip():
            try:
                self._on_submit(text)
            except Exception as exc:  # noqa: BLE001
                self.pane.write(f"that failed: {exc}\n")
        return False

    def _toggle_voice_recording(self) -> None:
        if self._approval is not None or self._voice_state == "transcribing":
            return
        if self._voice_state == "recording":
            elapsed = time.monotonic() - self._voice_started
            if elapsed < VOICE_MIN_RECORD_SECONDS:
                self.echo("voice: listening - press again after speaking", KIND_NOTICE)
                return
            self._stop_voice_recording()
            return
        self._start_voice_recording()

    def _start_voice_recording(self) -> None:
        if self._approval is not None or self._voice_state == "transcribing":
            return
        if self._voice_state == "recording":
            self.echo("voice: already listening - press F5 to submit", KIND_NOTICE)
            return
        self._stop_voice_output()
        try:
            recorder = self._ensure_voice_recorder()
            recorder.start()
        except Exception as exc:  # noqa: BLE001
            self.echo(f"voice input unavailable: {exc}", KIND_NOTICE)
            return
        self._voice_state = "recording"
        self._voice_started = time.monotonic()
        self.echo("voice: listening - press F5 to submit", KIND_NOTICE)

    def _stop_voice_recording(self) -> None:
        recorder = self._voice_recorder
        if recorder is None:
            self._voice_state = ""
            return
        try:
            audio_path = recorder.stop()
        except Exception as exc:  # noqa: BLE001
            self._voice_state = ""
            self.echo(f"voice input failed: {exc}", KIND_NOTICE)
            return
        self._voice_state = "transcribing"
        self.echo("voice: transcribing with Whisper...", KIND_NOTICE)

        def work() -> None:
            try:
                service = self._ensure_voice_service()
                transcript = service.transcribe_file(audio_path)
                text = transcript.text.strip()
            except Exception as exc:  # noqa: BLE001
                self._post_from_voice_thread(lambda exc=exc: self._voice_failed(exc))
                return
            finally:
                with contextlib.suppress(OSError):
                    audio_path.unlink()
            self._post_from_voice_thread(lambda: self._voice_transcribed(text))

        import threading

        threading.Thread(target=work, daemon=True).start()

    def _voice_failed(self, exc: BaseException) -> None:
        self._voice_state = ""
        self.echo(f"voice input failed: {exc}", KIND_NOTICE)

    def _voice_transcribed(self, text: str) -> None:
        self._voice_state = ""
        if not text:
            self.echo("voice input failed: Whisper did not hear any English speech.", KIND_NOTICE)
            return
        self.echo(f'Heard: "{text}"', KIND_NOTICE)
        self._voice_reply_armed = True
        try:
            self._on_submit(text)
        except Exception as exc:  # noqa: BLE001
            self.echo(f"that failed: {exc}", KIND_NOTICE)

    def _post_from_voice_thread(self, func: Callable[[], None]) -> None:
        app = self.app
        loop = getattr(app, "loop", None)
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(func)
                return
        func()

    def _ensure_voice_service(self) -> Any:
        if self._voice_service is None:
            if self._voice_service_factory is not None:
                self._voice_service = self._voice_service_factory()
            else:
                from shamsu.voice import VoiceService

                self._voice_service = VoiceService()
        return self._voice_service

    def _ensure_voice_recorder(self) -> Any:
        if self._voice_recorder is None:
            if self._voice_recorder_factory is not None:
                self._voice_recorder = self._voice_recorder_factory()
            else:
                from shamsu.voice.recorder import MicrophoneRecorder

                self._voice_recorder = MicrophoneRecorder()
        return self._voice_recorder

    def speak_reply(self, text: str) -> None:
        body = str(text or "").strip()
        if not body:
            return
        # Consumed whether or not we end up speaking: one spoken prompt earns
        # one spoken reply, and a turn that answers silently must not leave the
        # flag standing for whatever the user types next.
        voice_input = self._voice_reply_armed
        self._voice_reply_armed = False
        if not self._reply_should_be_spoken(voice_input=voice_input):
            return

        def work() -> None:
            try:
                speaker = self._ensure_voice_output()
                self._speaking = True
                self._post_from_voice_thread(self.invalidate)
                speaker.speak(body)
            except Exception as exc:  # noqa: BLE001
                self._post_from_voice_thread(lambda exc=exc: self.echo(f"voice output failed: {exc}", KIND_NOTICE))
            finally:
                # In a `finally`: a playback error must not leave the frame
                # believing it is still speaking, offering a key that stops
                # nothing for the rest of the session.
                self._speaking = False
                self._post_from_voice_thread(self.invalidate)

        import threading

        threading.Thread(target=work, daemon=True).start()

    def _reply_should_be_spoken(self, *, voice_input: bool) -> bool:
        from shamsu.voice.speech import reply_should_be_spoken

        try:
            return reply_should_be_spoken(voice_input=voice_input)
        except Exception:  # noqa: BLE001 - a settings read must not eat a reply
            return False

    def _ensure_voice_output(self) -> Any:
        if self._voice_output is None:
            if self._voice_output_factory is not None:
                self._voice_output = self._voice_output_factory()
            else:
                from shamsu.voice import SpeechPlayer

                self._voice_output = SpeechPlayer()
        return self._voice_output

    def _skip_speech(self) -> None:
        """Shut it up, without turning voice off.

        Separate from `SHAMSU_VOICE_OUTPUT=off`, which is the setting: this is
        the key you press when the answer is spoken, you have already read it,
        and you want the next thing to happen now. The reply stays in the pane
        - only the audio is dropped.
        """
        if not self._speaking:
            self.echo("voice: nothing is being spoken", KIND_NOTICE)
            return
        self._stop_voice_output()
        self.echo("voice: skipped", KIND_NOTICE)

    def _stop_voice_output(self) -> None:
        speaker = self._voice_output
        stop = getattr(speaker, "stop", None)
        if callable(stop):
            with contextlib.suppress(Exception):
                stop()

    # -- approvals ---------------------------------------------------------

    def approval_pending(self) -> bool:
        return self._approval is not None

    def open_approval(self, record: Any, *, offer_remember: bool = False) -> None:
        """Put the question in the pane and take the keyboard for the answer.

        The old path handed the REAL terminal to a second prompt_toolkit
        application through `run_in_terminal`, which drops the alternate
        screen: mid-turn the user was ejected from the TUI into the plain CLI
        to answer, and the question itself was written to the pane they had
        just been taken away from. Two applications cannot share a terminal,
        so the fix is not to start a second one - the frame already owns a
        keyboard and a place to draw, and only ever needed to be asked.
        """
        import threading

        self.close_approval()
        self._approval = {
            "answer": "",
            "answered": threading.Event(),
            "offer_remember": bool(offer_remember),
        }
        self.pane.write_as(approval_lines(record, offer_remember=offer_remember), KIND_APPROVAL)
        self.pane.to_end()
        self.invalidate()

    def await_approval(self, timeout: float | None = None) -> str:
        """Block until a key answers, or `close_approval` releases this.

        Returns the raw key - `y`, `a`, `n`, or `""` when the frame let go
        without an answer. Interpreting it is the caller's job, so this stays
        the only place the frame knows about approvals at all.
        """
        pending = self._approval
        if pending is None:
            return ""
        pending["answered"].wait(timeout)
        return str(pending.get("answer") or "")

    def close_approval(self, note: str = "") -> None:
        """Give the keyboard back. ALWAYS releases whoever is waiting.

        Called from the asking side's `finally`, including when a phone or a
        browser answered first - so the waiter has to be woken even though no
        key was pressed here, or the thread parked in `await_approval` never
        returns and the turn hangs on a question that is already settled.
        """
        pending, self._approval = self._approval, None
        if pending is None:
            return
        if note:
            self.pane.write_as(f"  {note}", KIND_APPROVAL)
        pending["answered"].set()
        self.invalidate()

    def _answer_approval(self, choice: str) -> None:
        pending = self._approval
        if pending is None:
            return
        pending["answer"] = choice
        self._approval = None
        self.pane.write_as(
            "  -> " + {"y": "allowed", "a": "allowed, and remembered"}.get(choice, "denied"),
            KIND_APPROVAL,
        )
        pending["answered"].set()
        self.invalidate()

    def _wheel(self, event: Any) -> None:
        from prompt_toolkit.mouse_events import MouseEventType

        kind = getattr(event, "event_type", None)
        if kind == MouseEventType.SCROLL_UP:
            self.pane.scroll(-WHEEL_ROWS, self._output_height())
        elif kind == MouseEventType.SCROLL_DOWN:
            self.pane.scroll(WHEEL_ROWS, self._output_height())
        else:
            return
        self.invalidate()

    def _bindings(self) -> Any:
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.key_binding import KeyBindings

        keys = KeyBindings()

        # -- while a question is on screen ---------------------------------
        #
        # `eager` because these have to beat the buffer's own self-insert: the
        # answer is a keypress, not a line, and a `y` that landed in the input
        # box would be submitted as a prompt the moment the frame came back.
        # `asking` and the statusline hint are read off the SAME flag, so the
        # keys named on the bar are exactly the keys that are bound.
        asking = Condition(lambda: self._approval is not None)
        remembering = Condition(
            lambda: bool((self._approval or {}).get("offer_remember"))
        )

        @keys.add("y", filter=asking, eager=True)
        @keys.add("Y", filter=asking, eager=True)
        def _(_event) -> None:
            self._answer_approval("y")

        @keys.add("a", filter=remembering, eager=True)
        @keys.add("A", filter=remembering, eager=True)
        def _(_event) -> None:
            self._answer_approval("a")

        @keys.add("n", filter=asking, eager=True)
        @keys.add("N", filter=asking, eager=True)
        @keys.add("enter", filter=asking, eager=True)
        @keys.add("escape", filter=asking, eager=True)
        def _(_event) -> None:
            # Enter and Escape deny. Anything that is not clearly a yes is a
            # no, the same rule `_decision_from` applies in the terminal.
            self._answer_approval("n")

        @keys.add("<any>", filter=asking, eager=True)
        def _(_event) -> None:
            # Swallowed. `a` when remembering was not offered lands here too,
            # rather than being typed into a box nobody is reading.
            return

        @keys.add("pageup")
        def _(_event) -> None:
            self.pane.page(-1, self._output_height())

        @keys.add("pagedown")
        def _(_event) -> None:
            self.pane.page(1, self._output_height())

        @keys.add("c-up")
        def _(_event) -> None:
            self.pane.scroll(-1, self._output_height())

        @keys.add("c-down")
        def _(_event) -> None:
            self.pane.scroll(1, self._output_height())

        @keys.add("c-end")
        def _(_event) -> None:
            self.pane.to_end()

        @keys.add("c-home")
        def _(_event) -> None:
            self.pane.to_start()

        @keys.add("enter")
        def _(event) -> None:
            # Enter submits even though the buffer is multiline; Alt+Enter is
            # how you get a new line. Same contract as the idle prompt in
            # `_make_input_key_bindings`, so the two boxes behave alike.
            buffer = event.current_buffer
            state = buffer.complete_state
            if state is not None and state.current_completion is not None:
                buffer.apply_completion(state.current_completion)
                return
            buffer.validate_and_handle()

        @keys.add("escape", "enter")
        def _(event) -> None:
            event.current_buffer.insert_text("\n")

        @keys.add("tab")
        def _(event) -> None:
            buffer = event.current_buffer
            if buffer.complete_state is not None:
                buffer.complete_next()
            else:
                buffer.start_completion(select_first=False)

        @keys.add("f4")
        def _(_event) -> None:
            self._start_voice_recording()

        @keys.add("f6")
        def _(_event) -> None:
            self._skip_speech()

        @keys.add("c-space")
        @keys.add("f5")
        def _(_event) -> None:
            self._toggle_voice_recording()

        @keys.add("s-tab")
        def _(event) -> None:
            buffer = event.current_buffer
            if buffer.complete_state is not None:
                buffer.complete_previous()

        @keys.add("right")
        def _(event) -> None:
            # Accept the ghost suggestion when the cursor is at the end and
            # there is one; otherwise this is an ordinary cursor move.
            buffer = event.current_buffer
            suggestion = buffer.suggestion
            if suggestion and buffer.cursor_position == len(buffer.text):
                buffer.insert_text(suggestion.text)
            else:
                buffer.cursor_right()

        @keys.add("f2")
        def _(_event) -> None:
            # Mouse capture takes click-drag away from the terminal's own
            # selection. Most terminals still allow it with Shift held; not
            # all do, so it has to be switchable without leaving the TUI.
            self._mouse = not self._mouse

        @keys.add("c-c")
        def _(_event) -> None:
            if self._on_interrupt is not None:
                self._on_interrupt()

        @keys.add("c-d")
        def _(_event) -> None:
            if self._on_exit is not None:
                self._on_exit()

        return keys

    # -- handing the terminal back -----------------------------------------

    def adopt_console(self, console: Any) -> None:
        """Take responsibility for this console's width while the frame is up."""
        self._output_console = console
        self._sync_console_width()

    def echo(self, text: str, kind: str = KIND_NOTICE) -> None:
        """Put a line into the pane directly, without going through rich."""
        self.pane.write_as(text, kind)
        self.invalidate()

    def echo_prompt(self, text: str, surface: str = "cli") -> None:
        """A human's request, coloured by the surface it arrived from."""
        self.pane.write_prompt(text, surface)
        self.invalidate()

    def answering(self) -> Any:
        """Mark everything written inside as the agent's ANSWER.

        Wraps the one `console.print(Markdown(body))` that ends a turn. The
        answer is the thing you scroll back to find, and it used to be
        indistinguishable from the forty action rows above it.
        """
        # One blank row between the last action and the answer. Without it the
        # reply begins on the line directly under a tool row and the eye has
        # nothing to catch: the mark changes, but marks are two columns wide
        # and the text runs on at the same indent.
        self.pane.separate()
        gutter, gutter_style, style = LINE_KINDS[KIND_ANSWER]
        return self.pane.decorate_as(
            gutter, gutter_style, style, CONTINUATION_MARKS.get(KIND_ANSWER, "")
        )

    def absorb_for_display(self, event: Any) -> None:
        """Show a turn that started somewhere ELSE in this pane.

        Registered as a process-wide observer, so it sees the web portal's own
        `TurnStream` - which the terminal previously had no way to know
        existed. A prompt sent from the browser ran to completion with nothing
        on screen here; the surfaces were not out of sync, they were not
        connected.

        Only the shape of the turn is echoed - the prompt, the answer, and how
        it ended. Not the tool rows: a foreign turn's forty action lines
        interleaved with this terminal's own would make both unreadable, and
        the browser is already showing them to whoever is watching there.

        The CLI's own turns are skipped, because `FrameHost.submit` echoed the
        prompt when it was typed and the loop is already rendering the rest.
        """
        try:
            kind = str(getattr(event, "kind", ""))
            surface = str(getattr(event, "source", "") or "cli")
            text = str(getattr(event, "text", "") or "")
            if surface == "cli":
                return
            absorb = getattr(self.telemetry, "absorb", None)
            if callable(absorb):
                absorb(event)
            if kind == "turn.start" and text:
                self.echo_prompt(text, surface)
            elif surface == "telegram" and kind in _remote_live_body_kinds() and text:
                self.pane.write_as(f"  {text}", KIND_LOG)
                self.invalidate()
            elif surface == "telegram" and kind == "tool.result":
                data = dict(getattr(event, "data", None) or {})
                if data.get("ok") is False and text:
                    self.echo(f"telegram: {text}", KIND_NOTICE)
            elif surface == "telegram" and kind == "approval" and text:
                self.echo(f"telegram: {text}", KIND_NOTICE)
            elif kind == "assistant" and text:
                self.pane.write_as(text, KIND_ANSWER)
                self.invalidate()
            elif kind == "error" and text:
                self.echo(f"{surface}: {text}", KIND_NOTICE)
            elif kind == "turn.end":
                status = str((getattr(event, "data", None) or {}).get("status") or "")
                if status and status != "done":
                    self.echo(f"{surface} turn {status}", KIND_NOTICE)
        except Exception:  # noqa: BLE001
            return

    async def suspended(self, func: Callable[[], Any]) -> Any:
        """Run `func` on the REAL console, with the frame torn down.

        Two prompt_toolkit applications cannot run at once, so an approval
        question cannot simply be called from inside the frame. This is
        prompt_toolkit's supported way out: it drops the alternate screen,
        runs the callable against the actual terminal, and restores.
        """
        from prompt_toolkit.application.run_in_terminal import run_in_terminal

        return await run_in_terminal(func)


#: Pushed onto the input queue to mean "the user closed the frame".
CLOSED = object()


def _remote_live_body_kinds() -> frozenset[str]:
    """Visible live rows for a Telegram turn mirrored into the frame."""
    try:
        from shamsu.runtime.settings import verbosity as saved_verbosity
        from shamsu.runtime.turn_stream import body_kinds

        return body_kinds(saved_verbosity())
    except Exception:  # noqa: BLE001
        return frozenset({"activity", "tool.call"})


class FrameHost:
    """Keeps the frame up for the whole SESSION, not for one turn.

    The first version built a `TuiApp` inside the turn dispatcher and exited it
    when the turn ended. Two things followed, and both were reported
    immediately: the TUI flashed up and dropped back to the ordinary prompt
    after every turn, and the pane was empty each time because it was a new
    pane - so there was no conversation in it, ever.

    A frame is a mode, not a decoration on one turn. This runs the application
    on its own thread for as long as the mode is on, and the REPL's main loop
    reads its prompts from `read_line()` instead of from a `PromptSession`.
    Everything the session prints goes into the one pane, so the scrollback IS
    the conversation.

    Why a thread rather than the REPL's own loop: `main()` is synchronous and
    blocks on reading a line, and each turn runs its own `run_until_complete`
    on the request runner's loop. An `Application` needs a loop that keeps
    turning the whole time - including while a turn is blocked on the model -
    so it gets its own. `Application.invalidate()` is documented thread-safe,
    which is what makes writing to the pane from the main thread legal.
    """

    def __init__(self, app: TuiApp) -> None:
        import queue
        import threading

        self.app = app
        self.turn_active = False
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._loop: Any = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def start(self, timeout: float = 5.0) -> bool:
        """Bring the frame up. False if it could not start."""
        import threading

        if self._thread is not None:
            return True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self._ready.wait(timeout)

    def _run(self) -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def go() -> None:
            self._ready.set()
            await self.app.app.run_async()

        # A frame that dies must drop the user back to the ordinary REPL, not
        # take the session with it - `finally` below unblocks `read_line`.
        with contextlib.suppress(Exception):
            loop.run_until_complete(go())
        self._ready.set()
        self._closed = True
        self._queue.put(CLOSED)
        with contextlib.suppress(Exception):
            loop.close()

    def stop(self) -> None:
        self._closed = True
        app = self.app.app
        loop = self._loop
        if loop is not None and not loop.is_closed():
            with contextlib.suppress(Exception):
                loop.call_soon_threadsafe(lambda: app.exit() if app.is_running else None)
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        self._queue.put(CLOSED)

    @property
    def running(self) -> bool:
        return not self._closed and self._thread is not None

    @property
    def loop(self) -> Any:
        return self._loop

    # -- input -------------------------------------------------------------

    def submit(self, text: str) -> None:
        """One input box, two meanings.

        Idle, a line is the next prompt. Mid-turn it is a steer or a local
        command, which is what `on_route` already decides. The box never stops
        accepting either way - that is the point of pinning it.
        """
        if self.turn_active:
            self.app.echo_prompt(text, "cli")
            if self.on_route is not None:
                self.on_route(text)
            return
        self.app.echo("", KIND_LOG)
        self.app.echo_prompt(text, "cli")
        self._queue.put(text)

    #: Set by the REPL: where a mid-turn line goes.
    on_route: Callable[[str], None] | None = None

    def read_line(self, timeout: float | None = None) -> str:
        """Block until the user submits a line. Raises EOFError when closed."""
        item = self._queue.get() if timeout is None else self._queue.get(timeout=timeout)
        if item is CLOSED:
            raise EOFError
        return str(item)

