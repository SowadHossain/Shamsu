"""Suggestions for a command's argument, once the command has been chosen.

`complete` deliberately stops at the space. This is what happens after it:
where the valid set is known — two modes, two theme states, whatever Ollama has
pulled — the dropdown offers it rather than leaving the user to remember.

Pure, like the rest of `commands`, so the dynamic values arrive as arguments
and no test needs a server.
"""

from __future__ import annotations

import pytest

from shamsu.ui.commands import COMMANDS, allowed_values, complete_argument, lookup

MODELS = ("qwen2.5-coder:14b", "qwen2.5-coder:7b-instruct", "mistral-nemo:12b")
PATHS = ("F:/Work/demo-1", "F:/Work/demo-2", "F:/Work/other")


class TestFixedValues:
    def test_mode_offers_both_modes(self) -> None:
        assert complete_argument("/mode ") == ("build", "plan")

    def test_it_narrows_as_the_value_is_typed(self) -> None:
        assert complete_argument("/mode p") == ("plan",)

    def test_theme_offers_on_and_off(self) -> None:
        assert complete_argument("/theme ") == ("on", "off")

    def test_an_alias_completes_the_same_values(self) -> None:
        assert complete_argument("/themes ") == ("on", "off")

    def test_nothing_matches_a_value_that_is_not_offered(self) -> None:
        assert complete_argument("/mode zzz") == ()


class TestDynamicValues:
    def test_model_offers_what_the_server_has_pulled(self) -> None:
        assert complete_argument("/model ", models=MODELS) == MODELS

    def test_it_narrows_by_prefix(self) -> None:
        assert complete_argument("/model mist", models=MODELS) == ("mistral-nemo:12b",)

    def test_an_unreachable_server_offers_nothing_rather_than_failing(self) -> None:
        """The prompt must not break because Ollama is down."""
        assert complete_argument("/model ", models=()) == ()

    def test_workspace_offers_directories(self) -> None:
        assert complete_argument("/workspace ", paths=PATHS) == PATHS

    def test_values_are_not_crossed_between_commands(self) -> None:
        """A model list must not turn up when completing a path."""
        assert complete_argument("/workspace ", models=MODELS) == ()

    def test_the_list_is_bounded(self) -> None:
        many = tuple(f"model-{index}" for index in range(50))
        assert len(complete_argument("/model ", models=many)) == 8


class TestWhenItDoesNotApply:
    def test_a_command_with_no_argument_offers_nothing(self) -> None:
        assert complete_argument("/help ") == ()

    def test_a_command_still_being_named_is_left_to_complete(self) -> None:
        """`complete` owns that half; offering values too would show both."""
        assert complete_argument("/mod") == ()

    def test_prose_is_never_completed(self) -> None:
        assert complete_argument("fix the bug in mode plan") == ()

    def test_an_unknown_command_offers_nothing(self) -> None:
        assert complete_argument("/frobnicate ") == ()

    def test_a_second_argument_is_not_guessed_at(self) -> None:
        """Nothing takes one; offering values for a position that does not exist."""
        assert complete_argument("/mode plan extra") == ()

    def test_context_has_no_completable_values(self) -> None:
        """A token count is a number, not a choice."""
        assert complete_argument("/context ") == ()


class TestHandlersAndDropdownAgree:
    """The registry is the single source; a second copy would drift."""

    @pytest.mark.parametrize("name", ["/mode", "/theme"])
    def test_a_commands_values_are_reachable_by_name(self, name: str) -> None:
        assert allowed_values(name) == (lookup(name).values if lookup(name) else ())

    def test_an_unknown_name_yields_no_values(self) -> None:
        assert allowed_values("/frobnicate") == ()

    def test_every_command_with_a_value_list_declares_it_in_its_usage(self) -> None:
        """`/mode [build|plan]` must not promise something `values` omits."""
        for command in COMMANDS:
            for value in command.values:
                assert value in command.argument, f"{command.name} hides {value}"

    def test_a_command_declaring_a_source_takes_an_argument(self) -> None:
        for command in COMMANDS:
            if command.source:
                assert command.argument, f"{command.name} has a source but no argument"
