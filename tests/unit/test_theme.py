"""Colour, and the rules that keep it honest.

Two properties matter more than which hue anything is. Colour must never be the
*only* carrier of a state, so the display survives a pipe, a mono terminal and a
reader who cannot separate red from green. And escapes must never reach
something that is not a terminal, because a run piped into a file should produce
text a grep can read.
"""

from __future__ import annotations

import io

from shamsu.ui.theme import (
    BLUE,
    GREEN,
    LEVEL_COLOUR,
    LEVEL_GLYPH,
    RED,
    RESET,
    activity_line,
    paint,
    supports_colour,
)
from shamsu.ui.view import Level


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestColourIsNeverTheOnlySignal:
    def test_every_level_has_a_glyph_as_well_as_a_colour(self) -> None:
        assert set(LEVEL_COLOUR) == set(LEVEL_GLYPH)

    def test_the_glyphs_distinguish_the_levels_that_matter(self) -> None:
        """Success and failure must not share a glyph."""
        assert LEVEL_GLYPH[Level.OK] != LEVEL_GLYPH[Level.FAIL]

    def test_a_line_without_colour_still_says_what_happened(self) -> None:
        plain = activity_line(Level.FAIL, "verify", "test.run  pytest", colour=False)
        assert LEVEL_GLYPH[Level.FAIL] in plain
        assert "verify" in plain
        assert "\x1b[" not in plain


class TestPaint:
    def test_painting_wraps_and_resets(self) -> None:
        assert paint("x", GREEN) == f"{GREEN}x{RESET}"

    def test_painting_is_a_no_op_when_disabled(self) -> None:
        assert paint("x", GREEN, False) == "x"

    def test_empty_text_is_left_alone(self) -> None:
        """Wrapping nothing emits escapes that occupy width but show nothing."""
        assert paint("", GREEN) == ""


class TestWhenColourIsUsed:
    def test_not_written_to_a_pipe(self, monkeypatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        assert supports_colour(io.StringIO()) is False

    def test_written_to_a_terminal(self, monkeypatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")
        assert supports_colour(_Tty()) is True

    def test_no_color_is_honoured(self, monkeypatch) -> None:
        monkeypatch.setenv("NO_COLOR", "1")
        assert supports_colour(_Tty()) is False

    def test_a_dumb_terminal_is_honoured(self, monkeypatch) -> None:
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "dumb")
        assert supports_colour(_Tty()) is False


class TestActivityLine:
    def test_a_tool_name_is_coloured_as_a_tool(self) -> None:
        """Blue means 'tool' — the only things that can produce evidence."""
        line = activity_line(Level.OK, "author", "file.patch  calc.py  0.2s", colour=True)
        assert f"{BLUE}file.patch{RESET}" in line

    def test_verified_evidence_reads_green(self) -> None:
        line = activity_line(Level.OK, "verify", "test.run  pytest  ✓ tests_passed", colour=True)
        assert f"{GREEN}✓ tests_passed{RESET}" in line

    def test_an_error_reads_red(self) -> None:
        line = activity_line(Level.FAIL, "author", "file.patch  calc.py  — no match", colour=True)
        assert f"{RED}— no match{RESET}" in line

    def test_prose_detail_is_not_carved_up(self) -> None:
        """Only tool-shaped details are split; a sentence is left alone."""
        line = activity_line(Level.NOTE, "started", "fix the add function", colour=True)
        assert "fix the add function" in line

    def test_labels_are_padded_so_details_line_up(self) -> None:
        short = activity_line(Level.OK, "plan", "a.b  x", colour=False)
        long = activity_line(Level.OK, "author", "a.b  x", colour=False)
        assert short.index("a.b") == long.index("a.b")
