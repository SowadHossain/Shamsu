"""Key normalisation, and the two Windows bugs it exists to fix.

Windows and POSIX deliver the same keystroke completely differently, so both
readers normalise to `Key` names and everything above `terminal` is written
once. What can be tested without a console is the naming and the capability
checks -- which is where both of the bugs were.
"""

from __future__ import annotations

import sys

import pytest

from shamsu.ui.terminal import _CONTROL, _WINDOWS_EXTENDED, Key, decode_escape, supports_tui


class _Tty:
    def __init__(self) -> None:
        self.written: list[str] = []

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        return None


class TestEscapeSequences:
    @pytest.mark.parametrize(
        ("sequence", "expected"),
        [
            ("[A", Key.UP),
            ("[B", Key.DOWN),
            ("[C", Key.RIGHT),
            ("[D", Key.LEFT),
            ("[H", Key.HOME),
            ("[F", Key.END),
            ("[3~", Key.DELETE),
        ],
    )
    def test_csi_sequences_are_named(self, sequence: str, expected: str) -> None:
        assert decode_escape(sequence) == expected

    def test_a_bare_escape_is_the_escape_key(self) -> None:
        assert decode_escape("") == Key.ESCAPE

    def test_an_unknown_sequence_is_swallowed_not_echoed(self) -> None:
        """Emitting `[27;5u` into the buffer is worse than dropping the key."""
        assert decode_escape("[27;5u") == ""


class TestControlCharacters:
    def test_both_line_endings_mean_enter(self) -> None:
        """CR in raw mode, LF through a cooked stream; same key."""
        assert _CONTROL["\r"] == Key.ENTER
        assert _CONTROL["\n"] == Key.ENTER

    def test_both_backspace_codes_are_backspace(self) -> None:
        """Windows sends 0x08 and POSIX sends 0x7f for the same physical key."""
        assert _CONTROL["\x08"] == Key.BACKSPACE
        assert _CONTROL["\x7f"] == Key.BACKSPACE

    def test_interrupt_and_end_of_input_are_distinguishable(self) -> None:
        assert _CONTROL["\x03"] == Key.CTRL_C
        assert _CONTROL["\x04"] == Key.CTRL_D


class TestWindowsExtendedKeys:
    def test_the_arrows_are_mapped(self) -> None:
        """Windows sends a lead-in byte then a letter, unrelated to the CSI one."""
        assert _WINDOWS_EXTENDED["H"] == Key.UP
        assert _WINDOWS_EXTENDED["P"] == Key.DOWN
        assert _WINDOWS_EXTENDED["K"] == Key.LEFT
        assert _WINDOWS_EXTENDED["M"] == Key.RIGHT

    def test_windows_and_posix_agree_on_the_names(self) -> None:
        """The whole point of normalising: one vocabulary above this module."""
        assert set(_WINDOWS_EXTENDED.values()) <= {
            Key.UP,
            Key.DOWN,
            Key.LEFT,
            Key.RIGHT,
            Key.HOME,
            Key.END,
            Key.DELETE,
        }


class TestSupportsTui:
    def test_a_pipe_is_never_a_tui(self) -> None:
        import io

        assert supports_tui(io.StringIO()) is False

    def test_no_color_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        assert supports_tui(_Tty()) is False  # type: ignore[arg-type]

    def test_a_dumb_terminal_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "dumb")
        assert supports_tui(_Tty()) is False  # type: ignore[arg-type]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows console behaviour")
    def test_an_unset_term_does_not_disqualify_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PowerShell and cmd set no TERM at all.

        Requiring one meant the full-screen interface could never start there
        and silently fell back to the plain path -- on the platform this is
        actually being run on.
        """
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("TERM", raising=False)

        # The remaining question is whether the console takes VT sequences,
        # which is answered by trying rather than by an environment variable.
        monkeypatch.setattr("shamsu.ui.terminal.enable_ansi", lambda stream=None: True)
        assert supports_tui(_Tty()) is True  # type: ignore[arg-type]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX behaviour")
    def test_an_unset_term_does_disqualify_posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("TERM", raising=False)
        assert supports_tui(_Tty()) is False  # type: ignore[arg-type]
