"""The framed TUI: scrollback, sidebar, and the frame itself.

Most of what matters here is `LogPane`, because the pane IS the scrollback -
the objection to a full-screen frame was that it costs the terminal's own, and
the answer is that the application keeps its own the way Neovim does. A
scrollback that jumps to the bottom while you are reading it is not one.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

import pytest

from shamsu.cli.live_console import TurnTelemetry
from shamsu.cli.tui import (
    MIN_WIDTH_FOR_SIDEBAR,
    LogPane,
    PaneWriter,
    TuiApp,
    coalesce,
    parse_ansi,
    render_sidebar,
    split_lines,
    tui_enabled,
    wrap_line,
)


class Event:
    def __init__(self, kind: str, text: str = "", **data) -> None:
        self.kind = kind
        self.text = text
        self.data = data


def _pane(width: int = 40, max_lines: int = 500) -> LogPane:
    pane = LogPane(max_lines=max_lines)
    pane.set_width(width)
    return pane


# -- follow-tail, which decides whether the scrollback is usable at all -----


def test_new_output_follows_the_tail_by_default():
    pane = _pane()
    for n in range(50):
        pane.write(f"line {n}\n")
    assert pane.follow
    assert "line 49" in pane.plain(5)


def test_scrolling_up_stops_new_output_yanking_you_back():
    """The single detail that decides whether you can read the log during a
    live turn. Without it, every new line throws away where you were."""
    pane = _pane()
    for n in range(50):
        pane.write(f"line {n}\n")
    pane.scroll(-20, 5)
    assert not pane.follow
    here = pane.plain(5)

    for n in range(50, 60):
        pane.write(f"line {n}\n")

    assert pane.plain(5) == here, "new output moved the view while it was scrolled up"
    assert "line 59" not in pane.plain(5)


def test_scrolling_back_to_the_bottom_re_arms_following():
    pane = _pane()
    for n in range(50):
        pane.write(f"line {n}\n")
    pane.scroll(-20, 5)
    assert not pane.follow

    pane.scroll(999, 5)
    assert pane.follow
    pane.write("line 50\n")
    assert "line 50" in pane.plain(5)


def test_paging_moves_about_a_screen():
    pane = _pane()
    for n in range(100):
        pane.write(f"line {n}\n")
    pane.page(-1, 10)
    assert not pane.follow
    assert pane.offset == 100 - 10 - 9


def test_home_and_end_reach_both_ends():
    pane = _pane()
    for n in range(100):
        pane.write(f"line {n}\n")
    pane.visible(10)  # teaches the pane how tall it is

    pane.to_start()
    assert pane.offset == 0
    assert "line 0" in pane.plain(10)
    assert not pane.follow

    pane.to_end()
    assert pane.follow
    assert "line 99" in pane.plain(10)


def test_scrolling_cannot_run_off_either_end():
    pane = _pane()
    for n in range(20):
        pane.write(f"line {n}\n")
    pane.scroll(-10_000, 5)
    assert pane.offset == 0
    pane.scroll(10_000, 5)
    assert pane.offset == pane.max_offset(5)


def test_the_ruler_says_where_you_are():
    pane = _pane()
    for n in range(100):
        pane.write(f"line {n}\n")
    pane.visible(10)
    assert pane.scroll_position() == "bot"
    pane.to_start()
    assert pane.scroll_position() == "top"
    pane.scroll(45, 10)
    assert pane.scroll_position().endswith("%")


# -- what goes in ----------------------------------------------------------


def test_a_chunk_that_does_not_end_in_a_newline_is_held():
    """Rich writes in pieces that do not respect line boundaries; half a line
    must not render as a whole one."""
    pane = _pane()
    pane.write("half a ")
    assert pane.plain(5) == "half a "
    pane.write("line\nand another\n")
    assert pane.plain(5) == "half a line\nand another"


# -- control characters must never reach the terminal ----------------------
#
# Live 2026-08-23, from a screenshot: the pane was thousands of rows of
# `Working...^M`, the sidebar labels were prefixed with stray `M`s, and the
# whole frame was scrambled. `ANSI()` consumes the SGR codes it understands and
# passes the rest through AS TEXT - and prompt_toolkit writes fragment text
# verbatim, so a `\x1b[1A` sitting in the pane physically moves the real
# cursor. Nothing that steers a cursor may be stored.


RICH_STATUS_FRAME = "\x1b[?25l\x1b[32m⠋\x1b[0m Working...\r\x1b[2K"


def test_a_spinner_redraw_replaces_its_line_instead_of_adding_one():
    """Everything that draws a spinner or a progress bar - rich's
    `console.status`, npm, pip, pytest - redraws by emitting `\\r` and the line
    again. Treated as ordinary text that is one row PER FRAME."""
    pane = _pane()
    for _ in range(200):
        pane.write(RICH_STATUS_FRAME)
    pane.write("\x1b[32m⠋\x1b[0m Working...\ndone\n")

    assert pane.total_rows == 2, "each spinner frame became its own row"
    assert pane.plain(10) == "⠋ Working...\ndone"


def test_no_control_character_is_ever_stored():
    pane = _pane()
    pane.write("a\x1b[1Ab\x1b[2Kc\x07d\x00e\n")
    text = pane.plain(5)
    for forbidden in ("\x1b", "\r", "\x07", "\x00"):
        assert forbidden not in text, repr(forbidden)


def test_a_private_mode_escape_does_not_become_literal_text():
    """`ANSI()` does not recognise the `ESC [ ? 25 l` form and renders it as
    the text `25l`, which is what put garbage in the pane."""
    pane = _pane()
    pane.write("\x1b[?25lhello\x1b[?25h\n")
    assert pane.plain(5) == "hello"


def test_carriage_returns_inside_one_write_all_collapse():
    pane = _pane()
    pane.write("first\rsecond\rthird\n")
    assert pane.plain(5) == "third"


def test_a_carriage_return_does_not_eat_the_line_before_it():
    pane = _pane()
    pane.write("kept\n")
    pane.write("scratch\rfinal\n")
    assert pane.plain(5) == "kept\nfinal"


def test_colour_survives_a_carriage_return_redraw():
    pane = _pane()
    pane.write("\x1b[31mold\x1b[0m\r\x1b[32mnew\x1b[0m\n")
    fragments = pane.visible(5)
    assert "".join(f[1] for f in fragments) == "new"
    assert any("green" in str(style) for style, *_rest in fragments)


def test_colour_survives_the_round_trip():
    pane = _pane()
    pane.write("plain \x1b[31mred\x1b[0m\n")
    fragments = pane.visible(5)
    styles = {style for style, *_rest in fragments}
    assert "ansired" in styles
    assert "".join(f[1] for f in fragments) == "plain red"


def test_fragments_are_coalesced():
    """`ANSI()` emits one fragment per CHARACTER - a full scrollback of those
    is millions of tuples."""
    assert coalesce(parse_ansi("hello")) == [("", "hello")]
    assert len(parse_ansi("hello \x1b[31mworld\x1b[0m")) == 2


def test_lines_split_without_losing_style():
    lines = split_lines([("red", "one\ntwo")])
    assert lines == [[("red", "one")], [("red", "two")]]


def test_a_long_line_wraps_to_the_pane_width():
    """Wrapping happens here rather than in the terminal, because the pane
    scrolls by counting rows - a line the terminal wrapped behind our back
    would put every offset below it out by one."""
    rows = wrap_line([("", "x" * 25)], 10)
    assert [sum(len(t) for _s, t in row) for row in rows] == [10, 10, 5]


def test_wrapping_splits_a_fragment_without_dropping_style():
    rows = wrap_line([("red", "abcdef")], 4)
    assert rows == [[("red", "abcd")], [("red", "ef")]]


def test_a_wrapped_line_counts_as_several_rows():
    pane = _pane(width=10)
    pane.write("x" * 25 + "\n")
    assert pane.total_rows == 3


def test_a_resize_rewraps_everything():
    pane = _pane(width=10)
    pane.write("x" * 24 + "\n")
    assert pane.total_rows == 3
    pane.set_width(30)
    assert pane.total_rows == 1


def test_the_scrollback_is_bounded():
    """A 24-round turn over big diffs will produce tens of thousands of lines;
    the cap is what stops a long session becoming a memory leak."""
    pane = _pane(max_lines=10)
    for n in range(100):
        pane.write(f"line {n}\n")
    assert pane.total_rows == 10
    assert "line 99" in pane.plain(10)
    assert "line 50" not in pane.plain(10)


def test_eviction_does_not_shift_what_you_are_reading_off_by_one():
    pane = _pane(max_lines=20)
    for n in range(20):
        pane.write(f"line {n}\n")
    pane.scroll(-10, 5)
    before = pane.plain(5)
    pane.write("new\n")
    # One line fell off the top, so the same content is now one row higher -
    # the view must move with it rather than drift.
    assert pane.plain(5) == before


def test_clearing_empties_it():
    pane = _pane()
    pane.write("something\n")
    pane.clear()
    assert pane.total_rows == 0
    assert pane.follow


# -- telling a prompt, a log and an answer apart ---------------------------
#
# Three different kinds of thing that all arrived as undifferentiated text, so
# a long session's scrollback read as one wall. A prompt is coloured by WHERE
# IT CAME FROM, because the same session takes work from this terminal, the web
# portal and Telegram, and "who asked for this?" was otherwise unanswerable.


def _styles_for(pane: LogPane, needle: str) -> set[str]:
    return {
        str(style)
        for style, *rest in pane.visible(40)
        if needle in (rest[0] if rest else "")
    }


def test_each_surface_gets_its_own_colour_and_its_own_mark():
    from shamsu.cli.tui import PROMPT_SURFACES

    pane = _pane(width=60)
    pane.write_prompt("from the terminal", "cli")
    pane.write_prompt("from the phone", "telegram")
    pane.write_prompt("from the browser", "web")

    assert _styles_for(pane, "from the terminal") == {"class:kind.cli"}
    assert _styles_for(pane, "from the phone") == {"class:kind.telegram"}
    assert _styles_for(pane, "from the browser") == {"class:kind.web"}

    # Every surface must be distinguishable from every other, by BOTH channels.
    marks = {mark for mark, _ms, _s in PROMPT_SURFACES.values()}
    colours = {style for _m, _ms, style in PROMPT_SURFACES.values()}
    assert len(marks) == len(PROMPT_SURFACES)
    assert len(colours) == len(PROMPT_SURFACES)


def test_a_prompt_is_marked_as_well_as_coloured():
    """Colour alone is not a distinction: palettes vary, some are unreadable,
    and some people cannot tell the green from the amber."""
    pane = _pane(width=60)
    pane.write_prompt("do the thing", "cli")
    assert pane.plain(5).startswith("› ")


def test_an_unknown_surface_is_still_visibly_a_prompt():
    pane = _pane(width=60)
    pane.write_prompt("from somewhere new", "carrier-pigeon")
    assert _styles_for(pane, "from somewhere new") == {"class:kind.other"}
    assert pane.plain(5).startswith("? ")


def test_the_answer_is_distinct_from_the_log_above_it():
    """The answer is the thing you scroll back to find, and it was
    indistinguishable from the forty action rows above it."""
    from shamsu.cli.tui import KIND_ANSWER

    pane = _pane(width=60)
    pane.write("  Reading config.py\n")
    pane.write_as("Fixed - it falls back to localhost now.", KIND_ANSWER)

    assert _styles_for(pane, "Reading config.py") == {""}
    assert _styles_for(pane, "Fixed -") == {"class:kind.answer"}
    assert "◆ " in pane.plain(10)


def test_ordinary_log_output_keeps_the_colour_rich_gave_it():
    """Logs are the neutral background the other two stand out against, which
    only works if nothing repaints them."""
    pane = _pane(width=60)
    pane.write("\x1b[31mFAILED\x1b[0m to patch\n")
    assert "ansired" in _styles_for(pane, "FAILED")


def test_a_decoration_does_not_leak_past_its_block():
    from shamsu.cli.tui import KIND_ANSWER, LINE_KINDS

    pane = _pane(width=60)
    with pane.decorate_as(*LINE_KINDS[KIND_ANSWER]):
        pane.write("the answer\n")
    pane.write("an ordinary log line\n")

    assert _styles_for(pane, "an ordinary log line") == {""}
    assert "◆ an ordinary" not in pane.plain(10)


def test_a_half_written_line_is_closed_before_the_kind_changes():
    """Rich writes in pieces; a chunk still open when the block opens would
    inherit a mark that belongs to the next thing."""
    from shamsu.cli.tui import KIND_ANSWER, LINE_KINDS

    pane = _pane(width=60)
    pane.write("half a log line")
    with pane.decorate_as(*LINE_KINDS[KIND_ANSWER]):
        pane.write("the answer\n")

    text = pane.plain(10)
    assert "half a log line" in text
    assert "◆ half a log line" not in text


def test_a_multi_line_answer_is_marked_on_its_first_row():
    from shamsu.cli.tui import KIND_ANSWER

    pane = _pane(width=60)
    pane.write_as("first line\nsecond line", KIND_ANSWER)
    assert _styles_for(pane, "second line") == {"class:kind.answer"}


@pytest.mark.asyncio
async def test_a_turn_started_elsewhere_shows_whose_it_was():
    """A turn begun in the web portal is still this session's work, and the
    terminal is where someone is watching it."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    class Remote:
        kind = "turn.start"
        text = "deploy the thing"
        source = "telegram"
        data: ClassVar[dict] = {}

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=_telemetry(), on_submit=lambda _t: None)
        app.absorb_for_display(Remote())

    assert "✈ deploy the thing" in app.pane.plain(10)


@pytest.mark.asyncio
async def test_a_telegram_turn_started_elsewhere_streams_live_rows():
    """Telegram turns are watched from the desktop too, so their live rows
    should not wait for the final answer before appearing in the frame."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from shamsu.runtime.turn_stream import TurnEvent

    common = {"source": "telegram", "session_id": "sess-tg", "workspace": "w"}
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=TurnTelemetry(unicode_ui=True), on_submit=lambda _t: None)
        app.absorb_for_display(TurnEvent(seq=1, kind="turn.start", text="fix login", **common))
        app.absorb_for_display(
            TurnEvent(
                seq=2,
                kind="status",
                text="reading auth.py",
                data={"round": 2, "max_rounds": 10},
                **common,
            )
        )
        app.absorb_for_display(
            TurnEvent(seq=3, kind="activity", text="model responded in 4s", **common)
        )
        app.absorb_for_display(
            TurnEvent(seq=4, kind="tool.call", text="read_file auth.py", **common)
        )

    text = app.pane.plain(20)
    assert "fix login" in text
    assert "model responded in 4s" in text
    assert "read_file auth.py" in text
    assert app.telemetry.round == 2
    assert app.telemetry.max_rounds == 10


@pytest.mark.asyncio
async def test_a_turn_the_terminal_never_started_still_shows_up():
    """The web portal runs in this same process and builds its own
    `TurnStream`, which the CLI had no way to know existed - so a prompt sent
    from the browser ran to completion with the terminal showing nothing. The
    surfaces were not out of sync; they were not connected."""
    import tempfile

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from shamsu.runtime.turn_stream import TurnEvent, TurnStream

    workspace = Path(tempfile.mkdtemp())
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=_telemetry(), on_submit=lambda _t: None)
        detach = TurnStream.add_observer(app.absorb_for_display)

        stream = TurnStream(workspace, "sess-web")
        common = {
            "session_id": "sess-web",
            "workspace": str(workspace),
            "source": "web",
        }
        stream.publish(TurnEvent(seq=1, kind="turn.start", text="write the plan", **common))
        stream.publish(TurnEvent(seq=2, kind="assistant", text="Wrote PLAN.md.", **common))
        stream.publish(
            TurnEvent(
                seq=3, kind="turn.end", text="failed", data={"status": "failed"}, **common
            )
        )

        text = app.pane.plain(20)
        assert "◈ write the plan" in text, "the prompt never reached the terminal"
        assert "◆ Wrote PLAN.md." in text, "the answer never reached the terminal"
        assert "web turn failed" in text, "the failure was silent"

        detach()
        stream.publish(TurnEvent(seq=4, kind="turn.start", text="after detach", **common))
        assert "after detach" not in app.pane.plain(20)


@pytest.mark.asyncio
async def test_a_foreign_turns_tool_rows_do_not_flood_this_terminal():
    """Forty action lines from another surface interleaved with this one's
    would make both unreadable, and the browser is already showing them."""
    import tempfile

    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from shamsu.runtime.turn_stream import TurnEvent, TurnStream

    workspace = Path(tempfile.mkdtemp())
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=_telemetry(), on_submit=lambda _t: None)
        detach = TurnStream.add_observer(app.absorb_for_display)
        stream = TurnStream(workspace, "sess-web")
        common = {"session_id": "sess-web", "workspace": str(workspace), "source": "web"}
        for seq in range(40):
            stream.publish(
                TurnEvent(seq=seq, kind="tool.call", text="Reading a.py", **common)
            )
            stream.publish(
                TurnEvent(seq=seq, kind="status", text="thinking...", **common)
            )
        detach()

    assert "Reading a.py" not in app.pane.plain(60)
    assert "thinking..." not in app.pane.plain(60)


def test_an_observer_that_raises_never_fails_the_turn():
    import tempfile

    from shamsu.runtime.turn_stream import TurnEvent, TurnStream

    workspace = Path(tempfile.mkdtemp())
    detach = TurnStream.add_observer(
        lambda _event: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    try:
        stream = TurnStream(workspace, "sess")
        stream.publish(TurnEvent(seq=1, kind="turn.start", text="go"))
    finally:
        detach()


@pytest.mark.asyncio
async def test_this_terminals_own_prompt_is_not_echoed_twice():
    """`FrameHost.submit` already echoed it at the moment it was typed."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    class Local:
        kind = "turn.start"
        text = "fix the tests"
        source = "cli"
        data: ClassVar[dict] = {}

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=_telemetry(), on_submit=lambda _t: None)
        app.absorb_for_display(Local())

    assert "fix the tests" not in app.pane.plain(10)


# -- the bridge rich writes through ----------------------------------------


def test_a_real_rich_console_lands_in_the_pane_with_colour():
    from rich.console import Console

    pane = _pane(width=60)
    painted: list[int] = []
    console = Console(force_terminal=True, color_system="truecolor", width=60)
    console.file = PaneWriter(pane, lambda: painted.append(1))

    console.print("[red]failed[/red] to patch config.py")

    assert "failed to patch config.py" in pane.plain(5)
    assert any(style for style, *_rest in pane.visible(5))
    assert painted, "the frame was never told to repaint"


def test_a_rich_panel_lands_whole():
    """Every route prints through the one Console; panels have to survive."""
    from rich.console import Console
    from rich.panel import Panel

    pane = _pane(width=60)
    console = Console(force_terminal=True, color_system="truecolor", width=60)
    console.file = PaneWriter(pane, lambda: None)
    console.print(Panel("Context Status", width=40))

    text = pane.plain(10)
    assert "Context Status" in text
    assert "─" in text or "-" in text


# -- the sidebar -----------------------------------------------------------


def _telemetry() -> TurnTelemetry:
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("turn.start", "fix it"))
    meter.absorb(
        Event(
            "status",
            "reading config.py",
            round=14,
            max_rounds=24,
            ctx_pct=71,
            ctx_used=23_300,
            ctx_window=32_768,
            contracts_open=1,
        )
    )
    meter.absorb(Event("tool.result", "ok", tool="patch_file", target="config.py", ok=True))
    return meter


def test_the_sidebar_shows_everything_the_toolbar_had_to_give_up():
    """The whole argument for the split: a column has room for all of it at
    once, where the bottom row shed cells as the terminal narrowed."""
    meter = _telemetry()
    meter.feedback_depth = lambda: 1
    meter.tasks_depth = lambda: 2
    text = "".join(t for _s, t in render_sidebar(meter))

    assert "14/24" in text
    assert "71%" in text
    assert "23.3k / 32.8k" in text
    assert "config.py" in text
    assert "feedback  1" in text
    assert "queued    2" in text


# -- what the turn actually cost -------------------------------------------
#
# A turn that ran 22 minutes and changed nothing reads identically to a
# productive one until these are on screen. All of them already existed in the
# runtime and none of them were shown anywhere.


def _busy() -> TurnTelemetry:
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("turn.start", "fix it"))
    for _ in range(12):
        meter.absorb(Event("activity", "model responded in 41s", model_seconds=41.0))
    for _ in range(8):
        meter.absorb(
            Event("tool.result", "ok", tool="contract_status", ok=True, duration_ms=6)
        )
    for _ in range(4):
        meter.absorb(
            Event("tool.result", "no", tool="patch_file", ok=False, duration_ms=120)
        )
    return meter


def test_the_time_split_between_model_and_tools_is_counted():
    """The diagnosis of a slow turn: 12 model calls at 8m12s against 12 tool
    calls at under a second is not a tool problem."""
    meter = _busy()
    assert meter.model_calls == 12
    assert round(meter.model_seconds) == 492
    assert meter.tool_calls == 12
    assert meter.tool_seconds < 1

    text = "".join(t for _s, t in render_sidebar(meter))
    assert "12 · 8m12s" in text


def test_generation_speed_rides_in_on_the_status_event():
    """From Ollama's own `eval_duration`, not a stopwatch around the call - so
    it excludes queueing, the HTTP round trip and prompt evaluation."""
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("turn.start"))
    meter.absorb(Event("status", "thinking", tokens_per_second=34.2))

    assert round(meter.tokens_per_second, 1) == 34.2
    assert "34 tok/s" in "".join(t for _s, t in render_sidebar(meter))


def test_a_model_that_has_fallen_off_the_gpu_is_flagged_red():
    """A 7-9B at q4 runs in the tens of tokens a second on the card and in low
    single digits on the CPU. The gap is not subtle, and catching the fall is
    the whole reason to show the number."""
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("turn.start"))

    meter.absorb(Event("status", "x", tokens_per_second=3.0))
    assert any("alarm" in s for s, t in render_sidebar(meter) if "3 tok/s" in t)

    meter.absorb(Event("status", "x", tokens_per_second=14.0))
    assert any("warn" in s for s, t in render_sidebar(meter) if "14 tok/s" in t)

    meter.absorb(Event("status", "x", tokens_per_second=41.0))
    assert any("ok" in s for s, t in render_sidebar(meter) if "41 tok/s" in t)


def test_an_unmeasured_speed_is_not_reported_as_zero():
    """A model that has not answered yet has no speed; `0 tok/s` is a claim."""
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("turn.start"))
    assert "tok/s" not in "".join(t for _s, t in render_sidebar(meter))


def test_speed_is_forgotten_with_the_rest_of_the_turn():
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("status", "x", tokens_per_second=34.0))
    meter.absorb(Event("turn.start"))
    assert meter.tokens_per_second == 0.0


def test_failed_tool_calls_are_counted_and_shown_in_red():
    meter = _busy()
    assert meter.tool_failures == 4

    rows = render_sidebar(meter)
    styles = {style for style, text in rows if "4" in text and "failed" not in text}
    assert any("alarm" in style for style in styles), "failures were not flagged"


def test_no_failures_is_not_flagged():
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("turn.start"))
    meter.absorb(Event("tool.result", "ok", tool="read_file", ok=True, duration_ms=5))
    rows = render_sidebar(meter)
    failed_row = [
        style for style, text in rows if text.strip() == "0"
    ]
    assert failed_row and all("alarm" not in style for style in failed_row)


def test_the_busiest_tool_is_named_with_its_count():
    """`contract_status x8` is the signature of a stuck run, and the count is
    the whole point - truncating it to `contract_status x` throws away the
    only number on the row."""
    meter = _busy()
    assert meter.busiest_tool() == ("contract_status", 8)

    text = "".join(t for _s, t in render_sidebar(meter))
    assert "contract_status x8" in text


def test_a_tool_called_once_is_not_reported_as_repeated():
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("turn.start"))
    meter.absorb(Event("tool.result", "ok", tool="read_file", ok=True, duration_ms=5))
    text = "".join(t for _s, t in render_sidebar(meter))
    assert "repeated" not in text


def test_thrashing_is_amber_and_a_pair_is_not():
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("turn.start"))
    for _ in range(2):
        meter.absorb(Event("tool.result", "ok", tool="read_file", ok=True))
    quiet = [s for s, t in render_sidebar(meter) if "read_file x2" in t]
    assert quiet and all("warn" not in s for s in quiet)

    meter.absorb(Event("tool.result", "ok", tool="read_file", ok=True))
    loud = [s for s, t in render_sidebar(meter) if "read_file x3" in t]
    assert loud and all("warn" in s for s in loud)


def test_the_round_budget_warns_as_it_runs_out():
    """Rounds run out too, and running out is how a turn ends with nothing."""
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("turn.start"))

    meter.absorb(Event("status", "x", round=2, max_rounds=24))
    assert any("ok" in s for s, t in render_sidebar(meter) if "2/24" in t)

    meter.absorb(Event("status", "x", round=16, max_rounds=24))
    assert any("warn" in s for s, t in render_sidebar(meter) if "16/24" in t)

    meter.absorb(Event("status", "x", round=22, max_rounds=24))
    assert any("alarm" in s for s, t in render_sidebar(meter) if "22/24" in t)


def test_the_verdict_replaces_the_spinner_when_the_turn_ends():
    meter = _busy()
    meter.absorb(Event("turn.end", "done", status="failed"))
    rows = render_sidebar(meter)
    text = "".join(t for _s, t in rows)
    assert "failed" in text
    assert any("alarm" in style for style, body in rows if "failed" in body)

    meter.absorb(Event("turn.end", "done", status="done"))
    ok_rows = render_sidebar(meter)
    assert any("ok" in style for style, body in ok_rows if "done" in body)


def test_a_new_turn_resets_the_cost():
    """Carrying the last turn's spend into this one would report finished work
    as if it were still happening."""
    meter = _busy()
    meter.absorb(Event("turn.start"))
    assert meter.model_calls == 0
    assert meter.tool_calls == 0
    assert meter.tool_failures == 0
    assert meter.busiest_tool() == ("", 0)
    assert meter.verdict == ""


def test_the_sidebar_says_when_the_context_is_unmeasured():
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("turn.start"))
    text = "".join(t for _s, t in render_sidebar(meter))
    assert "not measured" in text
    assert "0%" not in text


def test_the_sidebar_caps_the_file_list_and_says_how_many_are_hidden():
    meter = TurnTelemetry(unicode_ui=True)
    meter.absorb(Event("turn.start"))
    for n in range(9):
        meter.absorb(Event("tool.result", "ok", tool="write_file", target=f"f{n}.js", ok=True))
    text = "".join(t for _s, t in render_sidebar(meter))
    assert "+3 more" in text


def test_every_sidebar_row_fits_the_column():
    meter = _telemetry()
    for _style, text in render_sidebar(meter, width=30):
        assert len(text.rstrip("\n")) <= 29, repr(text)


def test_a_broken_telemetry_object_does_not_kill_the_frame():
    """An exception raised while painting a control tears down the whole
    Application - so the sidebar degrades to a message instead."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    class Broken:
        active = True

        def __getattr__(self, name):
            raise RuntimeError("nope")

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=Broken(), on_submit=lambda _t: None)
        fragments = app._sidebar_fragments()

    assert "telemetry unavailable" in "".join(text for _style, text in fragments)


# -- the frame -------------------------------------------------------------


def _app_session():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    return create_pipe_input(), create_app_session, DummyOutput


@pytest.mark.asyncio
async def test_the_frame_runs_and_routes_what_is_typed():
    """The real Application, over prompt_toolkit's pipe input. A layout that
    cannot be constructed, or a control that raises while painting, fails
    here rather than on the user's terminal."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from rich.console import Console

    submitted: list[str] = []
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=_telemetry(), on_submit=submitted.append)
        console = Console(force_terminal=True, color_system="truecolor", width=60)
        console.file = PaneWriter(app.pane, app.invalidate)
        console.print("[red]hello[/red] from the pane")

        task = asyncio.ensure_future(app.app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("you are editing the wrong file\n")
        await asyncio.sleep(0.05)
        app.app.exit()
        await task

    assert submitted == ["you are editing the wrong file"]
    assert "hello from the pane" in app.pane.plain(10)


@pytest.mark.asyncio
async def test_the_wheel_scrolls_the_log_and_nothing_else():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=_telemetry(), on_submit=lambda _t: None)
        app.pane.set_width(40)
        for n in range(200):
            app.pane.write(f"line {n}\n")
        app.pane.visible(10)
        assert app.pane.follow

        up = MouseEvent(position=None, event_type=MouseEventType.SCROLL_UP, button=None, modifiers=None)
        app._wheel(up)
        assert not app.pane.follow, "the wheel did not scroll the log"
        moved = app.pane.offset

        down = MouseEvent(position=None, event_type=MouseEventType.SCROLL_DOWN, button=None, modifiers=None)
        app._wheel(down)
        assert app.pane.offset > moved


@pytest.mark.asyncio
async def test_an_unrelated_mouse_event_is_ignored():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=_telemetry(), on_submit=lambda _t: None)
        for n in range(50):
            app.pane.write(f"line {n}\n")
        app.pane.visible(10)
        before = app.pane.offset
        app._wheel(
            MouseEvent(
                position=None,
                event_type=MouseEventType.MOUSE_DOWN,
                button=None,
                modifiers=None,
            )
        )
        assert app.pane.offset == before


@pytest.mark.asyncio
async def test_mouse_capture_can_be_turned_off_without_leaving_the_tui():
    """Capture takes click-drag away from the terminal's own selection, and
    not every terminal offers Shift as an escape hatch."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=_telemetry(), on_submit=lambda _t: None)
        assert app._mouse
        task = asyncio.ensure_future(app.app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("\x1b[12~")  # F2
        await asyncio.sleep(0.05)
        app.app.exit()
        await task

    assert not app._mouse
    assert "MOUSE OFF" in "".join(text for _style, text in app._statusline())


@pytest.mark.asyncio
async def test_ctrl_c_inside_the_frame_stops_the_turn():
    """prompt_toolkit owns the keyboard in full-screen mode, so the SIGINT
    handler never fires - the binding has to reach the same place."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    stopped: list[int] = []
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(
            telemetry=_telemetry(),
            on_submit=lambda _t: None,
            on_interrupt=lambda: stopped.append(1),
        )
        task = asyncio.ensure_future(app.app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("\x03")
        await asyncio.sleep(0.05)
        app.app.exit()
        await task

    assert stopped == [1]


@pytest.mark.asyncio
async def test_a_submit_that_raises_does_not_take_the_frame_down():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    def boom(_text: str) -> None:
        raise RuntimeError("nope")

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=_telemetry(), on_submit=boom)
        task = asyncio.ensure_future(app.app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("anything\n")
        await asyncio.sleep(0.05)
        assert app.app.is_running, "one bad submit closed the whole frame"
        app.app.exit()
        await task

    assert "nope" in app.pane.plain(10)


# -- the frame is a MODE, not a decoration on one turn ----------------------
#
# Reported live: "why the fuck is it going back to the normal cli after opening
# the tui, and in tui i don't see the chat history, nothing shows properly".
# Both symptoms were one mistake - the frame was built inside the turn
# dispatcher and exited when the turn ended, so it flashed up and dropped back,
# and the pane was a NEW pane every time so no conversation ever accumulated.


def _host():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from shamsu.cli.tui import FrameHost

    pipe_cm = create_pipe_input()
    pipe = pipe_cm.__enter__()
    session_cm = create_app_session(input=pipe, output=DummyOutput())
    session_cm.__enter__()
    app = TuiApp(telemetry=_telemetry(), on_submit=lambda text: host.submit(text))
    host = FrameHost(app)
    return host, app, (pipe_cm, session_cm)


def test_the_frame_stays_up_across_a_turn():
    """It used to be torn down when the turn ended, which is what "it goes back
    to the normal CLI" was."""
    host, app, cms = _host()
    try:
        assert host.start(), "the frame did not come up"
        assert host.running

        host.turn_active = True
        host.turn_active = False  # a whole turn, start to finish

        assert host.running, "the turn ending closed the frame"
        assert app.app.is_running
    finally:
        host.stop()
        for cm in reversed(cms):
            cm.__exit__(None, None, None)


def test_the_pane_keeps_the_whole_conversation():
    """A new pane per turn is why nothing showed: the scrollback IS the
    conversation, so it has to outlive the turn that produced it."""
    from rich.console import Console

    from shamsu.cli.tui import PaneWriter

    host, app, cms = _host()
    try:
        host.start()
        console = Console(force_terminal=True, color_system="truecolor", width=60)
        console.file = PaneWriter(app.pane, app.invalidate)

        console.print("first answer")
        host.turn_active = False
        console.print("second answer")

        text = app.pane.plain(40)
        assert "first answer" in text, "the earlier turn was lost"
        assert "second answer" in text
    finally:
        host.stop()
        for cm in reversed(cms):
            cm.__exit__(None, None, None)


def test_an_idle_line_becomes_the_next_prompt():
    host, app, cms = _host()
    try:
        host.start()
        host.turn_active = False
        host.submit("fix the tests")
        assert host.read_line(timeout=2) == "fix the tests"
        assert "fix the tests" in app.pane.plain(20), "the prompt was not echoed"
    finally:
        host.stop()
        for cm in reversed(cms):
            cm.__exit__(None, None, None)


def test_a_mid_turn_line_steers_instead_of_queueing_a_prompt():
    """One input box, two meanings. Sending a steer to the prompt queue would
    make it the NEXT request instead of a correction to this one."""
    import queue as queue_module

    host, app, cms = _host()
    routed: list[str] = []
    host.on_route = routed.append
    try:
        host.start()
        host.turn_active = True
        host.submit("you are editing the wrong file")

        assert routed == ["you are editing the wrong file"]
        with pytest.raises(queue_module.Empty):
            host.read_line(timeout=0.2)
        assert "wrong file" in app.pane.plain(20)
    finally:
        host.stop()
        for cm in reversed(cms):
            cm.__exit__(None, None, None)


def test_closing_the_frame_unblocks_whoever_is_waiting_for_a_line():
    """`main()` blocks on `read_line`. A frame that died without saying so
    would hang the whole REPL."""
    host, _app, cms = _host()
    try:
        host.start()
        host.stop()
        with pytest.raises(EOFError):
            host.read_line(timeout=2)
    finally:
        for cm in reversed(cms):
            cm.__exit__(None, None, None)


# -- the input box ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_input_box_takes_more_than_one_line():
    """A one-line box is fine for "fix the tests" and useless for the thing
    people actually paste - a PRD, a traceback, a spec with eight bullets."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    submitted: list[str] = []
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=_telemetry(), on_submit=submitted.append)
        assert app.buffer.multiline(), "the box is still one line"

        task = asyncio.ensure_future(app.app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("first line")
        pipe.send_text("\x1b\r")  # Alt+Enter: a new line, NOT a submit
        await asyncio.sleep(0.05)
        assert submitted == [], "Alt+Enter submitted instead of opening a line"
        pipe.send_text("second line\r")  # Enter: submit
        await asyncio.sleep(0.05)
        app.app.exit()
        await task

    assert submitted == ["first line\nsecond line"]


@pytest.mark.asyncio
async def test_the_box_completes_slash_commands():
    """The completer was simply never passed, AND a hand-made `Layout` has no
    completions menu - `PromptSession` builds one for you and this does not, so
    completions were computed and had nowhere to be drawn."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from shamsu.cli.repl import SlashCommandCompleter

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(
            telemetry=_telemetry(),
            on_submit=lambda _t: None,
            completer=SlashCommandCompleter(Path(".")),
        )
        task = asyncio.ensure_future(app.app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("/qu")
        await asyncio.sleep(0.3)

        state = app.buffer.complete_state
        assert state is not None, "nothing completed"
        offered = [completion.text for completion in state.completions]
        assert "/queue" in offered

        pipe.send_text("\t")
        await asyncio.sleep(0.2)
        chosen = app.buffer.complete_state
        assert chosen is not None and chosen.current_completion is not None
        app.app.exit()
        await task


def test_the_layout_has_somewhere_to_draw_completions():
    """Without a float anchored to the cursor the menu exists nowhere."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.containers import FloatContainer
    from prompt_toolkit.layout.menus import CompletionsMenu
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(telemetry=_telemetry(), on_submit=lambda _t: None)

    container = app.layout.container
    assert isinstance(container, FloatContainer)
    assert any(
        isinstance(float_.content, CompletionsMenu) for float_ in container.floats
    )


@pytest.mark.asyncio
async def test_the_box_suggests_from_what_you_typed_before():
    """Ghost text. The idle prompt has had history for as long as it has
    existed; the frame's box had neither this nor a menu."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    history = InMemoryHistory()
    history.append_string("fix the failing tests")

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        app = TuiApp(
            telemetry=_telemetry(), on_submit=lambda _t: None, history=history
        )
        task = asyncio.ensure_future(app.app.run_async())
        await asyncio.sleep(0.05)
        pipe.send_text("fix the")
        await asyncio.sleep(0.3)

        suggestion = app.buffer.suggestion
        assert suggestion is not None, "no suggestion from history"
        assert suggestion.text == " failing tests"

        pipe.send_text("\x1b[C")  # right arrow accepts it
        await asyncio.sleep(0.2)
        assert app.buffer.text == "fix the failing tests"
        app.app.exit()
        await task


def test_the_input_box_grows_but_is_bounded():
    """It has to hold a pasted traceback without swallowing the log."""
    from shamsu.cli.tui import INPUT_MAX_ROWS

    assert 1 < INPUT_MAX_ROWS <= 12


# -- what is running around the session ------------------------------------


def test_services_are_sampled_not_polled(tmp_path):
    """The toolbar repaints five times a second and these answers come from a
    SQLite lease table - one query per 200ms for a number that changes when you
    type a command would be absurd."""
    from shamsu.cli.tui import Services

    services = Services(tmp_path)
    taken: list[int] = []

    def counted() -> tuple[str, str]:
        taken.append(1)
        return ("running", "class:tb.ok")

    services._telegram = counted
    for _ in range(20):
        services.read()
    assert len(taken) == 1, "the sidebar hit the database on every repaint"


def test_services_refresh_once_the_reading_is_stale(tmp_path, monkeypatch):
    from shamsu.cli import tui
    from shamsu.cli.tui import Services

    services = Services(tmp_path)
    services.read()
    monkeypatch.setattr(
        tui.time, "monotonic", lambda: services._taken + tui.SERVICES_TTL_SECONDS + 1
    )
    taken: list[int] = []
    services._web = lambda: (taken.append(1), ("off", "class:tb.dim"))[1]
    services.read()
    assert taken == [1]


def test_what_ollama_is_holding_is_read_from_api_ps(tmp_path, monkeypatch):
    """Ollama reserves the KV cache for the WHOLE window up front, so a window
    that does not fit spills to the CPU and the same turn takes six times as
    long. "Loaded, 6.2G" and "not loaded" are different worlds."""
    from shamsu.cli.tui import Services

    class Response:
        @staticmethod
        def json():
            return {
                "models": [
                    {"size_vram": 6_207_559_433, "context_length": 32768}
                ]
            }

    monkeypatch.setattr("httpx.get", lambda *_a, **_k: Response())
    assert Services(tmp_path)._vram() == ("6.2G · 32k", "class:tb.ok")


def test_a_model_running_on_the_cpu_is_an_alarm(tmp_path, monkeypatch):
    from shamsu.cli.tui import Services

    class Response:
        @staticmethod
        def json():
            return {"models": [{"size_vram": 0, "context_length": 32768}]}

    monkeypatch.setattr("httpx.get", lambda *_a, **_k: Response())
    value, style = Services(tmp_path)._vram()
    assert value == "on cpu"
    assert "alarm" in style


def test_nothing_loaded_is_said_plainly(tmp_path, monkeypatch):
    from shamsu.cli.tui import Services

    class Response:
        @staticmethod
        def json():
            return {"models": []}

    monkeypatch.setattr("httpx.get", lambda *_a, **_k: Response())
    assert Services(tmp_path)._vram()[0] == "nothing loaded"


def test_an_unreachable_ollama_does_not_raise_into_the_renderer(tmp_path, monkeypatch):
    from shamsu.cli.tui import Services

    def refuse(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr("httpx.get", refuse)
    assert Services(tmp_path)._vram() == ("unknown", "class:tb.dim")


def test_this_processes_own_memory_is_reported(tmp_path):
    from shamsu.cli.tui import Services

    value, _style = Services(tmp_path)._ram()
    assert value.endswith(" MB")
    assert float(value.removesuffix(" MB")) > 0


def test_a_service_that_cannot_be_reached_reads_unknown(tmp_path, monkeypatch):
    """An unreadable control DB must be a grey word, not a crash in a renderer
    that would take the frame down."""
    from shamsu.cli.tui import Services

    services = Services(tmp_path)
    monkeypatch.setattr(
        "shamsu.integrations.telegram.service.poller_status",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no db")),
    )
    assert services._telegram() == ("unknown", "class:tb.dim")


def test_the_sidebar_shows_the_services_when_it_is_given_them(tmp_path):
    from shamsu.cli.tui import Services

    services = Services(tmp_path)
    services._telegram = lambda: ("running", "class:tb.ok")
    services._web = lambda: ("off", "class:tb.dim")
    services._model = lambda: ("qwen3.5:9b", "class:tui.val")

    text = "".join(t for _s, t in render_sidebar(_telemetry(), services=services))
    assert "SERVICES" in text
    assert "telegram" in text and "running" in text
    assert "qwen3.5:9b" in text


def test_the_sidebar_is_unchanged_when_no_services_are_given():
    """The panel is optional - a test or a surface without a workspace should
    not be forced to construct one."""
    text = "".join(t for _s, t in render_sidebar(_telemetry()))
    assert "SERVICES" not in text


def test_the_tui_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("SHAMSU_TUI", raising=False)
    assert not tui_enabled()
    monkeypatch.setenv("SHAMSU_TUI", "1")
    assert tui_enabled()


# -- handing the terminal to an approval -----------------------------------


@pytest.mark.asyncio
async def test_an_approval_suspends_the_frame_and_gets_it_back():
    """Two prompt_toolkit applications cannot run at once, so the frame has to
    give the real terminal up for exactly as long as the question lasts."""
    from shamsu.agents.simple_feedback import FeedbackQueue, TaskQueue
    from shamsu.cli.live_console import LiveConsole
    from shamsu.safety.approval import on_prompt_close, on_prompt_open, reading_input

    live = LiveConsole(
        session_factory=lambda _t: None,
        feedback=FeedbackQueue(),
        tasks=TaskQueue(),
        on_command=lambda _t: None,
        notify=lambda _t: None,
        prompt_is_active=lambda: False,
    )
    live._loop = asyncio.get_running_loop()

    suspended: list[str] = []

    class Frame:
        async def suspended(self, func):
            suspended.append("in")
            return func()

    live.set_frame(Frame())
    release_open = on_prompt_open(live.stand_down)
    release_close = on_prompt_close(live.resume)
    try:
        with reading_input():
            await asyncio.sleep(0)
            assert live._handover is not None, "the frame was never handed over"
        await asyncio.sleep(0)
        assert live._handover is None, "the frame never took the terminal back"
    finally:
        release_open()
        release_close()


@pytest.mark.asyncio
async def test_clearing_the_frame_releases_a_pending_handover():
    """A turn that ends mid-approval must not leave a waiter parked forever."""
    from shamsu.agents.simple_feedback import FeedbackQueue, TaskQueue
    from shamsu.cli.live_console import LiveConsole

    live = LiveConsole(
        session_factory=lambda _t: None,
        feedback=FeedbackQueue(),
        tasks=TaskQueue(),
        on_command=lambda _t: None,
        notify=lambda _t: None,
        prompt_is_active=lambda: False,
    )
    import threading

    waiter = threading.Event()
    live._handover = waiter
    live.set_frame(None)
    assert waiter.is_set()


def test_the_sidebar_is_dropped_on_a_terminal_too_narrow_for_it(monkeypatch):
    """A 30-column pane out of 70 leaves the log unreadable; below the
    threshold the telemetry falls back to the bottom toolbar."""
    from shamsu.cli import tui

    assert MIN_WIDTH_FOR_SIDEBAR > tui.SIDEBAR_WIDTH * 2


# --- R9: the approval handover across NESTED prompts -------------------------
#
# `reading_input()` keeps a `_PROMPT_DEPTH` because one prompt may open inside
# another. The handover did not: `_suspend_frame` overwrote `self._handover`
# with the inner waiter, so the outer one was never set and its
# `run_in_terminal` callable sat on the full 900s - a frame that does not come
# back is a terminal the user cannot reach.


def _console_with_frame(with_loop: bool = True):
    """A LiveConsole with just the handover fields, and no event loop running.

    `_suspend_frame` hands its coroutine to `asyncio.run_coroutine_threadsafe`
    inside `contextlib.suppress(Exception)`, so a dummy loop object exercises
    every line of the bookkeeping without needing a real one.
    """
    from shamsu.cli.live_console import LiveConsole

    live = LiveConsole.__new__(LiveConsole)
    live._handover = None
    live._handover_depth = 0
    live._frame = object()
    live._loop = object() if with_loop else None
    return live


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_a_nested_prompt_does_not_strand_the_outer_handover():
    live = _console_with_frame()

    live._suspend_frame()                       # the outer prompt opens
    outer = live._handover
    assert outer is not None and live._handover_depth == 1

    live._suspend_frame()                       # a prompt opens inside it
    assert live._handover is outer, "the inner prompt must not replace the waiter"
    assert live._handover_depth == 2

    live.resume()                               # inner closes
    assert not outer.is_set(), "the frame must not return while a prompt is up"
    assert live._handover_depth == 1

    live.resume()                               # outer closes
    assert outer.is_set(), "the last release hands the terminal back"
    assert live._handover is None


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
def test_one_prompt_hands_over_and_takes_back_as_before():
    """The ordinary, unnested case must be untouched by the depth counting."""
    live = _console_with_frame()

    live._suspend_frame()
    waiter = live._handover
    assert waiter is not None and not waiter.is_set()

    live.resume()

    assert waiter.is_set()
    assert live._handover_depth == 0


def test_no_handover_is_attempted_without_a_loop_to_run_it_on():
    live = _console_with_frame(with_loop=False)

    live._suspend_frame()

    assert live._handover is None
    assert live._handover_depth == 0


def test_resume_without_a_suspend_is_harmless():
    """`resume` is registered as an unconditional on_prompt_close callback, so
    it fires for prompts that never suspended a frame at all."""
    live = _console_with_frame()

    live.resume()
    live.resume()

    assert live._handover_depth == 0


def test_tearing_the_frame_down_releases_whatever_is_outstanding():
    import threading

    live = _console_with_frame()
    stuck = threading.Event()
    live._handover = stuck
    live._handover_depth = 3  # an unbalanced open - a prompt that raised

    live.set_frame(None)

    assert stuck.is_set(), "the turn is over; nothing else will release it"
    assert live._handover_depth == 0
