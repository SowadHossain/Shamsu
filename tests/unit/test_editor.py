"""Line editing and history.

`Buffer` is a pure state machine, so every rule here is asserted by pressing
keys at a value -- no terminal, no `msvcrt`, no pipe. That is the point of the
split: the editing rules are the part worth testing, and they are the part that
would otherwise need a TTY to exercise.
"""

from __future__ import annotations

from shamsu.ui.editor import CANCELLED, EOF, Buffer, History, render_line
from shamsu.ui.terminal import Key


def _type(text: str, buffer: Buffer | None = None) -> Buffer:
    current = buffer or Buffer.new()
    for char in text:
        current, _ = current.press(char)
    return current


def _press(buffer: Buffer, *keys: str) -> Buffer:
    for key in keys:
        buffer, _ = buffer.press(key)
    return buffer


class TestTyping:
    def test_characters_accumulate(self) -> None:
        assert _type("fix add").text == "fix add"

    def test_the_cursor_follows_the_text(self) -> None:
        assert _type("abc").cursor == 3

    def test_typing_inserts_at_the_cursor(self) -> None:
        buffer = _press(_type("ac"), Key.LEFT)
        assert _type("b", buffer).text == "abc"

    def test_control_keys_are_not_inserted_as_text(self) -> None:
        """A key name must never end up in the buffer as literal characters."""
        assert _press(_type("ab"), Key.CTRL_L, "unbound-key").text == "ab"


class TestMovingAndDeleting:
    def test_backspace_removes_before_the_cursor(self) -> None:
        assert _press(_type("abc"), Key.BACKSPACE).text == "ab"

    def test_backspace_at_the_start_does_nothing(self) -> None:
        assert _press(_type("abc"), Key.HOME, Key.BACKSPACE).text == "abc"

    def test_delete_removes_after_the_cursor(self) -> None:
        assert _press(_type("abc"), Key.HOME, Key.DELETE).text == "bc"

    def test_home_and_end_move_to_the_edges(self) -> None:
        buffer = _press(_type("abc"), Key.HOME)
        assert buffer.cursor == 0
        assert _press(buffer, Key.END).cursor == 3

    def test_the_cursor_cannot_leave_the_text(self) -> None:
        assert _press(Buffer.new(), Key.LEFT).cursor == 0
        assert _press(_type("ab"), Key.RIGHT, Key.RIGHT).cursor == 2

    def test_ctrl_u_kills_to_the_start(self) -> None:
        assert _press(_type("abc def"), Key.CTRL_U).text == ""

    def test_ctrl_k_kills_to_the_end(self) -> None:
        assert _press(_type("abc def"), Key.HOME, Key.CTRL_K).text == ""

    def test_ctrl_w_deletes_the_previous_word(self) -> None:
        assert _press(_type("fix the adder"), Key.CTRL_W).text == "fix the "

    def test_ctrl_w_skips_trailing_separators_first(self) -> None:
        """Otherwise deleting after a space removes only the space."""
        assert _press(_type("fix the adder  "), Key.CTRL_W).text == "fix the "

    def test_ctrl_w_treats_a_path_separator_as_a_boundary(self) -> None:
        assert _press(_type("src/shamsu/ui"), Key.CTRL_W).text == "src/shamsu/"


class TestFinishing:
    def test_enter_yields_the_line(self) -> None:
        _, finished = _type("fix add").press(Key.ENTER)
        assert finished == "fix add"

    def test_ctrl_c_cancels_and_clears(self) -> None:
        buffer, finished = _type("half a request").press(Key.CTRL_C)
        assert finished == CANCELLED
        assert buffer.text == ""

    def test_ctrl_d_on_an_empty_line_is_end_of_input(self) -> None:
        _, finished = Buffer.new().press(Key.CTRL_D)
        assert finished == EOF

    def test_ctrl_d_with_text_deletes_forward_instead(self) -> None:
        """Every shell behaves this way; closing the session mid-sentence would not."""
        buffer, finished = _press(_type("abc"), Key.HOME).press(Key.CTRL_D)
        assert finished is None
        assert buffer.text == "bc"


class TestHistory:
    def test_up_recalls_the_previous_line(self) -> None:
        buffer = Buffer.new(("first", "second"))
        assert _press(buffer, Key.UP).text == "second"

    def test_up_twice_goes_further_back(self) -> None:
        buffer = Buffer.new(("first", "second"))
        assert _press(buffer, Key.UP, Key.UP).text == "first"

    def test_it_stops_at_the_oldest_entry(self) -> None:
        buffer = Buffer.new(("only",))
        assert _press(buffer, Key.UP, Key.UP, Key.UP).text == "only"

    def test_down_returns_to_what_was_being_typed(self) -> None:
        """Scrolling up and back must not leave a history entry in the line."""
        buffer = _type("half typed", Buffer.new(("earlier",)))
        assert _press(buffer, Key.UP, Key.DOWN).text == "half typed"

    def test_down_on_a_fresh_line_does_nothing(self) -> None:
        assert _press(Buffer.new(("a",)), Key.DOWN).text == ""

    def test_a_recalled_line_is_editable(self) -> None:
        buffer = _press(Buffer.new(("fix add",)), Key.UP)
        assert _press(buffer, Key.BACKSPACE).text == "fix ad"

    def test_the_cursor_lands_at_the_end_of_a_recalled_line(self) -> None:
        assert _press(Buffer.new(("fix add",)), Key.UP).cursor == len("fix add")


class TestHistoryStore:
    def test_lines_are_kept_in_order(self) -> None:
        store = History()
        store.add("first")
        store.add("second")
        assert store.entries() == ("first", "second")

    def test_blank_lines_are_not_kept(self) -> None:
        store = History()
        store.add("   ")
        assert store.entries() == ()

    def test_an_immediate_repeat_is_not_kept_twice(self) -> None:
        store = History()
        store.add("same")
        store.add("same")
        assert store.entries() == ("same",)

    def test_it_is_bounded(self) -> None:
        store = History(limit=3)
        for index in range(10):
            store.add(f"line {index}")
        assert store.entries() == ("line 7", "line 8", "line 9")


class TestRendering:
    def test_the_line_is_rewritten_in_place(self) -> None:
        """A carriage return and erase, not a clear -- clearing flickers."""
        drawn = render_line("> ", _type("abc"))
        assert drawn.startswith("\r\x1b[K")
        assert "abc" in drawn

    def test_the_cursor_is_placed_after_the_prompt(self) -> None:
        drawn = render_line("> ", _type("abc"))
        assert drawn.endswith("\r\x1b[5C")  # 2 prompt columns + 3 typed

    def test_colour_in_the_prompt_costs_no_columns(self) -> None:
        """An escape occupies no width; counting it would misplace the cursor."""
        plain = render_line("> ", _type("abc"))
        painted = render_line("\x1b[38;5;179m> \x1b[0m", _type("abc"))
        assert plain[plain.rindex("\r") :] == painted[painted.rindex("\r") :]
