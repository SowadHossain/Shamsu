"""A one-line editor with history.

`input()` on Windows has no readline behind it: no history, no word deletion,
nothing but backspace. For a tool whose input is a sentence rather than a
command that is genuinely painful, and retyping a request to change one word is
the most common thing anyone does in a session.

**The editing is a pure state machine.** `Buffer` holds text, a cursor, and a
history, and `Buffer.press(key)` returns a new state. It performs no I/O and
imports nothing from the terminal, so every editing rule below is tested by
calling a function -- the lesson from `ui/__init__.py`, applied to input rather
than to display.

`prompt()` is the only part that touches a terminal: it reads keys, feeds them
to the buffer, and redraws one line.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TextIO

from shamsu.ui.terminal import Key, read_key
from shamsu.ui.theme import RESET

#: Returned by `press` when the line is finished. `None` means "still editing";
#: a string means "this is the line"; `CANCELLED` and `EOF` are the two ways a
#: line can end without producing one.
CANCELLED = "\x00cancelled"
EOF = "\x00eof"

_WORD_BREAK = " \t/\\.,:;()[]{}\"'"


@dataclass(frozen=True)
class Buffer:
    """The state of one line being edited."""

    text: str = ""
    cursor: int = 0
    history: tuple[str, ...] = ()

    #: Where in history the user has scrolled to. `len(history)` means "not in
    #: history, editing a fresh line" -- an index rather than a flag so that
    #: moving up and back down lands exactly where it started.
    position: int = field(default=0)

    #: What was being typed before the user started scrolling, so coming back
    #: down restores it rather than leaving a stale history entry in the line.
    pending: str = ""

    @classmethod
    def new(cls, history: tuple[str, ...] = ()) -> Buffer:
        return cls(history=history, position=len(history))

    def press(self, key: str) -> tuple[Buffer, str | None]:
        """Apply one keypress. Returns the new buffer and any finished line."""
        if key == Key.ENTER:
            return self, self.text

        if key == Key.CTRL_C:
            return replace(self, text="", cursor=0), CANCELLED

        if key == Key.CTRL_D:
            # Only when the line is empty, matching every shell: Ctrl-D with
            # text in the buffer is a forward delete, not "close the session".
            if not self.text:
                return self, EOF
            return self._delete_forward(), None

        handler = _KEYS.get(key)
        if handler is not None:
            return handler(self), None

        if len(key) == 1 and key.isprintable():
            return self._insert(key), None

        return self, None

    # -- editing -----------------------------------------------------------

    def _insert(self, text: str) -> Buffer:
        return replace(
            self,
            text=self.text[: self.cursor] + text + self.text[self.cursor :],
            cursor=self.cursor + len(text),
        )

    def _backspace(self) -> Buffer:
        if self.cursor == 0:
            return self
        return replace(
            self,
            text=self.text[: self.cursor - 1] + self.text[self.cursor :],
            cursor=self.cursor - 1,
        )

    def _delete_forward(self) -> Buffer:
        if self.cursor >= len(self.text):
            return self
        return replace(self, text=self.text[: self.cursor] + self.text[self.cursor + 1 :])

    def _delete_word(self) -> Buffer:
        """Ctrl-W: back to the start of the word under the cursor.

        Skips any run of separators first, so deleting after a trailing space
        removes the word rather than only the space.
        """
        index = self.cursor
        while index > 0 and self.text[index - 1] in _WORD_BREAK:
            index -= 1
        while index > 0 and self.text[index - 1] not in _WORD_BREAK:
            index -= 1
        return replace(self, text=self.text[:index] + self.text[self.cursor :], cursor=index)

    def _kill_to_start(self) -> Buffer:
        return replace(self, text=self.text[self.cursor :], cursor=0)

    def _kill_to_end(self) -> Buffer:
        return replace(self, text=self.text[: self.cursor])

    def _left(self) -> Buffer:
        return replace(self, cursor=max(0, self.cursor - 1))

    def _right(self) -> Buffer:
        return replace(self, cursor=min(len(self.text), self.cursor + 1))

    def _home(self) -> Buffer:
        return replace(self, cursor=0)

    def _end(self) -> Buffer:
        return replace(self, cursor=len(self.text))

    # -- history -----------------------------------------------------------

    def _earlier(self) -> Buffer:
        if self.position == 0 or not self.history:
            return self
        pending = self.text if self.position == len(self.history) else self.pending
        position = self.position - 1
        text = self.history[position]
        return replace(self, text=text, cursor=len(text), position=position, pending=pending)

    def _later(self) -> Buffer:
        if self.position >= len(self.history):
            return self
        position = self.position + 1
        text = self.pending if position == len(self.history) else self.history[position]
        return replace(self, text=text, cursor=len(text), position=position)


#: Key -> the unbound method that handles it. Unbound so the table is built
#: once at import rather than per keystroke, and so adding a binding is one
#: line next to the method it calls.
_KEYS: dict[str, Callable[[Buffer], Buffer]] = {
    Key.BACKSPACE: Buffer._backspace,
    Key.DELETE: Buffer._delete_forward,
    Key.LEFT: Buffer._left,
    Key.RIGHT: Buffer._right,
    Key.HOME: Buffer._home,
    Key.END: Buffer._end,
    Key.UP: Buffer._earlier,
    Key.DOWN: Buffer._later,
    Key.CTRL_A: Buffer._home,
    Key.CTRL_E: Buffer._end,
    Key.CTRL_U: Buffer._kill_to_start,
    Key.CTRL_K: Buffer._kill_to_end,
    Key.CTRL_W: Buffer._delete_word,
}


class History:
    """Recent lines, most recent last, without adjacent duplicates."""

    def __init__(self, limit: int = 200) -> None:
        self._lines: list[str] = []
        self._limit = limit

    def add(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        if self._lines and self._lines[-1] == stripped:
            return
        self._lines.append(stripped)
        if len(self._lines) > self._limit:
            del self._lines[: len(self._lines) - self._limit]

    def entries(self) -> tuple[str, ...]:
        return tuple(self._lines)


def render_line(prompt: str, buffer: Buffer, *, width: int = 0) -> str:
    """The escape sequence that redraws the input line and places the cursor.

    Pure, so what appears on screen is assertable without a terminal. `\\r` and
    an erase-to-end rather than a clear: the line is rewritten in place on every
    keystroke, and clearing first makes it flicker.
    """
    del width
    return f"\r\x1b[K{prompt}{buffer.text}\r\x1b[{_columns(prompt, buffer)}C"


def _columns(prompt: str, buffer: Buffer) -> int:
    return _visible(prompt) + buffer.cursor


def _visible(text: str) -> int:
    """Length ignoring escape sequences, which occupy no columns."""
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


def prompt(
    text: str,
    history: History | None = None,
    *,
    stream: TextIO | None = None,
) -> str:
    """Read one edited line. Raises `EOFError` on Ctrl-D, `KeyboardInterrupt` on Ctrl-C.

    Matches `input()`'s contract exactly, so it drops into the session in place
    of it and every caller keeps working.
    """
    import sys

    target = stream or sys.stdout
    store = history or History()
    buffer = Buffer.new(store.entries())

    target.write(text)
    target.flush()

    while True:
        key = read_key(0.05)
        if not key:
            continue

        buffer, finished = buffer.press(key)

        if finished == CANCELLED:
            target.write(RESET + "\n")
            target.flush()
            raise KeyboardInterrupt
        if finished == EOF:
            target.write(RESET + "\n")
            target.flush()
            raise EOFError
        if finished is not None:
            target.write(RESET + "\n")
            target.flush()
            store.add(finished)
            return finished

        target.write(render_line(text, buffer))
        target.flush()


__all__ = ["CANCELLED", "EOF", "Buffer", "History", "prompt", "render_line"]
