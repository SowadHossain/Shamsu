"""`@` file references.

Both halves are pure: finding the fragment under the cursor, and ranking the
paths that match it. So the rules are asserted by calling functions, with no
workspace on disk and no terminal.
"""

from __future__ import annotations

import pytest

from shamsu.ui.commands import file_fragment, match_files

PATHS = (
    "calc.py",
    "src/shamsu/ui/theme.py",
    "src/shamsu/ui/terminal.py",
    "src/shamsu/models/ollama.py",
    "tests/unit/test_theme.py",
    "docs/theme-notes.md",
)


class TestFindingTheFragment:
    def test_a_reference_at_the_end_is_found(self) -> None:
        assert file_fragment("look at @calc", 13) == (8, "calc")

    def test_a_bare_at_offers_everything(self) -> None:
        assert file_fragment("@", 1) == (0, "")

    def test_a_reference_must_start_a_word(self) -> None:
        """Otherwise an email address opens a file picker mid-sentence."""
        assert file_fragment("mail me at bob@example.com", 26) is None

    def test_text_with_no_at_has_no_fragment(self) -> None:
        assert file_fragment("fix the add function", 20) is None

    def test_a_completed_reference_is_no_longer_being_typed(self) -> None:
        """The space ended it; the cursor is in prose again."""
        assert file_fragment("@calc.py and then", 17) is None

    def test_only_the_reference_under_the_cursor_counts(self) -> None:
        text = "@one.py and @two.py"
        assert file_fragment(text, 19) == (12, "two.py")

    def test_a_cursor_before_the_at_sees_nothing(self) -> None:
        assert file_fragment("look at @calc", 4) is None


class TestRankingMatches:
    def test_an_empty_fragment_offers_a_stable_sample(self) -> None:
        assert match_files("", PATHS, limit=3) == tuple(sorted(PATHS)[:3])

    def test_a_filename_prefix_ranks_first(self) -> None:
        """`theme` should offer theme.py before test_theme.py or a path hit."""
        assert match_files("theme", PATHS)[0] == "src/shamsu/ui/theme.py"

    def test_a_filename_hit_beats_a_path_hit(self) -> None:
        ranked = match_files("ui", PATHS)
        assert ranked, "a path containing 'ui' should match"
        assert all("ui" in path for path in ranked)

    def test_matching_ignores_case(self) -> None:
        assert match_files("CALC", PATHS) == ("calc.py",)

    def test_nothing_matches_a_string_that_is_not_there(self) -> None:
        assert match_files("zzz", PATHS) == ()

    def test_the_list_is_bounded(self) -> None:
        assert len(match_files("", PATHS, limit=2)) == 2

    def test_it_is_a_substring_match_not_a_subsequence_one(self) -> None:
        """A subsequence matcher turns 'tp' into a match for 'theme.py' — noise."""
        assert match_files("tp", PATHS) == ()

    @pytest.mark.parametrize("fragment", ["calc.py", "calc"])
    def test_a_full_or_partial_name_both_find_it(self, fragment: str) -> None:
        assert "calc.py" in match_files(fragment, PATHS)
