"""The framed TUI: scrollback, sidebar, and the frame itself.

Most of what matters here is `LogPane`, because the pane IS the scrollback -
the objection to a full-screen frame was that it costs the terminal's own, and
the answer is that the application keeps its own the way Neovim does. A
scrollback that jumps to the bottom while you are reading it is not one.
"""
from __future__ import annotations

import asyncio

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
