"""The interactive session.

`ui/__init__.py` records why this file exists in the shape it does: v1's REPL
was 17,411 lines in which display, input, session management, and agent control
were one object, and nothing in it could be tested without driving a terminal.
So every command here is asserted by calling a pure function, and the loop
itself is driven with a list of strings and a `StringIO`. No terminal, no
server, no model.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Sequence
from pathlib import Path

import pytest

from shamsu.interfaces.models import ModelUnavailable
from shamsu.ui.repl import Repl, Settings, handle_command


def _settings(tmp_path: Path, **kwargs: object) -> Settings:
    base = {"model_name": "qwen2.5-coder:14b", "host": "http://localhost:11434"}
    return Settings(workspace=tmp_path, **{**base, **kwargs})  # type: ignore[arg-type]


class TestCommandsAreDecisionsNotActions:
    """A command returns what should change; it never changes anything itself."""

    def test_an_unknown_command_is_reported_not_executed(self, tmp_path: Path) -> None:
        outcome = handle_command("/frobnicate", _settings(tmp_path))
        assert outcome.settings is None
        assert "unknown command" in outcome.lines[0]

    def test_help_lists_every_command_it_implements(self, tmp_path: Path) -> None:
        rendered = "\n".join(handle_command("/help", _settings(tmp_path)).lines)
        for command in ("/model", "/workspace", "/context", "/status", "/exit"):
            assert command in rendered

    def test_exit_and_quit_both_end_the_session(self, tmp_path: Path) -> None:
        assert handle_command("/exit", _settings(tmp_path)).quit is True
        assert handle_command("/quit", _settings(tmp_path)).quit is True

    def test_status_reports_what_the_next_turn_will_use(self, tmp_path: Path) -> None:
        rendered = "\n".join(handle_command("/status", _settings(tmp_path)).lines)
        assert "qwen2.5-coder:14b" in rendered
        assert str(tmp_path) in rendered
        assert "state.db" in rendered


class TestModelCommand:
    def test_bare_model_shows_the_current_one_and_the_alternatives(self, tmp_path: Path) -> None:
        outcome = handle_command("/model", _settings(tmp_path), ["a:1", "b:2"])
        rendered = "\n".join(outcome.lines)
        assert "qwen2.5-coder:14b" in rendered
        assert "a:1, b:2" in rendered
        assert outcome.settings is None, "showing is not changing"

    def test_an_unreachable_server_still_lets_you_type_a_name(self, tmp_path: Path) -> None:
        """The prompt must not be wedged by a server that is briefly down."""
        rendered = "\n".join(handle_command("/model", _settings(tmp_path), []).lines)
        assert "could not reach the server" in rendered

    def test_naming_a_model_switches_to_it(self, tmp_path: Path) -> None:
        outcome = handle_command(
            "/model mistral-nemo:12b", _settings(tmp_path), ["mistral-nemo:12b"]
        )
        assert outcome.settings is not None
        assert outcome.settings.model_name == "mistral-nemo:12b"
        assert outcome.rewire is True

    def test_an_unpulled_name_warns_but_is_still_accepted(self, tmp_path: Path) -> None:
        """The list is what is pulled here; it is not a definition of valid."""
        outcome = handle_command("/model llama3:70b", _settings(tmp_path), ["a:1"])
        assert outcome.settings is not None
        assert outcome.settings.model_name == "llama3:70b"
        assert "pull it if a run fails" in "\n".join(outcome.lines)


class TestWorkspaceCommand:
    def test_moving_to_a_real_directory_resolves_it(self, tmp_path: Path) -> None:
        target = tmp_path / "sub"
        target.mkdir()
        outcome = handle_command(f"/workspace {target}", _settings(tmp_path))
        assert outcome.settings is not None
        assert outcome.settings.workspace == target.resolve()

    def test_a_missing_directory_is_refused_without_changing_anything(self, tmp_path: Path) -> None:
        outcome = handle_command(f"/workspace {tmp_path / 'nope'}", _settings(tmp_path))
        assert outcome.settings is None
        assert "not a directory" in outcome.lines[0]

    def test_a_path_with_spaces_survives(self, tmp_path: Path) -> None:
        target = tmp_path / "New folder"
        target.mkdir()
        outcome = handle_command(f"/workspace {target}", _settings(tmp_path))
        assert outcome.settings is not None
        assert outcome.settings.workspace == target.resolve()


class TestContextCommand:
    def test_setting_a_window(self, tmp_path: Path) -> None:
        outcome = handle_command("/context 8192", _settings(tmp_path))
        assert outcome.settings is not None
        assert outcome.settings.context_tokens == 8192

    @pytest.mark.parametrize("bad", ["/context abc", "/context 0", "/context -5"])
    def test_a_bad_window_changes_nothing(self, bad: str, tmp_path: Path) -> None:
        assert handle_command(bad, _settings(tmp_path)).settings is None


class _Script:
    """Feeds the loop a fixed list of lines, then EOF."""

    def __init__(self, lines: Sequence[str]) -> None:
        self._lines = list(lines)
        self.prompts = 0

    def __call__(self, prompt: str) -> str:
        self.prompts += 1
        if not self._lines:
            raise EOFError
        return self._lines.pop(0)


def _drive(lines: Sequence[str], tmp_path: Path, **kwargs: object) -> tuple[str, Repl]:
    out = io.StringIO()
    repl = Repl(
        _settings(tmp_path),
        kwargs.pop("build_model", lambda s: _unreachable()),  # type: ignore[arg-type]
        read_line=_Script(lines),
        stream=out,
        **kwargs,  # type: ignore[arg-type]
    )
    asyncio.run(repl.run())
    return out.getvalue(), repl


def _unreachable() -> object:
    raise AssertionError("the model must not be built for a command-only session")


class TestTheLoop:
    def test_it_greets_and_exits_cleanly_on_eof(self, tmp_path: Path) -> None:
        output, _ = _drive([], tmp_path)
        assert "type a request" in output

    def test_blank_lines_are_ignored(self, tmp_path: Path) -> None:
        script = _Script(["", "   ", "/exit"])
        repl = Repl(
            _settings(tmp_path), lambda s: _unreachable(), read_line=script, stream=io.StringIO()
        )  # type: ignore[arg-type,return-value]
        asyncio.run(repl.run())
        assert script.prompts == 3

    def test_a_command_only_session_never_builds_a_model(self, tmp_path: Path) -> None:
        """Opening a session must not require the server to be up."""
        output, _ = _drive(["/help", "/status", "/exit"], tmp_path)
        assert "/model" in output

    def test_settings_changed_by_a_command_persist_to_the_next_turn(self, tmp_path: Path) -> None:
        target = tmp_path / "other"
        target.mkdir()
        _, repl = _drive([f"/workspace {target}", "/context 4096", "/exit"], tmp_path)
        assert repl.settings.workspace == target.resolve()
        assert repl.settings.context_tokens == 4096

    def test_a_real_turn_runs_and_hands_the_prompt_back(self, tmp_path: Path) -> None:
        """The whole point of a session: a turn finishes and you get to type again.

        Driven with `ScriptedModel`, which ships and always answers validly, so
        this asserts the loop rather than the model.
        """
        from shamsu.models.scripted import ScriptedModel

        out = io.StringIO()
        script = _Script(["fix the adder", "/exit"])
        repl = Repl(
            _settings(tmp_path),
            lambda settings: ScriptedModel(),
            read_line=script,
            stream=out,
        )

        assert asyncio.run(repl.run()) == 0
        assert script.prompts == 2, "the prompt must come back after a turn"

        output = out.getvalue()
        # One fact per line, not the repr of a list.
        assert "  state: " in output
        assert "['state:" not in output

    def test_exit_stops_reading(self, tmp_path: Path) -> None:
        script = _Script(["/exit", "this should never be read"])
        repl = Repl(
            _settings(tmp_path), lambda s: _unreachable(), read_line=script, stream=io.StringIO()
        )  # type: ignore[arg-type,return-value]
        assert asyncio.run(repl.run()) == 0
        assert script.prompts == 1

    def test_an_unreachable_model_does_not_end_the_session(self, tmp_path: Path) -> None:
        """The whole point of /model is being able to recover from this.

        A dead server must report itself and hand the prompt back, so the user
        can switch to a model that is actually pulled -- not drop them out of
        the session and back to the shell.
        """

        def build(settings: Settings) -> object:
            raise ModelUnavailable("could not reach Ollama at http://localhost:11434")

        out = io.StringIO()
        script = _Script(["fix the adder", "/model other:1b", "/exit"])
        repl = Repl(_settings(tmp_path), build, read_line=script, stream=out)  # type: ignore[arg-type]

        assert asyncio.run(repl.run()) == 0
        assert "could not reach Ollama" in out.getvalue()
        # It kept reading: the failing turn, then the command, then the exit.
        assert script.prompts == 3
        assert repl.settings.model_name == "other:1b"
