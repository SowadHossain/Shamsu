"""The pinned input line, the telemetry toolbar, and the side dispatcher.

This is the console half of the upgrade spec: an input box that is always
accepting and always visible, a status bar that never stops updating, and a
routing rule that keeps read-only questions out of the model's context.

WHY THIS EXISTS AT ALL, given that a feedback reader already did the job

It did not do the job. The reader it replaces was forty lines of raw
`msvcrt.getwch()`: it collected characters with no echo, so there was no
cursor, no visible line, and nothing on screen until you pressed Enter and
hoped. Backspace popped a buffer you could not see. Every keystroke went to the
same queue and reached the model, so asking "how full is my context?" cost
context to ask.

That reader existed because a pinned input box was believed to conflict fatally
with a raw keystroke thread on Windows - which is true, and is the wrong
conclusion. `prompt_toolkit` already owns every other input surface in SHAMSU:
the idle REPL prompt (`_make_prompt_session`), the approval prompt
(`safety/approval.py`), and the control console (`control/console.py`). The
keystroke thread was the last raw-input holdout, and it was the only reason the
conflict existed. Delete the thread and the constraint goes with it.

So there is no `rich.Live` here and there is no raw thread either. One
`PromptSession`, running on the REPL's own event loop, with `patch_stdout()`
lifting the agent's output above it. The toolbar is prompt_toolkit's own
`bottom_toolbar`, which is already a pinned, refreshable region - the same
place the idle prompt puts its status - so nothing has to fight for ownership
of the bottom line.

WHAT REPLACED THE SPINNER

`console.status` is a rich `Live`, and a `Live` and a `PromptSession` both want
to own the last row of the terminal. Rather than arbitrate, the spinner moves
INTO the toolbar: `TurnTelemetry` carries the frame and the status text, the
toolbar renders them, and `refresh_interval` on the prompt drives the
animation. One owner, and the numbers that used to be crammed onto the spinner
line get a row of their own.

WHAT IS DELIBERATELY NOT HERE

A full-height right sidebar, which the spec asks for. A side pane means owning
the whole frame with an `Application` and a `Layout`, and that costs the
scrollback: you can no longer scroll up and read what happened earlier in the
turn, which is most of why the log is worth painting well. The toolbar carries
the same numbers - rounds, context, files, contracts, both queue depths - at
the bottom of a terminal that still scrolls.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import time
from collections.abc import Callable, Iterable
from typing import Any

#: Spinner frames, matching the ones rich used, so the turn looks the same
#: after the move off `console.status`.
SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ASCII_SPINNER_FRAMES = "|/-\\"

#: How often the toolbar repaints. Fast enough that the spinner reads as
#: motion and the elapsed clock ticks; slow enough that it is not a busy loop
#: on a machine already running a local model.
REFRESH_SECONDS = 0.2

#: How often the prompt checks whether something else has taken the terminal.
#: Only a backstop - `LiveConsole.stand_down` is the primary, synchronous path -
#: so it is cheap enough to keep tight. It is one integer comparison.
STAND_DOWN_POLL_SECONDS = 0.01

#: Bar glyphs for the context meter.
BAR_FULL = "█"
BAR_EMPTY = "░"
ASCII_BAR_FULL = "#"
ASCII_BAR_EMPTY = "."
BAR_WIDTH = 10

#: Files listed by name on the toolbar before it switches to a count. Four
#: paths is already most of a narrow terminal.
MAX_FILES_NAMED = 3

#: How much of one path survives. Enough for `js/PlayerShip.js`, and the head
#: is what gets dropped so the filename always shows.
MAX_PATH_CHARS = 22

#: Commands that may run WHILE a turn is in flight.
#:
#: The dispatcher is read-only on purpose. Anything that mutates session state,
#: starts work, or changes the mode is unsafe to run underneath a turn that is
#: itself reading and writing that state - `/compact clear` mid-turn would
#: rewrite the history the model is being sent on the next round.
#:
#: Kept deliberately small. Each entry is a command the mid-turn dispatcher can
#: actually call - not a command that merely looks harmless - because a name on
#: this list that has no handler behind it is a promise the prompt cannot keep.
#: Adding one means wiring its handler in `_midturn_command` and nothing else.
MIDTURN_COMMANDS: frozenset[str] = frozenset({"/context", "/queue", "/help"})

#: The rungs of the toolbar's degradation ladder, cheapest information first.
#: See `TurnTelemetry._render_meters` for why the row narrows from the LEFT.
_LEVEL_COUNT_FILES = 1
_LEVEL_HIDE_FILES = 2
_LEVEL_DROP_TOKENS = 3
_LEVEL_DROP_BAR = 4
_DEGRADE_LEVELS = 5

#: What `route_input` decided. See `route_input`.
ROUTE_EMPTY = "empty"
ROUTE_COMMAND = "command"
ROUTE_DEFERRED = "deferred"
ROUTE_FEEDBACK = "feedback"


def supports_unicode(stream: Any = None) -> bool:
    """Whether the terminal can print the box and braille glyphs.

    A Windows console left on cp1252 raises `UnicodeEncodeError` on the spinner
    frame, and an exception thrown from inside a toolbar renderer takes the
    prompt down with it - so this is checked once and the ASCII set is used
    when in doubt.
    """
    if os.environ.get("SHAMSU_ASCII_UI", "").strip() == "1":
        return False
    target = stream if stream is not None else sys.stdout
    encoding = getattr(target, "encoding", None) or ""
    try:
        SPINNER_FRAMES[0].encode(encoding or "ascii")
        BAR_FULL.encode(encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def route_input(text: str, *, midturn: bool) -> tuple[str, str]:
    """Decide where a line the user typed should go. Pure, so it is testable.

    Returns `(route, payload)`:

    * `ROUTE_EMPTY`    - nothing was typed.
    * `ROUTE_COMMAND`  - a slash command to execute locally and print. It never
      enters the message array, so asking a question about the run costs zero
      context tokens. This is the whole point of the side dispatcher.
    * `ROUTE_DEFERRED` - a slash command that is not safe to run mid-turn.
    * `ROUTE_FEEDBACK` - plain text, for the model at the next round boundary.

    `midturn` is what separates the last two. At an idle prompt every command
    is fine; underneath a running turn only the read-only ones are.
    """
    line = (text or "").strip()
    if not line:
        return (ROUTE_EMPTY, "")
    if not line.startswith("/"):
        return (ROUTE_FEEDBACK, line)
    if not midturn:
        return (ROUTE_COMMAND, line)
    head = line.split()[0].lower()
    if head in MIDTURN_COMMANDS:
        return (ROUTE_COMMAND, line)
    return (ROUTE_DEFERRED, line)


def _compact_tokens(count: Any) -> str:
    """`23.3k`, `6.1k`, `847`."""
    if not isinstance(count, (int, float)) or count < 0:
        return ""
    if count < 1000:
        return str(int(count))
    return f"{count / 1000:.1f}k".replace(".0k", "k")


class TurnTelemetry:
    """Every number the toolbar shows, and the rule for turning it into a row.

    Fed from the turn stream - the same events the CLI renderer and the phone
    read - rather than from private callbacks, so what the toolbar claims is
    what actually happened. Every one of these numbers already existed in the
    runtime and none of them were ever shown: you found out you had blown the
    context window by watching the run degrade.
    """

    def __init__(self, *, unicode_ui: bool = True) -> None:
        self.unicode_ui = unicode_ui
        self.reset()
        #: Depth callables, set by whoever owns the queues. Kept as callables
        #: rather than copied numbers so the toolbar cannot go stale between
        #: a push and the next event.
        self.feedback_depth: Callable[[], int] = lambda: 0
        self.tasks_depth: Callable[[], int] = lambda: 0

    def reset(self) -> None:
        self.active = False
        self.status_text = ""
        self.round = 0
        self.max_rounds = 0
        self.ctx_pct: int | None = None
        self.ctx_used = 0
        self.ctx_total = 0
        self.files: list[str] = []
        self.contracts = 0
        self.started = 0.0
        self._frame = 0

    # -- intake ------------------------------------------------------------

    def absorb(self, event: Any) -> None:
        """Read one turn-stream event. Never raises: a toolbar that throws
        takes the prompt down with it, and a wrong number is better than no
        terminal."""
        try:
            self._absorb(event)
        except Exception:  # noqa: BLE001
            return

    def _absorb(self, event: Any) -> None:
        kind = str(getattr(event, "kind", "") or "")
        data = dict(getattr(event, "data", None) or {})
        text = str(getattr(event, "text", "") or "")

        if kind == "turn.start":
            self.reset()
            self.active = True
            self.started = time.monotonic()
            return
        if kind == "turn.end":
            self.active = False
            self.status_text = ""
            return

        if kind == "status" and text:
            self.status_text = text
        self._absorb_meter(data)

        if kind == "tool.result" and data.get("ok", True):
            target = str(data.get("target") or "")
            tool = str(data.get("tool") or "")
            if target and tool in _WRITER_TOOLS and target not in self.files:
                self.files.append(target)

    def _absorb_meter(self, data: dict[str, Any]) -> None:
        current, total = data.get("round"), data.get("max_rounds")
        if isinstance(current, int) and current > 0:
            self.round = current
        if isinstance(total, int) and total > 0:
            self.max_rounds = total
        pct = data.get("ctx_pct")
        if isinstance(pct, int):
            self.ctx_pct = pct
        used, window = data.get("ctx_used"), data.get("ctx_window")
        if isinstance(used, int) and used > 0:
            self.ctx_used = used
        if isinstance(window, int) and window > 0:
            self.ctx_total = window
        contracts = data.get("contracts_open")
        if isinstance(contracts, int) and contracts >= 0:
            self.contracts = contracts

    def set_status(self, message: str) -> None:
        """The text half of the status line, for routes that have no turn
        stream to publish on (the legacy router, the PRD paths, plain Q&A)."""
        self.status_text = _strip_markup(message)

    # -- output ------------------------------------------------------------

    def tick(self) -> None:
        self._frame += 1

    @property
    def elapsed(self) -> str:
        if not self.started:
            return ""
        seconds = int(time.monotonic() - self.started)
        if seconds < 60:
            return f"{seconds}s"
        return f"{seconds // 60}m{seconds % 60:02d}s"

    def spinner(self) -> str:
        frames = SPINNER_FRAMES if self.unicode_ui else ASCII_SPINNER_FRAMES
        return frames[self._frame % len(frames)]

    def context_bar(self) -> str:
        if self.ctx_pct is None:
            return ""
        full_glyph = BAR_FULL if self.unicode_ui else ASCII_BAR_FULL
        empty_glyph = BAR_EMPTY if self.unicode_ui else ASCII_BAR_EMPTY
        filled = max(0, min(BAR_WIDTH, round(self.ctx_pct / 100 * BAR_WIDTH)))
        return full_glyph * filled + empty_glyph * (BAR_WIDTH - filled)

    def context_style(self) -> str:
        """Yellow at 60, red at 80 - the same thresholds the CLI renderer uses
        on the meter, so one number does not read as two different warnings."""
        if self.ctx_pct is None:
            return "class:tb.dim"
        if self.ctx_pct >= 80:
            return "class:tb.alarm"
        if self.ctx_pct >= 60:
            return "class:tb.warn"
        return "class:tb.ok"

    def files_label(self) -> str:
        """Paths, not basenames. `js/config.py` and `css/config.py` are
        different files and a toolbar that calls both `config.py` is worse than
        one that says nothing - so only the head of a long path is dropped, and
        the tail that identifies the file always survives."""
        if not self.files:
            return "-"
        if len(self.files) > MAX_FILES_NAMED:
            return f"{len(self.files)} files"
        return " ".join(_tail(path, MAX_PATH_CHARS) for path in self.files)

    def render(self, width: int = 0) -> list[tuple[str, str]]:
        """Two rows of prompt_toolkit formatted text.

        Row one is what is happening and for how long. Row two is the sidebar,
        laid on its side: rounds, context, files, contracts, and both queues.
        """
        columns = width or _terminal_width()
        rows: list[tuple[str, str]] = []

        head = f"{self.spinner()} {self.status_text}" if self.active else "idle"
        elapsed = self.elapsed if self.active else ""
        rows.extend(_justify(("class:tb.head", head), ("class:tb.dim", elapsed), columns))
        rows.append(("", "\n"))
        rows.extend(self._render_meters(columns))
        return rows

    def _render_meters(self, columns: int) -> list[tuple[str, str]]:
        """Build the row, then give ground in a fixed order until it fits.

        Straight clipping trims the END of the row, and the end is where the
        queue depths live - so on a narrow terminal the first thing to vanish
        was the number telling you the steer you just typed had been accepted.
        That is the exact failure the pinned prompt exists to fix.

        So the row yields from the left instead, cheapest information first:
        the file list becomes a count, then goes; then the raw token counts,
        which the percentage already summarises; then the bar, which the
        percentage already summarises too. The queue depths and the round
        budget are never given up, because at 50 columns they are the only
        things left worth showing.
        """
        for level in range(_DEGRADE_LEVELS):
            cells = self._meter_cells(level)
            if _cells_width(cells) <= columns:
                return cells
        return _clip_cells(cells, columns)

    def _meter_cells(self, level: int = 0) -> list[tuple[str, str]]:
        """One row of cells at a given degradation level. See `_render_meters`."""
        cells: list[tuple[str, str]] = []
        separator = ("class:tb.sep", " │ " if self.unicode_ui else " | ")

        if self.max_rounds:
            cells.append(("class:tb.label", "rnd "))
            cells.append(("class:tb.value", f"{self.round}/{self.max_rounds}"))
            cells.append(separator)

        cells.append(("class:tb.label", "ctx "))
        if self.ctx_pct is None:
            cells.append(("class:tb.dim", "-"))
        else:
            if level < _LEVEL_DROP_BAR:
                cells.append((self.context_style(), self.context_bar() + " "))
            cells.append((self.context_style(), f"{self.ctx_pct}%"))
            if level < _LEVEL_DROP_TOKENS and self.ctx_used and self.ctx_total:
                used = _compact_tokens(self.ctx_used)
                total = _compact_tokens(self.ctx_total)
                cells.append(("class:tb.dim", f" {used}/{total}"))
        cells.append(separator)

        if level < _LEVEL_HIDE_FILES:
            label = self.files_label()
            if level >= _LEVEL_COUNT_FILES and self.files:
                label = f"{len(self.files)} files"
            cells.append(("class:tb.label", "files "))
            cells.append(("class:tb.value", label))
            cells.append(separator)

        cells.append(("class:tb.label", "ct " if level >= _LEVEL_DROP_TOKENS else "contracts "))
        cells.append(
            (("class:tb.warn" if self.contracts else "class:tb.value"), str(self.contracts))
        )
        cells.append(separator)

        feedback, tasks = _depth(self.feedback_depth), _depth(self.tasks_depth)
        cells.append(("class:tb.label", "fb "))
        cells.append((("class:tb.value" if feedback else "class:tb.dim"), str(feedback)))
        cells.append(("class:tb.label", "  tasks "))
        cells.append((("class:tb.value" if tasks else "class:tb.dim"), str(tasks)))

        return cells

    def plain(self, width: int = 0) -> str:
        """The same two rows as text. For tests, and for any surface that
        cannot render styles."""
        return "".join(text for _style, text in self.render(width))


#: Tools whose success means a file on disk changed. Same set the CLI renderer
#: treats as writers.
_WRITER_TOOLS = frozenset(
    {"write_file", "patch_file", "append_file", "replace_symbol", "delete_file"}
)


def _depth(source: Callable[[], int]) -> int:
    try:
        return int(source())
    except Exception:  # noqa: BLE001
        return 0


def _tail(path: str, limit: int) -> str:
    """Keep the end of a path, which is the part that names the file."""
    text = (path or "").replace("\\", "/")
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1):]


def _terminal_width(default: int = 100) -> int:
    try:
        return max(40, shutil.get_terminal_size((default, 24)).columns)
    except Exception:  # noqa: BLE001
        return default


def _strip_markup(text: str) -> str:
    """Drop rich markup tags. The status line arrives already composed for a
    rich console - `[yellow]ctx 71%[/yellow]` - and prompt_toolkit would print
    the brackets literally."""
    out: list[str] = []
    depth = 0
    for char in text or "":
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return "".join(out).strip()


def _justify(
    left: tuple[str, str], right: tuple[str, str], columns: int
) -> list[tuple[str, str]]:
    """Left text, right text, padding between - clipped to the terminal."""
    left_style, left_text = left
    right_style, right_text = right
    room = max(0, columns - len(right_text) - 1)
    if len(left_text) > room:
        left_text = left_text[: max(0, room - 1)] + ("…" if room else "")
    pad = max(1, columns - len(left_text) - len(right_text))
    cells = [(left_style, left_text), ("", " " * pad)]
    if right_text:
        cells.append((right_style, right_text))
    return cells


def _cells_width(cells: Iterable[tuple[str, str]]) -> int:
    return sum(len(text) for _style, text in cells)


def _clip_cells(cells: Iterable[tuple[str, str]], columns: int) -> list[tuple[str, str]]:
    """Trim a row of cells to the terminal width, cell by cell."""
    out: list[tuple[str, str]] = []
    used = 0
    for style, text in cells:
        if used >= columns:
            break
        room = columns - used
        if len(text) > room:
            out.append((style, text[:room]))
            used = columns
            break
        out.append((style, text))
        used += len(text)
    return out


class ToolbarStatus:
    """Stands in for rich's `console.status` object.

    The REPL passes a `thinking_status` down through every route and they all
    call `.update(...)` on it. Keeping that shape means the twenty-odd call
    sites do not have to know whether they are painting a spinner or a toolbar.
    """

    def __init__(self, telemetry: TurnTelemetry, invalidate: Callable[[], None]) -> None:
        self._telemetry = telemetry
        self._invalidate = invalidate

    def update(self, message: str) -> None:
        self._telemetry.set_status(str(message))
        self._invalidate()

    def stop(self) -> None:
        self._telemetry.status_text = ""
        self._invalidate()

    def start(self) -> None:  # pragma: no cover - protocol completeness
        pass

    def __enter__(self) -> ToolbarStatus:  # noqa: PYI034
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


class LiveConsole:
    """The input line that stays up while the agent works.

    Owns one `PromptSession` and runs it as a task on the REPL's own event
    loop, alongside the turn. `patch_stdout()` - applied by the caller around
    the whole turn - lifts everything the agent prints above the prompt, so the
    log scrolls normally and the input line stays where you left it, with
    whatever you had half-typed still in it.

    Three things it must not do:

    * **Never hold the keyboard while an approval is waiting.** Two
      prompt_toolkit applications cannot share a terminal. `prompt_is_active()`
      is the existing flag for this, and a watchdog exits our prompt the moment
      it flips - the approval question then owns the console exactly as it did
      before. Anything half-typed at that moment is lost, which is the correct
      trade against the two prompts corrupting each other's rendering.
    * **Never raise into the turn.** A broken terminal, a piped stdin, a CI
      runner: the turn must run exactly as it did before, just without a live
      prompt.
    * **Never send a slash command to the model.** That is `route_input`, and
      it is the difference between asking a question about the run and paying
      context to ask it.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[Callable[[], Any]], Any],
        feedback: Any,
        tasks: Any,
        on_command: Callable[[str], None],
        notify: Callable[[str], None],
        prompt_is_active: Callable[[], bool],
        unicode_ui: bool | None = None,
    ) -> None:
        self.telemetry = TurnTelemetry(
            unicode_ui=supports_unicode() if unicode_ui is None else unicode_ui
        )
        self.feedback = feedback
        self.tasks = tasks
        self.telemetry.feedback_depth = lambda: _queue_depth(feedback)
        self.telemetry.tasks_depth = lambda: _queue_depth(tasks)
        self._session_factory = session_factory
        self._on_command = on_command
        self._notify = notify
        self._prompt_is_active = prompt_is_active
        self._session: Any = None
        self._broken = False
        #: The loop the prompt runs on, so `stand_down` can reach it from the
        #: worker thread a tool's approval fires on.
        self._loop: Any = None

    # -- the bus -----------------------------------------------------------

    def absorb(self, event: Any) -> None:
        """Turn-stream renderer. Registered next to `CliTurnRenderer`, reading
        the same events, so the toolbar and the log can never disagree."""
        self.telemetry.absorb(event)
        self.invalidate()

    def status(self) -> ToolbarStatus:
        """The `thinking_status` object the REPL's routes already expect."""
        return ToolbarStatus(self.telemetry, self.invalidate)

    def invalidate(self) -> None:
        session = self._session
        app = getattr(session, "app", None)
        if app is None:
            return
        with contextlib.suppress(Exception):
            if app.is_running:
                app.invalidate()

    # -- the prompt --------------------------------------------------------

    def _toolbar(self) -> Any:
        # Called by prompt_toolkit on every repaint, which is what advances the
        # spinner: the refresh interval IS the animation clock, so there is no
        # second timer to keep in step with it.
        self.telemetry.tick()
        return self.telemetry.render()

    def _ensure_session(self) -> Any:
        if self._session is None and not self._broken:
            try:
                self._session = self._session_factory(self._toolbar)
            except Exception:  # noqa: BLE001
                self._broken = True
                self._session = None
        return self._session

    def stand_down(self) -> None:
        """Give the terminal up NOW. Safe to call from any thread.

        The synchronous half of handing over to an approval prompt. Polling
        `prompt_is_active()` alone leaves a window: the flag goes up, and until
        the next poll two prompt_toolkit applications are live on one terminal.
        `safety/approval.py` calls this before it renders the question, and
        rendering the question is what buys the loop the moment it needs to act.

        Tools run on a worker thread, so this must not touch the app directly -
        it hands the work to the loop the prompt is actually running on.
        """
        loop, app = self._loop, getattr(self._session, "app", None)
        if loop is None or app is None:
            return

        def close() -> None:
            with contextlib.suppress(Exception):
                if app.is_running:
                    app.exit(result="")

        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(close)

    async def input_loop(self) -> None:
        """Prompt, route, repeat, until the turn ends and this is cancelled."""
        self._loop = asyncio.get_running_loop()
        while True:
            if self._prompt_is_active():
                await asyncio.sleep(STAND_DOWN_POLL_SECONDS)
                continue
            session = self._ensure_session()
            if session is None:
                return
            try:
                text = await self._prompt_once(session)
            except asyncio.CancelledError:
                raise
            except (EOFError, KeyboardInterrupt):
                # Ctrl+C belongs to `_RequestRunner`, which cancels the turn.
                # Swallowing it here would make the turn uncancellable.
                await asyncio.sleep(0.05)
                continue
            except Exception:  # noqa: BLE001 - a dead terminal costs the turn nothing
                self._broken = True
                return
            self.route(text)

    async def _prompt_once(self, session: Any) -> str:
        guard = asyncio.ensure_future(self._stand_down(session))
        try:
            return await session.prompt_async(
                [("class:tb.caret", "» ")],
                refresh_interval=REFRESH_SECONDS,
            )
        finally:
            guard.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await guard

    async def _stand_down(self, session: Any) -> None:
        """The polling half, as a backstop for anything that takes the terminal
        without announcing itself through `on_prompt_open`."""
        while not self._prompt_is_active():
            await asyncio.sleep(STAND_DOWN_POLL_SECONDS)
        app = getattr(session, "app", None)
        if app is None:
            return
        with contextlib.suppress(Exception):
            if app.is_running:
                app.exit(result="")

    # -- routing -----------------------------------------------------------

    def route(self, text: str) -> str:
        """Send one typed line where it belongs. Returns the route taken."""
        route, payload = route_input(text, midturn=True)
        if route == ROUTE_EMPTY:
            return route
        if route == ROUTE_FEEDBACK:
            if _queue_push(self.feedback, payload):
                self._notify("noted - passing that to the agent at the next step")
            return route
        if route == ROUTE_COMMAND:
            try:
                self._on_command(payload)
            except Exception as exc:  # noqa: BLE001
                self._notify(f"that command failed: {exc}")
            return route
        head = payload.split()[0]
        self._notify(
            f"{head} changes session state, so it cannot run while a turn is in "
            "flight - it would rewrite the history the model is being sent. "
            "Run it when this turn ends."
        )
        return route


def _queue_depth(queue: Any) -> int:
    try:
        return len(queue)
    except Exception:  # noqa: BLE001
        return 0


def _queue_push(queue: Any, text: str) -> bool:
    try:
        return bool(queue.push(text))
    except Exception:  # noqa: BLE001
        return False
