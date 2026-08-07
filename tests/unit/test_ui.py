"""The terminal interface, tested without a terminal.

That is the whole point of the split. `RunView` folds events into state and
`render` turns state into lines; both are pure, so every layout question — does
the footer fit at 40 columns, does the latest line stay visible, does a stale
frame keep its box rectangular — is answerable in a unit test.

v1's CLI was 18,729 lines and effectively untested, because display, input, and
agent control lived in one object that needed a TTY to instantiate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shamsu.interfaces.enums import AgentState, EvidenceKind, Phase, RunStatus
from shamsu.interfaces.ids import RunId
from shamsu.runtime.events import EventKind, RunEvent
from shamsu.ui.render import MIN_HEIGHT, MIN_WIDTH, render
from shamsu.ui.view import MAX_ACTIVITY, Level, RunView

START = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)
RUN = RunId("run-1")


def _event(kind: EventKind, **kwargs: object) -> RunEvent:
    return RunEvent(run_id=RUN, kind=kind, at=START, **kwargs)  # type: ignore[arg-type]


def _view(**kwargs: object) -> RunView:
    view = RunView(request="fix add() so it sums", workspace="/home/u/proj", **kwargs)  # type: ignore[arg-type]
    view.started_at = START
    return view


# ---------------------------------------------------------------------------
# The view model
# ---------------------------------------------------------------------------


class TestView:
    def test_events_become_activity(self) -> None:
        view = _view()
        view.apply(_event(EventKind.STARTED, status=RunStatus.RUNNING))
        view.apply(_event(EventKind.TOOL_INVOKED, detail="file.read calc.py"))

        assert [item.label for item in view.activity] == ["started", "tool"]
        assert view.activity[-1].detail == "file.read calc.py"

    def test_a_failed_tool_call_is_marked(self) -> None:
        """The '!' prefix is the runtime's way of saying the call did not work."""
        view = _view()
        view.apply(_event(EventKind.TOOL_INVOKED, detail="!file.patch refused"))

        assert view.activity[-1].level == Level.FAIL
        assert view.activity[-1].detail == "file.patch refused"

    def test_a_phase_change_is_noted_once_not_every_state(self) -> None:
        """Nineteen states, roughly five things a watcher cares about."""
        view = _view()
        for state in (
            AgentState.CREATE_PLAN,
            AgentState.VALIDATE_PLAN,
            AgentState.APPROVAL_CHECK,
        ):
            view.apply(_event(EventKind.STATE_CHANGED, state=state))

        assert [item.label for item in view.activity] == ["plan"]
        assert view.phase is Phase.PLAN

    def test_an_unknown_event_kind_is_ignored_not_printed(self) -> None:
        """A new EventKind must not start showing raw enum names to a user."""
        view = _view()
        view.apply(_event(EventKind.MODEL_CALLED, detail="whatever"))
        assert view.activity == []

    def test_status_follows_the_events(self) -> None:
        view = _view()
        view.apply(_event(EventKind.STARTED, status=RunStatus.RUNNING))
        assert view.running is True

        view.apply(_event(EventKind.COMPLETED, status=RunStatus.COMPLETED, detail="done"))
        assert view.running is False
        assert view.outcome == "done"

    def test_cancelling_is_distinct_from_cancelled(self) -> None:
        """One means the request was delivered; the other means it stopped."""
        view = _view()
        view.apply(_event(EventKind.CANCEL_REQUESTED, detail="user"))
        view.status = RunStatus.CANCELLING
        assert view.cancelling is True and view.running is True

        view.apply(_event(EventKind.CANCELLED, status=RunStatus.CANCELLED))
        assert view.running is False

    def test_activity_is_capped(self) -> None:
        """An unbounded list on a long run is a memory leak with a nice UI."""
        view = _view()
        for index in range(MAX_ACTIVITY + 50):
            view.note(Level.NOTE, "tool", str(index))

        assert len(view.activity) == MAX_ACTIVITY
        assert view.activity[-1].detail == str(MAX_ACTIVITY + 49)

    def test_elapsed_stops_at_the_finish(self) -> None:
        view = _view()
        view.apply(_event(EventKind.COMPLETED, status=RunStatus.COMPLETED))
        assert view.elapsed(START + timedelta(minutes=5)) == 0.0


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRenderGeometry:
    @pytest.mark.parametrize(("width", "height"), [(80, 24), (40, 10), (120, 40), (34, 6)])
    def test_a_frame_is_exactly_the_window_it_was_given(self, width: int, height: int) -> None:
        """One line too many and the display scrolls itself off the screen."""
        lines = render(_view(), width, height, now=START, colour=False)

        assert len(lines) == height
        assert all(len(line) <= width for line in lines), [
            line for line in lines if len(line) > width
        ]

    def test_the_box_stays_rectangular(self) -> None:
        view = _view()
        view.note(Level.OK, "tool", "x" * 300)
        lines = render(view, 60, 12, now=START, colour=False)

        body = lines[1:-1]
        assert all(line.startswith("│") and line.endswith("│") for line in body[:-2]), body

    def test_colour_adds_no_visible_width(self) -> None:
        """Padding must count visible columns, not bytes."""
        view = _view()
        view.note(Level.OK, "tool", "file.read calc.py")

        plain = render(view, 60, 12, now=START, colour=False)
        painted = render(view, 60, 12, now=START, colour=True)

        assert len(plain) == len(painted)
        assert all("\x1b" not in line for line in plain)
        assert any("\x1b" in line for line in painted)

    def test_a_tiny_window_degrades_instead_of_breaking(self) -> None:
        lines = render(_view(), MIN_WIDTH - 1, MIN_HEIGHT - 1, now=START, colour=False)
        assert len(lines) == MIN_HEIGHT - 1
        assert "shamsu" in lines[0]

    def test_a_one_line_window_still_says_something_true(self) -> None:
        lines = render(_view(), 20, 1, now=START, colour=False)
        assert len(lines) == 1


class TestRenderContent:
    def test_the_request_is_shown(self) -> None:
        rendered = "\n".join(render(_view(), 70, 14, now=START, colour=False))
        assert "fix add() so it sums" in rendered

    def test_the_latest_activity_is_always_visible(self) -> None:
        """A run whose newest line scrolled off screen is unusable."""
        view = _view()
        for index in range(60):
            view.note(Level.OK, "tool", f"call-{index}")

        rendered = "\n".join(render(view, 60, 14, now=START, colour=False))
        assert "call-59" in rendered
        assert "call-0 " not in rendered

    def test_the_workspace_is_truncated_from_the_left(self) -> None:
        """The informative end of a path is the far end."""
        view = RunView(workspace="/very/long/path/to/the/actual/project")
        view.started_at = START
        header = render(view, 44, 10, now=START, colour=False)[0]

        assert "project" in header
        assert "…" in header

    def test_the_clock_advances(self) -> None:
        view = _view()
        early = "\n".join(render(view, 70, 14, now=START + timedelta(seconds=5), colour=False))
        later = "\n".join(render(view, 70, 14, now=START + timedelta(seconds=75), colour=False))

        assert "00:05" in early
        assert "01:15" in later

    def test_the_cancel_hint_survives_a_narrow_window(self) -> None:
        """The one thing a user may urgently need must not be the bit truncated."""
        view = _view()
        view.observe_step(3, 7, "something")
        view.observe_evidence([EvidenceKind.FILE_CHANGED, EvidenceKind.TESTS_PASSED])
        view.waiting_on = "model"

        for width in (36, 44, 60, 100):
            footer = render(view, width, 12, now=START, colour=False)[-2]
            assert "^C cancel" in footer, (width, footer)

    def test_a_finished_run_offers_a_way_out(self) -> None:
        view = _view()
        view.apply(_event(EventKind.COMPLETED, status=RunStatus.COMPLETED))
        footer = render(view, 70, 12, now=START, colour=False)[-2]

        assert "done" in footer
        assert "q quit" in footer
        assert "^C cancel" not in footer

    def test_stopping_is_shown_while_cancelling(self) -> None:
        view = _view()
        view.status = RunStatus.CANCELLING
        footer = render(view, 70, 12, now=START, colour=False)[-2]
        assert "stopping" in footer

    def test_the_spinner_turns(self) -> None:
        """A static interface during a thirty-second model call reads as a hang."""
        view = _view()
        view.waiting_on = "model"

        frames = {
            render(view, 100, 12, now=START, colour=False, tick=tick)[-2] for tick in range(4)
        }
        assert len(frames) == 4

    def test_the_phase_is_shown(self) -> None:
        view = _view()
        view.apply(_event(EventKind.STATE_CHANGED, state=AgentState.EXECUTE_CURRENT_STEP))
        assert "author" in render(view, 70, 12, now=START, colour=False)[-2]
