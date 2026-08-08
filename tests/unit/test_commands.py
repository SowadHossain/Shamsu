"""The command registry and completion.

One table feeds `/help`, the dropdown, and the dispatcher, so the property
worth pinning is that they cannot disagree: a command that can be typed is a
command that can be discovered.
"""

from __future__ import annotations

from shamsu.ui.commands import COMMANDS, common_prefix, complete, help_lines, lookup


class TestTheRegistryIsTheSingleSource:
    def test_every_command_is_reachable_by_its_own_name(self) -> None:
        for command in COMMANDS:
            assert lookup(command.name) is command

    def test_every_alias_resolves_to_its_command(self) -> None:
        for command in COMMANDS:
            for alias in command.aliases:
                assert lookup(alias) is command

    def test_no_name_or_alias_is_claimed_twice(self) -> None:
        seen: set[str] = set()
        for command in COMMANDS:
            for name in (command.name, *command.aliases):
                assert name not in seen, f"{name} is registered twice"
                seen.add(name)

    def test_help_lists_every_registered_command(self) -> None:
        """A command absent from /help is one nobody can find."""
        rendered = "\n".join(help_lines())
        for command in COMMANDS:
            assert command.name in rendered

    def test_lookup_is_case_insensitive(self) -> None:
        assert lookup("/HELP") is lookup("/help")

    def test_an_unknown_name_resolves_to_nothing(self) -> None:
        assert lookup("/frobnicate") is None


class TestCompletion:
    def test_a_bare_slash_offers_everything(self) -> None:
        assert complete("/") == COMMANDS

    def test_a_prefix_narrows(self) -> None:
        names = {command.name for command in complete("/mo")}
        assert names == {"/mode", "/model"}

    def test_matching_is_by_prefix_not_substring(self) -> None:
        """Otherwise typing 'e' would offer every command containing an e."""
        assert all(c.name.startswith("/e") or "/e" in c.aliases for c in complete("/e"))

    def test_an_alias_completes_its_command(self) -> None:
        assert lookup("/q") in complete("/q")

    def test_prose_is_never_completed(self) -> None:
        """A request that mentions a path must not open a dropdown."""
        assert complete("fix the bug in src/calc.py") == ()

    def test_the_dropdown_closes_once_an_argument_is_being_typed(self) -> None:
        """The command has been chosen; the list has nothing left to offer."""
        assert complete("/model qwen") == ()
        assert complete("/model ") == ()

    def test_nothing_matches_an_unknown_prefix(self) -> None:
        assert complete("/zzz") == ()


class TestTabCompletion:
    def test_a_single_match_completes_fully(self) -> None:
        assert common_prefix(complete("/wor")) == "/workspace"

    def test_ambiguity_completes_only_as_far_as_it_is_unambiguous(self) -> None:
        """`/s` must not silently pick /sessions over /status."""
        assert common_prefix(complete("/s")) == "/s"

    def test_a_name_that_prefixes_another_completes_to_itself(self) -> None:
        """`/mode` is a strict prefix of `/model`, so Tab can only reach `/mode`.

        Standard completion behaviour, and survivable because the dropdown
        still lists both and the arrows select between them — but it does mean
        `/model` cannot be reached by Tab alone.
        """
        assert common_prefix(complete("/mo")) == "/mode"
        assert {c.name for c in complete("/mo")} == {"/mode", "/model"}

    def test_nothing_to_complete_yields_nothing(self) -> None:
        assert common_prefix(()) == ""
