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

_WINDOWS = sys.platform == "win32"


class Key:
    """Key names, so callers never match on raw bytes.

    Windows and POSIX deliver the same keystroke completely differently -- an
    arrow is `\\x1b[A` on one and a `\\xe0` prefix plus `H` on the other -- so
    both readers normalise to these names and everything above this module is
    written once. A printable character is returned as itself.
    """

    UP = "up"
    DOWN = "down"
    LEFT = "left"
    RIGHT = "right"
    HOME = "home"
    END = "end"
    DELETE = "delete"
    BACKSPACE = "backspace"
    ENTER = "enter"
    TAB = "tab"
    ESCAPE = "escape"

    CTRL_A = "ctrl-a"
    CTRL_C = "ctrl-c"
    CTRL_D = "ctrl-d"
    CTRL_E = "ctrl-e"
    CTRL_K = "ctrl-k"
    CTRL_L = "ctrl-l"
    CTRL_P = "ctrl-p"
    CTRL_U = "ctrl-u"
    CTRL_W = "ctrl-w"
    CTRL_X = "ctrl-x"


#: Control bytes that carry a name. Enter arrives as CR in raw mode and as LF
#: through a cooked stream; both mean the same thing to a single-line editor.
_CONTROL: dict[str, str] = {
    "\r": Key.ENTER,
    "\n": Key.ENTER,
    "\t": Key.TAB,
    "\x7f": Key.BACKSPACE,
    "\x08": Key.BACKSPACE,
    "\x01": Key.CTRL_A,
    "\x03": Key.CTRL_C,
    "\x04": Key.CTRL_D,
    "\x05": Key.CTRL_E,
    "\x0b": Key.CTRL_K,
    "\x0c": Key.CTRL_L,
    "\x10": Key.CTRL_P,
    "\x15": Key.CTRL_U,
    "\x17": Key.CTRL_W,
    "\x18": Key.CTRL_X,
}

#: The tail of a CSI sequence, as POSIX terminals send it.
_CSI: dict[str, str] = {
    "A": Key.UP,
    "B": Key.DOWN,
    "C": Key.RIGHT,
    "D": Key.LEFT,
    "H": Key.HOME,
    "F": Key.END,
    "3~": Key.DELETE,
    "1~": Key.HOME,
    "4~": Key.END,
}

#: The second byte Windows sends after a `\x00` or `\xe0` lead-in.
_WINDOWS_EXTENDED: dict[str, str] = {
    "H": Key.UP,
    "P": Key.DOWN,
    "K": Key.LEFT,
    "M": Key.RIGHT,
    "G": Key.HOME,
    "O": Key.END,
    "S": Key.DELETE,
}


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

    A TTY, not `TERM=dumb`, and `NO_COLOR` unset -- conventions users expect
    honoured, and ignoring any of them puts escape codes in someone's log.

    **An unset `TERM` is only disqualifying on POSIX.** Windows consoles do not
    set it at all: `TERM` is empty in both PowerShell and cmd, so requiring it
    meant the full-screen interface could never start there, and silently took
    the plain path instead. On Windows the capability question is whether the
    console accepts virtual-terminal sequences, which `enable_ansi` answers by
    trying.
    """
    target = stream or sys.stdout
    if not hasattr(target, "isatty") or not target.isatty():
        return False
    if "NO_COLOR" in os.environ:
        return False

    term = os.environ.get("TERM", "").lower()
    if term == "dumb":
        return False
    if _WINDOWS:
        return enable_ansi(target)
    return term != ""


def enable_ansi(stream: TextIO | None = None) -> bool:
    """Turn on virtual-terminal processing for a Windows console.

    Windows Terminal enables it already; conhost does not, and without it every
    escape sequence is printed literally. Returns whether escapes can be used.
    Always true off Windows, where nothing needs enabling.
    """
    if not _WINDOWS:
        return True

    import ctypes

    target = stream or sys.stdout
    try:
        handle = ctypes.windll.kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_virtual_terminal = 0x0004
        if mode.value & enable_virtual_terminal:
            return True
        return bool(
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | enable_virtual_terminal)
        )
    except (AttributeError, OSError, ValueError):  # pragma: no cover - not a console
        return False
    finally:
        del target


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

    def place_cursor(self, row: int, column: int, *, visible: bool = True) -> None:
        """Leave the cursor at a one-indexed position, optionally showing it.

        A full-screen display normally hides the cursor, because it would sit
        wherever the last write finished. An interface with an input line wants
        the opposite: the terminal's own cursor is what marks the typing
        position, which costs no column arithmetic and handles wide characters
        for free.
        """
        self._stream.write(f"\x1b[{max(1, row)};{max(1, column)}H")
        self._stream.write(_CURSOR_SHOW if visible else _CURSOR_HIDE)
        self._stream.flush()


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
    """One keypress as a `Key` name or a character, or "" on timeout.

    Non-blocking so the repaint loop keeps running: an interface that only
    redraws when a key is pressed looks frozen during the thirty seconds a
    local model spends thinking.

    Two readers, because there is no shared mechanism. POSIX selects on stdin;
    **Windows cannot** -- `select` there accepts sockets only, and calling it on
    stdin raises `WinError 10093`. This module used to do exactly that, which
    meant every keypress on Windows was silently dropped: no cancel, no dismiss,
    no keys at all.
    """
    return _read_key_windows(timeout) if _WINDOWS else _read_key_posix(timeout)


def _read_key_windows(timeout: float) -> str:
    """Poll the console buffer, since Windows cannot select on stdin.

    `kbhit` is a poll rather than a wait, so the timeout is spent sleeping in
    short slices. The slice is small enough to feel immediate and long enough
    not to spin a core.
    """
    import msvcrt
    import time

    deadline = time.monotonic() + timeout
    while True:
        if msvcrt.kbhit():
            break
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.005)

    char = msvcrt.getwch()

    # Special keys arrive as two reads: a lead-in byte, then the identity.
    if char in ("\x00", "\xe0"):
        return _WINDOWS_EXTENDED.get(msvcrt.getwch(), "")

    return _CONTROL.get(char, char)


def _read_key_posix(timeout: float) -> str:
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
        return _CONTROL.get(char, char)

    # An escape sequence: read the rest without blocking, so a lone Escape key
    # is not mistaken for the start of an arrow key that never arrives.
    sequence = ""
    while True:
        ready, _, _ = select.select([sys.stdin], [], [], 0.01)
        if not ready:
            break
        sequence += os.read(sys.stdin.fileno(), 1).decode("utf-8", "ignore")

    return decode_escape(sequence)


def decode_escape(sequence: str) -> str:
    """Name the key an escape sequence stands for.

    Split out from the reader because it is the only interesting part and the
    reader needs a terminal. An unrecognised sequence is swallowed rather than
    returned raw: emitting `[27;5u` into a text buffer because a terminal sent
    something unexpected is worse than ignoring the keystroke.
    """
    if not sequence:
        return Key.ESCAPE
    if sequence[0] != "[":
        return Key.ESCAPE
    return _CSI.get(sequence[1:], "")


# -- platform details ------------------------------------------------------


if sys.platform == "win32":
    # Dispatched at definition rather than inside the body. A `sys.platform`
    # branch *within* a function leaves the POSIX half statically unreachable on
    # Windows, which `warn_unreachable` reports -- correctly. Splitting the
    # definitions means each platform type-checks only the code it actually runs.

    def _raw_mode(stream: TextIO) -> Any | None:
        """Nothing to change: `msvcrt` reads the console directly."""
        del stream
        return None

    def _restore_mode(stream: TextIO, original: Any | None) -> None:
        """Nothing was changed, so there is nothing to put back."""
        del stream, original

else:

    def _raw_mode(stream: TextIO) -> Any | None:
        """Put the terminal in cbreak mode, returning the settings to restore.

        cbreak rather than full raw: signals stay enabled, so Ctrl-C still
        raises `KeyboardInterrupt` and the run's real cancellation path is used
        instead of a key handler reimplementing it.
        """
        import termios
        import tty

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

        import termios

        # Restoring is best-effort by necessity: if it fails there is nothing
        # further to try, and raising here would mask the real exception that
        # sent us into the `finally`.
        with contextlib.suppress(Exception):
            termios.tcsetattr(stream.fileno(), termios.TCSADRAIN, original)


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
    "Key",
    "Screen",
    "Size",
    "decode_escape",
    "enable_ansi",
    "managed_screen",
    "read_key",
    "supports_tui",
    "terminal_size",
]
