"""Answering an approval INSIDE the frame.

Reported live 2026-08-24: "when it asks me for approval it takes me to the
default cli interface from the tui." It did. `reading_input()` fired
`LiveConsole.stand_down`, which called `run_in_terminal` to drop the alternate
screen and hand the raw console to a SECOND prompt_toolkit application - while
the question itself was written to the pane the user had just been taken away
from. Two applications cannot share a terminal, so the frame asks for itself.
"""
from __future__ import annotations

import threading

import pytest

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from shamsu.cli.tui import TuiApp, approval_lines


class _Record:
    approval_id = "approval-0123456789abcdef"
    action_type = "run_command"
    description = "node --check js/game.js"
    risk_level = "medium"
    preview = "cwd: /tmp/project"
    workspace = "/tmp/project"


def _telemetry():
    from shamsu.cli.live_console import TurnTelemetry

    return TurnTelemetry(unicode_ui=True)


def _app():
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        return TuiApp(telemetry=_telemetry(), on_submit=lambda _t: None)


# -- the card --------------------------------------------------------------


def test_the_card_carries_the_fields_the_terminal_panel_carries():
    text = approval_lines(_Record())

    assert "run_command" in text
    assert "node --check js/game.js" in text
    assert "medium" in text
    assert "cwd: /tmp/project" in text


def test_the_hint_names_only_the_keys_on_offer():
    """`a` meant DENY for a year because the hint and the menu disagreed."""
    assert "[a] always allow" not in approval_lines(_Record())
    assert "[a] always allow" in approval_lines(_Record(), offer_remember=True)


def test_the_question_lands_in_the_pane():
    app = _app()
    app.open_approval(_Record())

    assert "APPROVAL REQUIRED" in app.pane.plain(40)
    assert "node --check js/game.js" in app.pane.plain(40)


# -- answering -------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expected"), [("y", "y"), ("a", "a"), ("n", "n")]
)
def test_a_keypress_releases_the_waiting_tool(key, expected):
    app = _app()
    app.open_approval(_Record(), offer_remember=True)

    answered: list[str] = []
    reader = threading.Thread(target=lambda: answered.append(app.await_approval(5)))
    reader.start()
    app._answer_approval(key)
    reader.join(5)

    assert answered == [expected]
    assert not app.approval_pending()


def test_a_remote_answer_releases_the_waiter_with_no_keypress():
    """The phone answered. Nothing is pressed here, and the thread must wake.

    Without this the turn hangs for ever on a question that is already
    settled - the failure mode is worse than the one being fixed.
    """
    app = _app()
    app.open_approval(_Record())

    answered: list[str] = []
    reader = threading.Thread(target=lambda: answered.append(app.await_approval(5)))
    reader.start()
    app.close_approval("answered on telegram")
    reader.join(5)

    assert answered == [""], "released, with no answer of its own"
    assert not app.approval_pending()
    assert "answered on telegram" in app.pane.plain(40)


def test_closing_twice_is_harmless():
    app = _app()
    app.open_approval(_Record())
    app.close_approval()
    app.close_approval()
    assert not app.approval_pending()


def test_await_without_a_question_does_not_block():
    assert _app().await_approval(0.1) == ""


# -- the frame keeps the keyboard -----------------------------------------


def test_the_statusline_shouts_while_a_question_is_open():
    app = _app()
    plain = lambda: "".join(text for _style, text in app._statusline())

    assert "APPROVAL" not in plain()
    app.open_approval(_Record())
    assert "APPROVAL NEEDED" in plain()
    assert "y allow" in plain()
    assert "a always" not in plain(), "not on offer, so not on the bar"
    app.close_approval()
    assert "APPROVAL" not in plain()


def test_the_answer_keys_are_bound_only_while_asking():
    """A bare `y` must reach the input box when nothing is being asked."""
    app = _app()
    bound = [
        binding
        for binding in app.app.key_bindings.bindings
        if [str(key) for key in binding.keys] == ["y"]
    ]
    assert bound, "y is bound for approvals"
    assert not bound[0].filter(), "but not while the box is just a box"

    app.open_approval(_Record())
    assert bound[0].filter(), "and it is while a question is open"
