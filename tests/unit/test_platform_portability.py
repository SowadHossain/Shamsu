"""Three assumptions about paths that only Windows disproved.

They accounted for every remaining failure in the suite — 26 of them, looking
like a platform quirk and each in fact a product bug that made SHAMSU behave
differently, and worse, on the machine it is actually run on.

1. **A key is not a filename.** A symbol key is `path::symbol`, `:` is illegal
   in a Windows filename, and `OSError: [Errno 22]` meant *no symbol card
   could ever be written* — 24 tests, one missing `sub()`.
2. **A traceback frame is not a portable path.** Windows tracebacks say
   `tests\\test_calc.py`; every other path in the system uses `/`, so the
   implicated file matched nothing and the repair scope excluded the very file
   the failure named.
3. **Text mode rewrites what you give it.** `write_text` translates `\\n` to
   `os.linesep`, so every file the agent wrote came out CRLF — turning a
   one-line edit into a whole-file diff in an LF repository.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.artifacts.registry import content_filename
from shamsu.interfaces.enums import ArtifactKind
from shamsu.tools.editing import PatchUndo, _newline_of
from shamsu.verification.failure import _files_from_frames

#: Everything Windows refuses in a filename, plus the reserved device names.
ILLEGAL = '<>:"|?*'


class TestAnArtifactKeyBecomesALegalFilename:
    def test_a_symbol_key_loses_its_colons(self) -> None:
        """The bug itself: `src/auth.py::login` could not be written at all."""
        name = content_filename(ArtifactKind.SYMBOL_CARD, "src/auth.py::login")
        assert ":" not in name

    @pytest.mark.parametrize("char", list(ILLEGAL))
    def test_no_illegal_character_survives(self, char: str) -> None:
        name = content_filename(ArtifactKind.SYMBOL_CARD, f"a{char}b")
        assert char not in name.removeprefix("symbols/")

    def test_a_plain_module_key_is_left_readable(self) -> None:
        """The documented form still holds where nothing had to change."""
        assert (
            content_filename(ArtifactKind.MODULE_CARD, "apps/api/auth.py")
            == "modules/apps__api__auth.py.md"
        )

    def test_sanitising_cannot_collide_two_keys(self) -> None:
        """`a:b` and `a_b` both map to `a_b`; one card would overwrite the other.

        Silent overwrite is worse than the crash it replaced, so an altered key
        carries a digest of the original.
        """
        assert content_filename(ArtifactKind.SYMBOL_CARD, "a:b") != content_filename(
            ArtifactKind.SYMBOL_CARD, "a_b"
        )

    def test_two_altered_keys_stay_distinct(self) -> None:
        assert content_filename(ArtifactKind.SYMBOL_CARD, "a:b") != content_filename(
            ArtifactKind.SYMBOL_CARD, "a?b"
        )

    def test_the_same_key_always_gives_the_same_name(self) -> None:
        """Stable across runs, or every pass regenerates every artifact."""
        key = "src/auth.py::login"
        assert content_filename(ArtifactKind.SYMBOL_CARD, key) == content_filename(
            ArtifactKind.SYMBOL_CARD, key
        )

    def test_a_reserved_device_name_is_escaped(self) -> None:
        """`NUL.md` is still the null device — the extension does not save it."""
        name = content_filename(ArtifactKind.MODULE_CARD, "NUL")
        assert name != "modules/NUL.md"

    def test_an_over_long_key_is_truncated_and_stays_unique(self) -> None:
        first = content_filename(ArtifactKind.SYMBOL_CARD, "x" * 300 + "::a")
        second = content_filename(ArtifactKind.SYMBOL_CARD, "x" * 300 + "::b")
        assert len(Path(first).name) < 200
        assert first != second

    def test_an_empty_key_still_names_something(self) -> None:
        assert content_filename(ArtifactKind.MODULE_CARD, "").endswith(".md")


class TestImplicatedFilesArePosix:
    def test_a_windows_frame_is_normalised(self) -> None:
        """These build the WriteScope a repair is confined to."""
        assert _files_from_frames((r"tests\test_calc.py:12",)) == ("tests/test_calc.py",)

    def test_a_posix_frame_is_untouched(self) -> None:
        assert _files_from_frames(("tests/test_calc.py:12",)) == ("tests/test_calc.py",)

    def test_the_two_spellings_deduplicate_to_one(self) -> None:
        frames = (r"tests\test_calc.py:12", "tests/test_calc.py:40")
        assert _files_from_frames(frames) == ("tests/test_calc.py",)


class TestLineEndingsAreNotRewritten:
    def test_an_lf_file_is_detected(self) -> None:
        assert _newline_of(b"a\nb\nc\n") == "\n"

    def test_a_crlf_file_is_detected(self) -> None:
        assert _newline_of(b"a\r\nb\r\nc\r\n") == "\r\n"

    def test_a_mixed_file_follows_the_majority(self) -> None:
        assert _newline_of(b"a\r\nb\r\nc\n") == "\r\n"

    def test_a_file_with_no_newline_defaults_to_lf(self) -> None:
        """No convention to preserve, and source is written in LF."""
        assert _newline_of(b"single line") == "\n"

    def test_a_rollback_restores_the_original_endings(self, tmp_path: Path) -> None:
        """`previous_content` is LF in memory; restoring blind would reformat."""
        undo = PatchUndo(path="calc.py", previous_content="a\nb\n", existed=True, newline="\r\n")
        undo.apply(tmp_path)
        assert (tmp_path / "calc.py").read_bytes() == b"a\r\nb\r\n"

    def test_a_rollback_to_absence_still_deletes(self, tmp_path: Path) -> None:
        (tmp_path / "new.py").write_text("x = 1\n", encoding="utf-8")
        PatchUndo(path="new.py", previous_content="", existed=False).apply(tmp_path)
        assert not (tmp_path / "new.py").exists()
