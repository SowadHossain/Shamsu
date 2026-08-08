"""The key loop of the full-screen session.

`_press` is synchronous and returns what should happen, so the whole input
layer — dropdown navigation, Tab completion, the `^X` leader, the palette,
`@` references — is driven by feeding it keys. No alternate screen, no console,
no model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.ui.commands import COMMANDS
from shamsu.ui.interactive import QUIT, InteractiveSession
from shamsu.ui.repl import Settings, handle_command
from shamsu.ui.terminal import Key


def _session(tmp_path: Path, paths: tuple[str, ...] = ()) -> InteractiveSession:
    settings = Settings(
        model_name="qwen2.5-coder:14b",
        host="http://localhost:11434",
        workspace=tmp_path,
    )
    session = InteractiveSession(
        settings,
        lambda _settings: pytest.fail("the model must not be built for an input test"),
        handle_command=handle_command,
    )
    # The workspace scan is cached; seeding it keeps these tests off the disk.
    session._file_cache = paths or ("calc.py", "src/theme.py", "tests/test_calc.py")
    return session


def _type(session: InteractiveSession, text: str) -> str:
    result = ""
    for char in text:
        result = session._press(char)
    return result


class TestTypingAndSubmitting:
    def test_a_request_is_returned_on_enter(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "fix the adder")
        assert session._press(Key.ENTER) == "fix the adder"

    def test_the_line_is_cleared_after_submitting(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "fix the adder")
        session._press(Key.ENTER)
        assert session.state.text == ""

    def test_a_submitted_request_joins_the_history(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "fix the adder")
        session._press(Key.ENTER)
        session._press(Key.UP)
        assert session.state.text == "fix the adder"

    def test_ctrl_d_on_an_empty_line_ends_the_session(self, tmp_path: Path) -> None:
        assert _session(tmp_path)._press(Key.CTRL_D) == QUIT

    def test_an_empty_line_runs_nothing(self, tmp_path: Path) -> None:
        assert _session(tmp_path)._press(Key.ENTER) == ""


class TestCommandDropdown:
    def test_typing_a_slash_opens_it(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "/")
        assert session.state.suggestions == COMMANDS

    def test_it_narrows_as_you_type(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "/mo")
        assert {c.name for c in session.state.suggestions} == {"/mode", "/model"}

    def test_the_arrows_move_the_selection_while_it_is_open(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "/mo")
        session._press(Key.DOWN)
        assert session.state.selected == 1

    def test_the_selection_wraps(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "/mo")
        session._press(Key.UP)
        assert session.state.selected == len(session.state.suggestions) - 1

    def test_tab_completes_to_the_shared_prefix(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "/wor")
        session._press(Key.TAB)
        assert session.state.text == "/workspace"

    def test_tab_takes_the_highlighted_entry_when_there_is_one(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "/mo")
        session._press(Key.DOWN)
        session._press(Key.TAB)
        assert session.state.text == "/model"

    def test_escape_closes_it_without_changing_the_line(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "/mo")
        session._press(Key.ESCAPE)
        assert session.state.suggestions == ()
        assert session.state.text == "/mo"

    def test_the_arrows_return_to_history_once_it_closes(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "earlier request")
        session._press(Key.ENTER)
        session._press(Key.UP)
        assert session.state.text == "earlier request"

    def test_prose_never_opens_it(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "fix src/calc.py")
        assert session.state.suggestions == ()

    def test_running_a_command_does_not_return_it_as_a_request(self, tmp_path: Path) -> None:
        """A command is handled here; it must never reach the agent as a task."""
        _type(session := _session(tmp_path), "/status")
        assert session._press(Key.ENTER) == ""

    def test_exit_ends_the_session(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "/exit")
        assert session._press(Key.ENTER) == QUIT


class TestPalette:
    def test_ctrl_p_offers_everything_from_a_blank_prompt(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        session._press(Key.CTRL_P)
        assert session.state.suggestions == COMMANDS

    def test_it_ignores_what_has_been_typed(self, tmp_path: Path) -> None:
        """Discovery, not completion: it answers "what can this do"."""
        _type(session := _session(tmp_path), "/mo")
        session._press(Key.CTRL_P)
        assert session.state.suggestions == COMMANDS

    def test_escape_closes_it(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        session._press(Key.CTRL_P)
        session._press(Key.ESCAPE)
        assert session.state.suggestions == ()


class TestLeaderKey:
    def test_ctrl_x_then_a_letter_runs_a_command(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        session._press(Key.CTRL_X)
        assert session._press("q") == QUIT

    def test_the_leader_is_shown_while_it_is_pending(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        session._press(Key.CTRL_X)
        assert "^X" in session.state.status

    def test_an_unbound_letter_cancels_rather_than_acting(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        session._press(Key.CTRL_X)
        assert session._press("z") == ""
        assert session.state.text == "", "the letter must not be typed into the line"

    def test_the_leader_does_not_persist(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        session._press(Key.CTRL_X)
        session._press("z")
        _type(session, "q")
        assert session.state.text == "q"


class TestFileReference:
    def test_an_at_opens_the_file_list(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "look at @")
        assert session.state.files == ("calc.py", "src/theme.py", "tests/test_calc.py")

    def test_it_narrows_as_you_type(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "@theme")
        assert session.state.files == ("src/theme.py",)

    def test_tab_inserts_the_chosen_path(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "look at @theme")
        session._press(Key.TAB)
        assert session.state.text == "look at @src/theme.py "

    def test_choosing_a_file_closes_the_list(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "@theme")
        session._press(Key.TAB)
        assert session.state.files == ()

    def test_commands_and_files_are_never_offered_together(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "/mo")
        assert session.state.files == ()

    def test_the_arrows_select_a_file(self, tmp_path: Path) -> None:
        _type(session := _session(tmp_path), "@")
        session._press(Key.DOWN)
        session._press(Key.TAB)
        assert "src/theme.py" in session.state.text
