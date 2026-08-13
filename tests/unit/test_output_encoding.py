"""The display must survive a stream that cannot encode its glyphs.

`theme` already claimed the display "survives a pipe, a mono terminal,
`--no-colour`". It did not survive a stdout that cannot *encode* — on a default
Windows console (cp1252) the first activity line raised
`UnicodeEncodeError: 'charmap' codec can't encode character '▸'` and took
down a run whose work had already finished.
"""

from __future__ import annotations

import io

import pytest

from shamsu.ui.theme import (
    ASCII_GLYPH,
    LEVEL_GLYPH,
    activity_line,
    fit_encoding,
    supports_unicode,
)
from shamsu.ui.view import Level


class _Console(io.StringIO):
    """A text stream that declares a byte encoding, as a real console does."""

    def __init__(self, encoding: str) -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        return self._encoding


class TestDetection:
    def test_a_utf8_stream_takes_the_real_glyphs(self) -> None:
        assert supports_unicode(_Console("utf-8")) is True

    def test_a_cp1252_console_does_not(self) -> None:
        assert supports_unicode(_Console("cp1252")) is False

    def test_plain_ascii_does_not(self) -> None:
        assert supports_unicode(_Console("ascii")) is False

    def test_a_stream_with_no_encoding_is_text_and_always_fine(self) -> None:
        """A StringIO holds text; it cannot raise UnicodeEncodeError on write."""
        assert supports_unicode(io.StringIO()) is True

    def test_an_unknown_encoding_name_is_not_a_crash(self) -> None:
        assert supports_unicode(_Console("not-a-real-codec")) is False


class TestGlyphFallback:
    def test_every_level_has_an_ascii_substitute(self) -> None:
        assert set(ASCII_GLYPH) == set(LEVEL_GLYPH)

    def test_the_substitutes_are_encodable_everywhere(self) -> None:
        "".join(ASCII_GLYPH.values()).encode("ascii")

    def test_the_substitutes_stay_distinct(self) -> None:
        """A fallback that maps two meanings onto one character loses one."""
        assert len(set(ASCII_GLYPH.values())) == len(ASCII_GLYPH)

    @pytest.mark.parametrize("level", sorted(LEVEL_GLYPH))
    def test_an_ascii_line_is_writable_to_a_cp1252_console(self, level: str) -> None:
        line = activity_line(level, "plan", "something", colour=False, unicode=False)
        line.encode("cp1252")

    def test_the_unicode_line_is_still_the_default(self) -> None:
        line = activity_line(Level.STEP, "plan", "", colour=False)
        assert LEVEL_GLYPH[Level.STEP] in line


class TestFitEncoding:
    def test_encodable_text_is_untouched(self) -> None:
        assert fit_encoding("plain text", _Console("cp1252")) == "plain text"

    def test_unencodable_text_degrades_rather_than_raising(self) -> None:
        fitted = fit_encoding("verified ✓", _Console("cp1252"))
        assert "verified" in fitted
        fitted.encode("cp1252")

    def test_utf8_keeps_everything(self) -> None:
        assert fit_encoding("verified ✓", _Console("utf-8")) == "verified ✓"

    def test_a_textual_stream_is_untouched(self) -> None:
        assert fit_encoding("verified ✓", io.StringIO()) == "verified ✓"


class TestTheRunSurvives:
    def test_a_full_report_writes_to_a_cp1252_console(self) -> None:
        """The end-to-end property: no exception reaches the caller."""
        console = _Console("cp1252")
        report = "Task: x\n\nSteps:\n  [✓] 1. Inspect\n  [✗] 2. Patch — missing\n"
        console.write(fit_encoding(report, console))

        written = console.getvalue()
        assert "Inspect" in written
        written.encode("cp1252")
