"""The full-screen session frame.

`render_session` is a pure function of its state, so the layout is asserted by
comparing strings — no terminal, no alternate screen, no keystrokes. That is
the property `ui/__init__.py` says v1's interface never had.
"""

from __future__ import annotations

import pytest

from shamsu.ui.commands import complete
from shamsu.ui.session_frame import (
    MAX_SUGGESTIONS,
    SessionState,
    render_session,
)
from shamsu.ui.view import Level


def _state(**kwargs: object) -> SessionState:
    base: dict[str, object] = {
        "workspace": "F:/Work/demo-1/New folder",
        "model": "qwen2.5-coder:14b",
    }
    return SessionState(**{**base, **kwargs})  # type: ignore[arg-type]


def _visible(text: str) -> int:
    total, index = 0, 0
    while index < len(text):
        if text[index] == "\x1b":
            end = text.find("m", index)
            if end == -1:
                break
            index = end + 1
            continue
        total += 1
        index += 1
    return total


class TestGeometry:
    @pytest.mark.parametrize(("width", "height"), [(40, 10), (80, 24), (120, 40), (66, 16)])
    def test_a_frame_is_exactly_the_window_it_was_given(self, width: int, height: int) -> None:
        state = _state()
        for index in range(30):
            state.note(Level.OK, "author", f"file.read  file{index}.py")

        lines = render_session(state, width, height, colour=False)
        assert len(lines) == height
        assert all(len(line) <= width for line in lines)

    def test_the_box_stays_rectangular(self) -> None:
        lines = render_session(_state(), 60, 14, colour=False)
        box = [line for line in lines if line.startswith(("┌", "│", "├", "└"))]
        assert len({_visible(line) for line in box}) == 1

    def test_a_tiny_window_degrades_instead_of_drawing_a_broken_box(self) -> None:
        lines = render_session(_state(text="hi"), 20, 4, colour=False)
        assert len(lines) <= 4
        assert not any(line.startswith("┌") for line in lines)

    def test_colour_adds_no_visible_width(self) -> None:
        plain = render_session(_state(), 60, 14, colour=False)
        painted = render_session(_state(), 60, 14, colour=True)
        assert [_visible(line) for line in plain] == [_visible(line) for line in painted]


class TestTranscript:
    def test_the_newest_activity_is_always_visible(self) -> None:
        """A pane that hides the present is worse than one that scrolls."""
        state = _state()
        for index in range(50):
            state.note(Level.OK, "author", f"file.read  file{index}.py")

        rendered = "\n".join(render_session(state, 70, 16, colour=False))
        assert "file49.py" in rendered
        assert "file0.py" not in rendered

    def test_an_empty_session_still_fills_the_box(self) -> None:
        lines = render_session(_state(), 60, 14, colour=False)
        assert len(lines) == 14


class TestStatusBar:
    def test_it_names_the_mode_and_the_model(self) -> None:
        rendered = "\n".join(render_session(_state(), 80, 14, colour=False))
        assert "build" in rendered
        assert "qwen2.5-coder:14b" in rendered

    def test_a_running_turn_offers_a_way_to_stop_it(self) -> None:
        state = _state(busy=True, spinner="⠹", status="verify")
        assert "^C stop" in "\n".join(render_session(state, 80, 14, colour=False))

    def test_evidence_is_shown_once_there_is_any(self) -> None:
        state = _state(evidence=2)
        assert "✓2 evidence" in "\n".join(render_session(state, 80, 14, colour=False))

    def test_a_narrow_window_sheds_detail_rather_than_overflowing(self) -> None:
        state = _state(busy=True, spinner="⠹", status="verify", evidence=9)
        lines = render_session(state, 40, 14, colour=False)
        assert all(len(line) <= 40 for line in lines)


class TestSuggestions:
    def test_typing_a_command_opens_the_dropdown(self) -> None:
        state = _state(text="/mo", suggestions=complete("/mo"))
        rendered = "\n".join(render_session(state, 70, 16, colour=False))
        assert "/mode" in rendered
        assert "/model" in rendered

    def test_the_selected_entry_is_marked(self) -> None:
        state = _state(text="/s", suggestions=complete("/s"), selected=1)
        lines = [line for line in render_session(state, 70, 16, colour=False) if "/status" in line]
        assert lines and "›" in lines[0]

    def test_a_long_list_is_capped_and_says_so(self) -> None:
        state = _state(text="/", suggestions=complete("/"))
        rendered = render_session(state, 70, 20, colour=False)
        assert any("more" in line for line in rendered)

    def test_the_dropdown_does_not_push_the_frame_over_its_height(self) -> None:
        state = _state(text="/", suggestions=complete("/"))
        assert len(render_session(state, 70, 20, colour=False)) == 20

    def test_no_dropdown_when_typing_prose(self) -> None:
        state = _state(text="fix the add function")
        rendered = "\n".join(render_session(state, 70, 16, colour=False))
        assert "/model" not in rendered


class TestInputLine:
    def test_what_is_typed_appears_below_the_box(self) -> None:
        state = _state(text="fix the add function")
        lines = render_session(state, 70, 16, colour=False)
        assert "fix the add function" in lines[-1]

    def test_the_input_sits_above_the_dropdown(self) -> None:
        state = _state(text="/mo", suggestions=complete("/mo"))
        lines = render_session(state, 70, 16, colour=False)
        typed = next(i for i, line in enumerate(lines) if "/mo" in line and "›" in line)
        listed = next(i for i, line in enumerate(lines) if "show the model" in line)
        assert typed < listed

    def test_the_cap_matches_what_the_driver_counts(self) -> None:
        """The cursor row is computed from MAX_SUGGESTIONS; drift misplaces it."""
        state = _state(text="/", suggestions=complete("/"))
        shown = [line for line in render_session(state, 70, 24, colour=False) if "  /" in line]
        assert len(shown) <= MAX_SUGGESTIONS + 1
