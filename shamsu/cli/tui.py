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

#: The sidebar's fixed width. Below `MIN_WIDTH_FOR_SIDEBAR` it is dropped
#: entirely and the telemetry falls back to the bottom toolbar, because a
#: 28-column pane out of 70 leaves the log unreadable.
SIDEBAR_WIDTH = 30
MIN_WIDTH_FOR_SIDEBAR = 90


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


def parse_ansi(text: str) -> list[tuple[str, str]]:
    """ANSI escapes to styled fragments, coalesced."""
    return coalesce(to_formatted_text(ANSI(text)))


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

    # -- intake ------------------------------------------------------------

    def write(self, text: str) -> None:
        """Append output. Accepts ANSI; partial lines are held until closed."""
        if not text:
            return
        lines = split_lines(parse_ansi(text))
        lines[0] = self._partial + lines[0]
        self._partial = lines.pop()
        for line in lines:
            self._append_logical(line)
        if self.follow:
            self.to_end()

    def _append_logical(self, line: list[tuple[str, str]]) -> None:
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


def render_sidebar(telemetry: Any, width: int = SIDEBAR_WIDTH) -> list[tuple[str, str]]:
    """`TurnTelemetry` laid out vertically, which is what it was always for.

    The bottom toolbar had to give up cells as the terminal narrowed - the file
    list first, then the token counts, then the bar. A column has room for all
    of it at once, which is the whole argument for the split.
    """
    rows: list[tuple[str, str]] = []

    def line(style: str, text: str) -> None:
        rows.append((style, text[: width - 1].ljust(width - 1) + "\n"))

    def heading(text: str) -> None:
        line("class:tui.heading", text.upper())

    active = bool(getattr(telemetry, "active", False))
    line("class:tui.title", " SHAMSU")
    line("class:tui.rule", " " + "─" * (width - 3))

    status = str(getattr(telemetry, "status_text", "") or "")
    if active:
        line("class:tb.head", f" {telemetry.spinner()} {status}"[: width - 1])
        line("class:tb.dim", f"   {telemetry.elapsed}")
    else:
        line("class:tb.dim", " idle")
        line("", "")
    line("", "")

    if getattr(telemetry, "max_rounds", 0):
        heading(" Round budget")
        line("class:tb.value", f"   {telemetry.round} / {telemetry.max_rounds}")
        line("", "")

    heading(" Context window")
    pct = getattr(telemetry, "ctx_pct", None)
    if pct is None:
        line("class:tb.dim", "   not measured yet")
    else:
        line(telemetry.context_style(), f"   {telemetry.context_bar()} {pct}%")
        used, total = getattr(telemetry, "ctx_used", 0), getattr(telemetry, "ctx_total", 0)
        if used and total:
            line("class:tb.dim", f"   {_short(used)} / {_short(total)}")
    line("", "")

    heading(" Files modified")
    files = list(getattr(telemetry, "files", []) or [])
    if not files:
        line("class:tb.dim", "   none")
    for path in files[:6]:
        line("class:tb.value", f"   {path}"[: width - 1])
    if len(files) > 6:
        line("class:tb.dim", f"   +{len(files) - 6} more")
    line("", "")

    heading(" Open contracts")
    contracts = int(getattr(telemetry, "contracts", 0) or 0)
    line("class:tb.warn" if contracts else "class:tb.value", f"   {contracts}")
    line("", "")

    heading(" Queues")
    feedback = _depth(getattr(telemetry, "feedback_depth", None))
    tasks = _depth(getattr(telemetry, "tasks_depth", None))
    line("class:tb.value" if feedback else "class:tb.dim", f"   feedback  {feedback}")
    line("class:tb.value" if tasks else "class:tb.dim", f"   tasks     {tasks}")
    return rows


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

TUI_STYLE = {
    "tui.title": "bold #d7af5f",
    "tui.heading": "#7f7f7f",
    "tui.rule": "#4e4e4e",
    "tui.status": "bg:#262626 #b2b2b2",
    "tui.sidebar": "bg:#1c1c1c",
    "tui.caret": "bold #5fd75f",
    "tb.head": "bold",
    "tb.label": "#7f7f7f",
    "tb.value": "#d0d0d0",
    "tb.dim": "#6c6c6c",
    "tb.sep": "#4e4e4e",
    "tb.ok": "#5fd75f",
    "tb.warn": "#d7af5f",
    "tb.alarm": "bold #ff5f5f",
}


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
        mouse_support: bool = True,
    ) -> None:
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
        from prompt_toolkit.layout.containers import ConditionalContainer
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension
        from prompt_toolkit.styles import Style

        self.telemetry = telemetry
        self.pane = LogPane()
        self._on_submit = on_submit
        self._on_interrupt = on_interrupt
        self._on_exit = on_exit
        self._prompt_label = prompt_label
        self._mouse = mouse_support
        self.app: Any = None

        self.buffer = Buffer(
            history=history,
            completer=completer,
            complete_while_typing=bool(completer),
            multiline=False,
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
        self.layout = Layout(
            HSplit(
                [
                    VSplit([output, sidebar]),
                    Window(height=1, content=FormattedTextControl(self._statusline)),
                    Window(
                        height=1,
                        content=BufferControl(
                            buffer=self.buffer,
                            input_processors=[],
                        ),
                    ),
                ]
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
        width = self._terminal_width()
        return max(20, width - (SIDEBAR_WIDTH if self._sidebar_fits() else 0))

    def _output_height(self) -> int:
        try:
            return max(1, self.app.output.get_size().rows - 2)
        except Exception:  # noqa: BLE001
            return 20

    def _output_fragments(self) -> list[tuple]:
        self.pane.set_width(self.output_width())
        return self.pane.visible(self._output_height(), self._wheel)

    def _sidebar_fragments(self) -> list[tuple[str, str]]:
        try:
            self.telemetry.tick()
            return render_sidebar(self.telemetry)
        except Exception:  # noqa: BLE001 - a sidebar that raises kills the frame
            return [("class:tb.dim", " telemetry unavailable")]

    def _statusline(self) -> list[tuple[str, str]]:
        width = self._terminal_width()
        left = f" {self._prompt_label()}"
        right = (
            f"{'mouse' if self._mouse else 'MOUSE OFF'} · "
            f"{self.pane.scroll_position()} · "
            "PgUp/PgDn scroll · F2 mouse · ^C stop "
        )
        pad = max(1, width - len(left) - len(right))
        return [("class:tui.status", left + " " * pad + right)]

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
        if text.strip():
            try:
                self._on_submit(text)
            except Exception as exc:  # noqa: BLE001
                self.pane.write(f"that failed: {exc}\n")
        return False

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
        from prompt_toolkit.key_binding import KeyBindings

        keys = KeyBindings()

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

    def echo(self, text: str, style: str = "") -> None:
        """Put a line into the pane directly, without going through rich."""
        self.pane.write(text if text.endswith("\n") else text + "\n")
        self.invalidate()

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
            self.app.echo(f"» {text}")
            if self.on_route is not None:
                self.on_route(text)
            return
        self.app.echo("")
        self.app.echo(f"› {text}")
        self._queue.put(text)

    #: Set by the REPL: where a mid-turn line goes.
    on_route: Callable[[str], None] | None = None

    def read_line(self, timeout: float | None = None) -> str:
        """Block until the user submits a line. Raises EOFError when closed."""
        item = self._queue.get() if timeout is None else self._queue.get(timeout=timeout)
        if item is CLOSED:
            raise EOFError
        return str(item)

