"""The only module that touches a terminal.

Everything else in `ui/` is a pure function of its input. This is where the file
descriptors, the escape codes, and the signal handlers live, isolated so that
the parts worth testing do not need a TTY.

**The terminal is always restored.** Raw mode, the alternate screen, and a
hidden cursor are all process-global state borrowed from the user's shell. A
crash that leaves a terminal in raw mode makes the user reach for `reset` — so
restoration happens in a `finally`, and the original `termios` settings are
captured before anything is changed.

**Not a TTY is a supported case.** Piped output, a CI log, and `--no-tui` all
take the plain path: no escape codes, no alternate screen, one line per event.
A tool that only works interactively cannot be scripted, and a coding agent
that cannot be scripted is half a tool.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType
from typing import IO, Any, TextIO

#: Escape sequences, named rather than inlined.
_ALT_SCREEN_ON = "\x1b[?1049h"
_ALT_SCREEN_OFF = "\x1b[?1049l"
_CURSOR_HIDE = "\x1b[?25l"
_CURSOR_SHOW = "\x1b[?25h"
_CURSOR_HOME = "\x1b[H"
_CLEAR_BELOW = "\x1b[J"

DEFAULT_SIZE = (80, 24)


@dataclass(frozen=True)
class Size:
    width: int
    height: int


def terminal_size(stream: IO[str] | None = None) -> Size:
    """Current window size, with a sane fallback.

    A fallback rather than a failure: output redirected to a file has no size,
    and refusing to render there would make the plain path impossible.
    """
    try:
        columns, lines = shutil.get_terminal_size(DEFAULT_SIZE)
    except (OSError, ValueError):  # pragma: no cover - platform dependent
        columns, lines = DEFAULT_SIZE
    del stream
    return Size(width=max(1, columns), height=max(1, lines))


def supports_tui(stream: TextIO | None = None) -> bool:
    """Whether a full-screen interface is appropriate here.

    Checks the stream is a TTY, that `TERM` is not `dumb`, and that `NO_COLOR`
    has not been set. All three are conventions users expect to be honoured,
    and ignoring any of them produces escape codes in someone's log file.
    """
    target = stream or sys.stdout
    if not hasattr(target, "isatty") or not target.isatty():
        return False
    if os.environ.get("TERM", "").lower() in ("", "dumb"):
        return False
    return "NO_COLOR" not in os.environ


class Screen:
    """A full-screen alternate-buffer display.

    Repaints by moving the cursor home and overwriting, rather than clearing
    first: clearing produces a visible flash on every frame, which on a run
    that repaints once a second is genuinely unpleasant to sit in front of.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
        self._previous: list[str] = []
        self._resized = False

    def paint(self, lines: list[str]) -> None:
        """Draw a frame.

        Only the lines that changed are rewritten unless the window resized, in
        which case everything is. Diffing matters on a slow link and costs
        nothing locally.
        """
        out = [_CURSOR_HOME]

        if self._resized or len(lines) != len(self._previous):
            out.append(_CLEAR_BELOW)
            self._previous = []
            self._resized = False

        for index, line in enumerate(lines):
            if index < len(self._previous) and self._previous[index] == line:
                continue
            out.append(f"\x1b[{index + 1};1H\x1b[K{line}")

        self._previous = list(lines)
        self._stream.write("".join(out))
        self._stream.flush()

    def note_resize(self) -> None:
        self._resized = True


@contextmanager
def managed_screen(stream: TextIO | None = None) -> Iterator[Screen]:
    """Enter the alternate screen and guarantee the terminal is restored.

    The `finally` is the entire point. Every escape sequence written here
    changes state that outlives this process if it is not undone.
    """
    target = stream or sys.stdout
    screen = Screen(target)

    original = _raw_mode(target)
    target.write(_ALT_SCREEN_ON + _CURSOR_HIDE)
    target.flush()

    previous_winch = _install_resize_handler(screen)

    try:
        yield screen
    finally:
        _restore_resize_handler(previous_winch)
        target.write(_CURSOR_SHOW + _ALT_SCREEN_OFF)
        target.flush()
        _restore_mode(target, original)


def read_key(timeout: float = 0.1) -> str:
    """One keypress, or "" when nothing arrived before the timeout.

    Non-blocking so the repaint loop keeps running: an interface that only
    redraws when a key is pressed looks frozen during the thirty seconds a
    local model spends thinking.
    """
    import select

    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):  # pragma: no cover - closed stdin
        return ""

    if not ready:
        return ""

    try:
        char = os.read(sys.stdin.fileno(), 1).decode("utf-8", "ignore")
    except (OSError, ValueError):  # pragma: no cover
        return ""

    if char != "\x1b":
        return char

    # An escape sequence: read the rest without blocking, so a lone Escape key
    # is not mistaken for the start of an arrow key that never arrives.
    sequence = char
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], 0.01)
        if not ready:
            break
        sequence += os.read(sys.stdin.fileno(), 1).decode("utf-8", "ignore")
    return sequence


# -- platform details ------------------------------------------------------


def _raw_mode(stream: TextIO) -> Any | None:
    """Put the terminal in cbreak mode, returning the settings to restore.

    cbreak rather than full raw: signals stay enabled, so Ctrl-C still raises
    `KeyboardInterrupt` and the run's real cancellation path is used instead of
    a key handler reimplementing it.
    """
    try:
        import termios
        import tty
    except ImportError:  # pragma: no cover - Windows
        return None

    if not hasattr(stream, "fileno") or not stream.isatty():
        return None

    try:
        descriptor = stream.fileno()
        original = termios.tcgetattr(descriptor)
        tty.setcbreak(descriptor)
    except (termios.error, OSError, ValueError):  # pragma: no cover
        return None
    return original


def _restore_mode(stream: TextIO, original: Any | None) -> None:
    if original is None:
        return
    try:
        import termios

        termios.tcsetattr(stream.fileno(), termios.TCSADRAIN, original)
    except Exception:  # noqa: BLE001 - pragma: no cover
        # Restoring is best-effort by necessity: if it fails there is nothing
        # further to try, and raising here would mask the real exception that
        # sent us into the `finally`.
        pass


def _install_resize_handler(screen: Screen) -> object | None:
    if not hasattr(signal, "SIGWINCH"):  # pragma: no cover - Windows
        return None

    def handler(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        screen.note_resize()

    try:
        return signal.signal(signal.SIGWINCH, handler)
    except (ValueError, OSError):  # pragma: no cover - not the main thread
        return None


def _restore_resize_handler(previous: object | None) -> None:
    if previous is None or not hasattr(signal, "SIGWINCH"):  # pragma: no cover
        return
    with contextlib.suppress(ValueError, OSError, TypeError):  # pragma: no cover
        signal.signal(signal.SIGWINCH, previous)  # type: ignore[arg-type]


__all__ = [
    "DEFAULT_SIZE",
    "Screen",
    "Size",
    "managed_screen",
    "read_key",
    "supports_tui",
    "terminal_size",
]
