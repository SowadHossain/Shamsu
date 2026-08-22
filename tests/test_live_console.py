"""The pinned prompt, the telemetry toolbar, and the side dispatcher.

The thing being replaced was forty lines of raw `msvcrt.getwch()` with no echo
and no routing, so most of what is worth asserting here is behaviour that had
no code to test before: that a slash command never reaches the model, that a
task and a correction go to different queues, and that a number on the toolbar
came from the turn stream rather than from a guess.
"""
from __future__ import annotations

import asyncio

import pytest

from shamsu.agents.simple_feedback import FeedbackQueue, TaskQueue
from shamsu.cli.live_console import (
    ROUTE_COMMAND,
    ROUTE_DEFERRED,
    ROUTE_EMPTY,
    ROUTE_FEEDBACK,
    LiveConsole,
    ToolbarStatus,
    TurnTelemetry,
    route_input,
    supports_unicode,
)


class Event:
    """A turn-stream event, as the renderers see it."""

    def __init__(self, kind: str, text: str = "", **data) -> None:
        self.kind = kind
        self.text = text
        self.data = data


# -- the side dispatcher ----------------------------------------------------
#
# The rule that keeps read-only questions out of the message array. Before it,
# every keystroke went to the same queue and reached the model, so asking "how
# full is my context?" cost context to ask - and the answer came back a round
# later, phrased by a 7B that had to be told the number first.


def test_plain_text_is_a_steer_for_the_model():
    assert route_input("also log a warning", midturn=True) == (
        ROUTE_FEEDBACK,
        "also log a warning",
    )


def test_a_read_only_command_runs_locally_and_never_reaches_the_model():
    route, payload = route_input("/context status", midturn=True)
    assert route == ROUTE_COMMAND
    assert payload == "/context status"


def test_a_state_changing_command_is_deferred_not_run():
    """`/compact clear` mid-turn would rewrite the history the model is being
    sent on the very next round."""
    route, _payload = route_input("/compact clear", midturn=True)
    assert route == ROUTE_DEFERRED


def test_every_command_is_allowed_at_an_idle_prompt():
    """The whitelist exists because a turn is in flight, not because the
    commands are dangerous in themselves."""
    assert route_input("/compact clear", midturn=False)[0] == ROUTE_COMMAND


def test_nothing_typed_routes_nowhere():
    assert route_input("   ", midturn=True) == (ROUTE_EMPTY, "")
    assert route_input("", midturn=True) == (ROUTE_EMPTY, "")


def test_routing_ignores_case_and_surrounding_space():
    route, payload = route_input("  /CONTEXT status  ", midturn=True)
    assert route == ROUTE_COMMAND
    assert payload == "/CONTEXT status"


def test_a_slash_inside_a_sentence_is_still_feedback():
    """Only a LEADING slash is a command. "use / as the separator" is a steer."""
    assert route_input("use / as the separator", midturn=True)[0] == ROUTE_FEEDBACK


# -- the telemetry the toolbar shows ----------------------------------------
#
# Every one of these numbers already existed in the runtime and none of them
# were ever shown: you found out you had blown the context window by watching
# the run degrade.


def _telemetry() -> TurnTelemetry:
    return TurnTelemetry(unicode_ui=True)


def test_the_meters_come_from_the_turn_stream():
    meter = _telemetry()
    meter.absorb(Event("turn.start", "fix the thing"))
    meter.absorb(
        Event(
            "status",
            "thinking... 12s",
            round=8,
            max_rounds=24,
            ctx_pct=41,
            ctx_used=13_200,
            ctx_window=32_768,
            contracts_open=2,
        )
    )

    assert meter.round == 8
    assert meter.max_rounds == 24
    assert meter.ctx_pct == 41
    assert meter.contracts == 2
    row = meter.plain(120)
    assert "rnd 8/24" in row
    assert "41%" in row
    assert "13.2k/32.8k" in row


def test_a_write_puts_the_file_on_the_toolbar():
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    meter.absorb(Event("tool.result", "ok", tool="patch_file", target="js/Ship.js", ok=True))

    assert meter.files == ["js/Ship.js"]
    assert "js/Ship.js" in meter.plain(120)


def test_a_read_does_not_count_as_a_file_modified():
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    meter.absorb(Event("tool.result", "ok", tool="read_file", target="js/Ship.js", ok=True))
    assert meter.files == []


def test_a_failed_write_does_not_count():
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    meter.absorb(
        Event("tool.result", "failed", tool="patch_file", target="js/Ship.js", ok=False)
    )
    assert meter.files == []


def test_the_same_file_written_twice_is_listed_once():
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    for _ in range(3):
        meter.absorb(
            Event("tool.result", "ok", tool="patch_file", target="js/Ship.js", ok=True)
        )
    assert meter.files == ["js/Ship.js"]


def test_the_directory_survives_so_two_files_cannot_read_as_one():
    """`js/config.py` and `css/config.py` are different files, and a toolbar
    that calls both `config.py` is worse than one that says nothing."""
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    meter.absorb(Event("tool.result", "ok", tool="write_file", target="js/config.py", ok=True))
    assert "js/config.py" in meter.files_label()


def test_many_files_collapse_to_a_count():
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    for name in ("a.js", "b.js", "c.js", "d.js"):
        meter.absorb(Event("tool.result", "ok", tool="write_file", target=name, ok=True))
    assert meter.files_label() == "4 files"


def test_a_long_path_keeps_the_end_that_names_the_file():
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    meter.absorb(
        Event(
            "tool.result",
            "ok",
            tool="write_file",
            target="src/very/deeply/nested/PlayerShip.js",
            ok=True,
        )
    )
    assert meter.files_label().endswith("PlayerShip.js")


def test_a_new_turn_starts_from_zero():
    """Carrying the last turn's files and rounds into this one would report
    work that is already finished as if it were still happening."""
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    meter.absorb(Event("status", "x", round=8, max_rounds=24))
    meter.absorb(Event("tool.result", "ok", tool="write_file", target="a.js", ok=True))

    meter.absorb(Event("turn.start"))
    assert meter.files == []
    assert meter.round == 0


def test_the_turn_ending_stops_the_clock():
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    assert meter.active
    meter.absorb(Event("turn.end", "done"))
    assert not meter.active
    assert "idle" in meter.plain(120)


def test_the_context_meter_warns_at_sixty_and_alarms_at_eighty():
    """The same thresholds the CLI renderer uses, so one number does not read
    as two different warnings on two parts of the screen."""
    meter = _telemetry()
    meter.absorb(Event("status", "x", ctx_pct=41))
    assert meter.context_style().endswith("ok")
    meter.absorb(Event("status", "x", ctx_pct=71))
    assert meter.context_style().endswith("warn")
    meter.absorb(Event("status", "x", ctx_pct=84))
    assert meter.context_style().endswith("alarm")


def test_the_bar_fills_with_the_percentage():
    meter = _telemetry()
    meter.absorb(Event("status", "x", ctx_pct=0))
    assert meter.context_bar().count("█") == 0
    meter.absorb(Event("status", "x", ctx_pct=100))
    assert meter.context_bar().count("░") == 0
    meter.absorb(Event("status", "x", ctx_pct=50))
    assert meter.context_bar().count("█") == 5


def test_an_unknown_context_says_so_rather_than_showing_zero():
    """A meter reading 0% is a claim. An empty window is not the same as an
    unmeasured one."""
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    assert meter.ctx_pct is None
    assert "0%" not in meter.plain(120)


def test_the_queue_depths_are_read_live():
    """Held as callables, so the toolbar cannot go stale between a push and
    the next turn-stream event."""
    meter = _telemetry()
    feedback, tasks = FeedbackQueue(), TaskQueue()
    meter.feedback_depth = lambda: len(feedback)
    meter.tasks_depth = lambda: len(tasks)

    assert "fb 0" in meter.plain(120)
    feedback.push("wrong file")
    tasks.push("write the tests")
    tasks.push("then run them")
    row = meter.plain(120)
    assert "fb 1" in row
    assert "tasks 2" in row


def test_the_row_is_clipped_to_the_terminal():
    """A toolbar wider than the terminal wraps, and a wrapped toolbar pushes
    the prompt off the bottom of the screen."""
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    meter.absorb(Event("status", "x" * 200, round=8, max_rounds=24, ctx_pct=41))
    for line in meter.plain(60).split("\n"):
        assert len(line) <= 60, line


def test_a_narrow_terminal_keeps_the_queues_and_gives_up_the_file_list():
    """The row narrows from the LEFT. Clipping trims the end, and the end is
    where the queue depths are - so straight clipping made the number telling
    you your steer was accepted the first thing to disappear."""
    meter = _telemetry()
    meter.feedback_depth = lambda: 2
    meter.tasks_depth = lambda: 1
    meter.absorb(Event("turn.start"))
    meter.absorb(
        Event(
            "status",
            "contract_status",
            round=14,
            max_rounds=24,
            ctx_pct=71,
            ctx_used=23_300,
            ctx_window=32_768,
            contracts_open=1,
        )
    )
    for name in ("config.py", "js/PlayerShip.js"):
        meter.absorb(Event("tool.result", "ok", tool="write_file", target=name, ok=True))

    for width in (110, 100, 80, 70, 62, 50, 44):
        row = meter.plain(width).split("\n")[1]
        assert len(row) <= width, f"{width}: {row!r}"
        assert "rnd 14/24" in row, f"the round budget was given up at {width}"
        assert "71%" in row, f"the context percentage was given up at {width}"
        assert "fb 2" in row, f"the feedback depth was given up at {width}"
        assert "tasks 1" in row, f"the task depth was given up at {width}"

    assert "js/PlayerShip.js" in meter.plain(110).split("\n")[1]
    assert "2 files" in meter.plain(100).split("\n")[1]
    assert "files" not in meter.plain(70).split("\n")[1]


def test_rich_markup_is_stripped_from_the_status_line():
    """The status arrives composed for a rich console; prompt_toolkit would
    print the brackets literally."""
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    meter.set_status("[yellow]ctx 71% (23.3k/32.8k)[/yellow] | [dim]rnd 14/25[/dim]")
    row = meter.plain(120)
    assert "ctx 71% (23.3k/32.8k)" in row
    assert "[yellow]" not in row
    assert "[/dim]" not in row


def test_a_malformed_event_never_takes_the_prompt_down():
    """An exception raised inside a toolbar renderer kills the prompt, and a
    wrong number is better than no terminal."""
    meter = _telemetry()
    meter.absorb(object())
    meter.absorb(Event("status", "x", round="not a number", ctx_pct="nope"))
    meter.absorb(Event("tool.result", "ok", tool=None, target=None, ok=True))
    assert meter.plain(120)


def test_the_spinner_advances_on_every_repaint():
    meter = _telemetry()
    meter.absorb(Event("turn.start"))
    frames = set()
    for _ in range(4):
        frames.add(meter.spinner())
        meter.tick()
    assert len(frames) == 4


def test_ascii_only_terminals_get_ascii_glyphs():
    """A Windows console left on cp1252 raises UnicodeEncodeError on the
    braille frames."""
    meter = TurnTelemetry(unicode_ui=False)
    meter.absorb(Event("turn.start"))
    meter.absorb(Event("status", "working", ctx_pct=50))
    row = meter.plain(120)
    assert "█" not in row
    assert "⠋" not in row
    row.encode("ascii")


def test_unicode_support_follows_the_streams_encoding():
    class Stream:
        encoding = "cp1252"

    assert not supports_unicode(Stream())

    class Utf8:
        encoding = "utf-8"

    assert supports_unicode(Utf8())


# -- the queues -------------------------------------------------------------
#
# "Also log a warning" and "next, write the tests" are different requests. One
# queue serving both intentions means one of them always lands at the wrong
# time.


def test_a_task_waits_and_is_taken_once():
    tasks = TaskQueue()
    assert tasks.push("write the tests")
    assert len(tasks) == 1
    assert tasks.pop() == "write the tests"
    assert tasks.pop() == ""
    assert len(tasks) == 0


def test_tasks_run_in_the_order_they_were_queued():
    tasks = TaskQueue()
    tasks.push("first")
    tasks.push("second")
    assert [tasks.pop(), tasks.pop()] == ["first", "second"]


def test_peeking_does_not_consume():
    tasks = TaskQueue()
    tasks.push("first")
    assert tasks.peek_all() == ["first"]
    assert len(tasks) == 1


def test_clearing_reports_what_it_dropped():
    tasks = TaskQueue()
    tasks.push("a")
    tasks.push("b")
    assert tasks.clear() == 2
    assert not tasks


def test_an_empty_task_is_not_queued():
    tasks = TaskQueue()
    assert not tasks.push("   ")
    assert len(tasks) == 0


def test_the_task_queue_is_bounded():
    """A queue that grows without limit is a way to discover at 3am that you
    have scheduled forty tasks."""
    tasks = TaskQueue(limit=2)
    for n in range(5):
        tasks.push(f"task {n}")
    assert len(tasks) == 2


def test_the_feedback_queue_reports_its_depth():
    """New: the toolbar shows it, so a steer reads as queued rather than
    swallowed."""
    feedback = FeedbackQueue()
    assert len(feedback) == 0
    feedback.push("wrong file")
    assert len(feedback) == 1
    feedback.drain()
    assert len(feedback) == 0


# -- the console itself -----------------------------------------------------


class FakeSession:
    """Stands in for a prompt_toolkit PromptSession."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self.app = None
        self.prompts = 0

    async def prompt_async(self, *_args, **_kwargs) -> str:
        self.prompts += 1
        if not self._lines:
            await asyncio.sleep(3600)
        return self._lines.pop(0)


def _console(lines: list[str], *, active=lambda: False):
    notes: list[str] = []
    commands: list[str] = []
    session = FakeSession(lines)
    live = LiveConsole(
        session_factory=lambda _toolbar: session,
        feedback=FeedbackQueue(),
        tasks=TaskQueue(),
        on_command=commands.append,
        notify=notes.append,
        prompt_is_active=active,
        unicode_ui=True,
    )
    return live, notes, commands, session


def test_a_steer_reaches_the_feedback_queue_and_says_so():
    live, notes, commands, _session = _console([])
    assert live.route("you are editing the wrong file") == ROUTE_FEEDBACK
    assert live.feedback.drain() == ["you are editing the wrong file"]
    assert commands == []
    assert notes and "next step" in notes[0]


def test_a_command_is_dispatched_and_never_queued_for_the_model():
    live, _notes, commands, _session = _console([])
    assert live.route("/context status") == ROUTE_COMMAND
    assert commands == ["/context status"]
    assert live.feedback.drain() == [], "a slash command reached the model"


def test_a_deferred_command_is_explained_not_executed():
    live, notes, commands, _session = _console([])
    assert live.route("/compact clear") == ROUTE_DEFERRED
    assert commands == []
    assert notes and "cannot run while a turn is in flight" in notes[0]


def test_a_failing_command_does_not_take_the_prompt_down():
    def boom(_text: str) -> None:
        raise RuntimeError("nope")

    live, notes, _commands, _session = _console([])
    live._on_command = boom
    assert live.route("/context status") == ROUTE_COMMAND
    assert any("nope" in note for note in notes)


def test_the_toolbar_reads_the_same_events_the_log_does():
    live, _notes, _commands, _session = _console([])
    live.absorb(Event("turn.start", "go"))
    live.absorb(Event("status", "thinking", round=3, max_rounds=25, ctx_pct=71))
    assert live.telemetry.round == 3
    assert "rnd 3/25" in live.telemetry.plain(120)


def test_the_status_object_keeps_the_shape_the_routes_expect():
    """Twenty-odd call sites pass `thinking_status` down and call `.update`;
    they must not have to know whether it paints a spinner or a toolbar."""
    painted: list[int] = []
    meter = _telemetry()
    status = ToolbarStatus(meter, lambda: painted.append(1))
    status.update("[dim]reading config.py[/dim]")
    assert meter.status_text == "reading config.py"
    assert painted == [1]
    status.stop()
    assert meter.status_text == ""


@pytest.mark.asyncio
async def test_the_input_loop_routes_what_was_typed():
    live, _notes, commands, _session = _console(["wrong file", "/context status"])
    task = asyncio.ensure_future(live.input_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await task

    assert live.feedback.drain() == ["wrong file"]
    assert commands == ["/context status"]


@pytest.mark.asyncio
async def test_the_prompt_stands_down_while_an_approval_is_waiting():
    """Two prompt_toolkit applications cannot share a terminal. This is the
    run_in_executor+stdin trap that used to hang turns on Windows."""
    live, _notes, _commands, session = _console(["hello"], active=lambda: True)
    task = asyncio.ensure_future(live.input_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with __import__("contextlib").suppress(asyncio.CancelledError):
        await task

    assert session.prompts == 0, "the input loop competed with the approval prompt"


@pytest.mark.asyncio
async def test_the_approval_prompt_takes_the_terminal_synchronously():
    """The polling backstop leaves a window: the flag goes up, and until the
    next poll two prompt_toolkit applications are live on one terminal. This is
    the synchronous half - `reading_input()` tells whoever is holding the
    console BEFORE it renders the question."""
    from shamsu.safety.approval import on_prompt_open, reading_input

    live, _notes, _commands, _fake = _console([])

    class RunningApp:
        is_running = True

        def __init__(self) -> None:
            self.exited = False

        def exit(self, result=None) -> None:
            self.exited = True

    app = RunningApp()
    live._session = type("S", (), {"app": app})()
    live._loop = asyncio.get_running_loop()

    release = on_prompt_open(live.stand_down)
    try:
        with reading_input():
            # `call_soon_threadsafe` lands on the next loop iteration, which is
            # what rendering the approval panel buys in the real thing.
            await asyncio.sleep(0)
            assert app.exited, "the prompt kept the terminal while a human was asked"
    finally:
        release()

    # And unregistering really unregisters: a stale observer on a dead console
    # would fire on every approval for the rest of the session.
    app.exited = False
    with reading_input():
        await asyncio.sleep(0)
    assert not app.exited


def test_a_broken_observer_never_stops_a_human_being_asked():
    from shamsu.safety.approval import on_prompt_open, reading_input

    def boom() -> None:
        raise RuntimeError("nope")

    release = on_prompt_open(boom)
    try:
        with reading_input():
            pass  # must not raise
    finally:
        release()


def test_stand_down_before_a_prompt_exists_is_harmless():
    """Registered at startup; a turn may never open one."""
    live, _notes, _commands, _fake = _console([])
    live.stand_down()


@pytest.mark.asyncio
async def test_a_terminal_that_cannot_host_a_prompt_costs_the_turn_nothing():
    live = LiveConsole(
        session_factory=lambda _toolbar: None,
        feedback=FeedbackQueue(),
        tasks=TaskQueue(),
        on_command=lambda _text: None,
        notify=lambda _text: None,
        prompt_is_active=lambda: False,
    )
    await asyncio.wait_for(live.input_loop(), timeout=1.0)


@pytest.mark.asyncio
async def test_a_session_factory_that_raises_is_survivable():
    def broken(_toolbar):
        raise RuntimeError("no console screen buffer")

    live = LiveConsole(
        session_factory=broken,
        feedback=FeedbackQueue(),
        tasks=TaskQueue(),
        on_command=lambda _text: None,
        notify=lambda _text: None,
        prompt_is_active=lambda: False,
    )
    await asyncio.wait_for(live.input_loop(), timeout=1.0)


# -- how the REPL wires it in -----------------------------------------------


@pytest.fixture
def repl_live(monkeypatch):
    """Install a live console as the REPL's, and take it away afterwards."""
    from shamsu.cli import repl

    live, notes, commands, _session = _console([])
    monkeypatch.setattr(repl, "_LIVE_CONSOLE", live)
    return repl, live, notes, commands


def test_a_queued_task_is_taken_exactly_once(repl_live):
    """It becomes the next prompt. Popped twice it would run twice."""
    repl, live, _notes, _commands = repl_live
    live.tasks.push("write the tests")

    assert repl._next_queued_task() == "write the tests"
    assert repl._next_queued_task() == ""


def test_no_live_console_means_no_queued_task(monkeypatch):
    from shamsu.cli import repl

    monkeypatch.setattr(repl, "_LIVE_CONSOLE", None)
    assert repl._next_queued_task() == ""


def test_queue_add_lines_up_work_without_interrupting(repl_live):
    from rich.console import Console

    repl, live, _notes, _commands = repl_live
    console = Console(record=True, width=100)

    repl._handle_queue("/queue add write the tests", console)

    assert live.tasks.peek_all() == ["write the tests"]
    assert live.feedback.drain() == [], "a task interrupted the running turn"
    assert "queued" in console.export_text()


def test_queue_add_with_nothing_to_run_says_so(repl_live):
    from rich.console import Console

    repl, live, _notes, _commands = repl_live
    console = Console(record=True, width=100)
    repl._handle_queue("/queue add", console)
    assert len(live.tasks) == 0
    assert "/queue add" in console.export_text()


def test_queue_lists_what_is_waiting(repl_live):
    from rich.console import Console

    repl, live, _notes, _commands = repl_live
    live.tasks.push("write the tests")
    live.tasks.push("then run them")
    console = Console(record=True, width=100)

    repl._handle_queue("/queue", console)
    printed = console.export_text()
    assert "2 task(s) waiting" in printed
    assert "write the tests" in printed
    assert "then run them" in printed


def test_queue_clear_empties_it(repl_live):
    from rich.console import Console

    repl, live, _notes, _commands = repl_live
    live.tasks.push("a")
    console = Console(record=True, width=100)
    repl._handle_queue("/queue clear", console)
    assert len(live.tasks) == 0
    assert "Dropped 1" in console.export_text()


def test_an_empty_queue_says_how_to_fill_it(repl_live):
    from rich.console import Console

    repl, _live, _notes, _commands = repl_live
    console = Console(record=True, width=100)
    repl._handle_queue("/queue", console)
    assert "/queue add" in console.export_text()


def test_the_midturn_dispatcher_answers_context_locally(tmp_path):
    """Zero tokens, no round, and the runtime's own numbers rather than the
    model's account of them."""
    from rich.console import Console

    from shamsu.cli import repl

    console = Console(record=True, width=120)
    repl._midturn_command("/context status", tmp_path, console)
    assert console.export_text().strip()


def test_every_whitelisted_command_has_a_handler_behind_it(tmp_path):
    """A name on the whitelist with nothing behind it is a promise the prompt
    cannot keep."""
    from rich.console import Console

    from shamsu.cli import repl
    from shamsu.cli.live_console import MIDTURN_COMMANDS

    for command in sorted(MIDTURN_COMMANDS):
        console = Console(record=True, width=120)
        repl._midturn_command(command, tmp_path, console)
        assert console.export_text().strip(), f"{command} printed nothing"


def test_the_turn_still_runs_without_a_live_console(monkeypatch, tmp_path):
    """A pipe, a CI runner, a console with no screen buffer. The fallback is
    not a degraded mode - it is what every turn did before this."""
    from shamsu.cli import repl

    monkeypatch.setattr(repl, "_LIVE_CONSOLE", None)
    seen: list[str] = []

    async def fake_handle(dispatch_input, *_args, **kwargs):
        seen.append(dispatch_input)
        kwargs["thinking_status"].update("working")

    monkeypatch.setattr(repl, "_handle_request", fake_handle)
    monkeypatch.setattr(repl, "_run_request", lambda coro: asyncio.run(coro) or True)

    from rich.console import Console

    completed = repl._dispatch_turn(
        "fix the thing",
        tmp_path,
        Console(record=True, width=100),
        None,
        None,
        previous_user_prompt="",
        session_logger=None,
        user_input="fix the thing",
    )
    assert completed is True
    assert seen == ["fix the thing"]


def test_the_live_console_is_skipped_when_stdin_is_not_a_terminal(monkeypatch, tmp_path):
    from rich.console import Console

    from shamsu.cli import repl

    class NotATty:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(repl.sys, "stdin", NotATty())
    assert repl._build_live_console(tmp_path, Console()) is None


def test_the_two_prompts_share_one_history(tmp_path):
    """They are two PromptSessions that never run at once. With a history each,
    something typed while the agent was working could not be recalled with Up
    at the next prompt."""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from shamsu.cli import repl

    history = InMemoryHistory()
    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        idle = repl._make_prompt_session(tmp_path, None, history)
        # Built the way `_build_live_console` builds it, but without its tty
        # gate: the point under test is the wiring, and this suite has no
        # terminal of its own.
        live = LiveConsole(
            session_factory=lambda toolbar: repl._make_prompt_session(
                tmp_path, toolbar, history
            ),
            feedback=FeedbackQueue(),
            tasks=TaskQueue(),
            on_command=lambda _text: None,
            notify=lambda _text: None,
            prompt_is_active=lambda: False,
        )
        midturn = live._ensure_session()

    assert idle is not None and midturn is not None
    assert idle.history is history
    assert midturn.history is history, "the mid-turn prompt has its own history"

    history.append_string("fix the thing")
    assert "fix the thing" in midturn.history.get_strings()


def test_startup_says_the_live_console_is_on_and_what_it_gives_you(repl_live):
    """Everything the upgrade changed happens DURING a turn; at an idle prompt
    the screen is deliberately identical to before. Without this line, "is the
    new interface running?" has no answer short of starting a turn."""
    from rich.console import Console

    repl, _live, _notes, _commands = repl_live
    console = Console(record=True, width=120)
    repl._announce_live_console(console)

    printed = console.export_text()
    assert "Live console on" in printed
    assert "/queue add" in printed


def test_startup_says_WHY_the_live_console_is_off(monkeypatch, tmp_path):
    """"The new interface is not showing up" and "this terminal cannot host it"
    look identical from the outside."""
    from rich.console import Console

    from shamsu.cli import repl

    monkeypatch.setenv("SHAMSU_LIVE_FEEDBACK", "0")
    monkeypatch.setattr(repl, "_LIVE_CONSOLE", repl._build_live_console(tmp_path, Console()))

    console = Console(record=True, width=120)
    repl._announce_live_console(console)
    printed = console.export_text()

    assert "Live console off" in printed
    assert "SHAMSU_LIVE_FEEDBACK=0" in printed


def test_the_off_reason_names_a_pipe(monkeypatch, tmp_path):
    """This used to assert the reason was "stdin is not a terminal", asserted by
    patching `sys.stdin.isatty()` to False - which is how a test suite came to
    guarantee the bug. A redirected stdin is NOT a pipe and NOT a reason: see
    `test_a_redirected_stdin_does_not_turn_the_live_console_off`. A real pipe
    still has to be named, and prompt_toolkit is what knows."""
    from rich.console import Console

    from shamsu.cli import repl

    monkeypatch.delenv("SHAMSU_LIVE_FEEDBACK", raising=False)
    _pretend_prompt_toolkit_attaches(monkeypatch, kind="Win32PipeInput")
    monkeypatch.setattr(
        repl, "_LIVE_CONSOLE", repl._build_live_console(tmp_path, Console(force_terminal=True))
    )

    console = Console(record=True, width=120)
    repl._announce_live_console(console)
    assert "stdin is a pipe" in console.export_text()


def test_the_live_console_can_be_switched_off(monkeypatch, tmp_path):
    from rich.console import Console

    from shamsu.cli import repl

    monkeypatch.setenv("SHAMSU_LIVE_FEEDBACK", "0")
    assert repl._build_live_console(tmp_path, Console()) is None


@pytest.mark.asyncio
async def test_the_real_prompt_session_renders_the_real_toolbar(tmp_path):
    """Everything above uses a fake session. This one drives the actual
    `PromptSession` the REPL builds, with the actual toolbar callable, over
    prompt_toolkit's pipe input - so a toolbar that raises, or a session the
    factory builds wrong, fails here rather than on the user's terminal.
    """
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from shamsu.cli.repl import _make_prompt_session

    painted: list[str] = []

    with create_pipe_input() as pipe, create_app_session(
    input=pipe, output=DummyOutput()
    ):
        live, _notes, commands, _fake = _console([])
        live.absorb(Event("turn.start", "go"))
        live.absorb(
            Event("status", "reading calc.py", round=3, max_rounds=24, ctx_pct=71)
        )

        def toolbar():
            value = live._toolbar()
            painted.append("".join(text for _style, text in value))
            return value

        session = _make_prompt_session(tmp_path, toolbar)
        assert session is not None
        live._session = session

        pipe.send_text("/context status\n")
        typed = await session.prompt_async(
            [("class:tb.caret", "» ")], refresh_interval=0.05
        )

    assert typed == "/context status"
    assert painted, "the toolbar callable was never invoked by the prompt"
    assert "/context status" in session.history.get_strings(), (
        "the mid-turn prompt did not record what was typed in the shared history"
    )
    assert "rnd 3/24" in painted[-1]
    assert "71%" in painted[-1]

    live.route(typed)
    assert commands == ["/context status"]
    assert live.feedback.drain() == []


@pytest.mark.asyncio
async def test_the_input_line_is_taken_down_when_the_turn_ends():
    """A prompt left running past its turn would eat the next idle prompt's
    keystrokes."""
    from shamsu.cli.repl import _with_live_input

    live, _notes, _commands, _session = _console([])

    async def turn() -> str:
        await asyncio.sleep(0.02)
        return "done"

    assert await _with_live_input(turn(), live) == "done"
    await asyncio.sleep(0.02)


# -- what "this terminal can host a prompt" actually means -------------------
#
# Live 2026-08-22: the whole live console was off on a normal interactive run,
# announcing "stdin is not a terminal" - in a process where rich was painting
# full box-drawing panels at true console width and prompt_toolkit was reading
# keys and wrapping the prompt correctly. Both libraries were fine; the gate
# was asking a question neither of them asks.
#
# On Windows prompt_toolkit does not read `sys.stdin` at all, it reads the
# console input buffer through the Win32 API. So a launcher that hands python a
# redirected stdin (SHAMSU's own `shamsu.ps1` did, by evaluating `@($input)`)
# leaves `isatty()` False with the terminal fully usable.


def _pretend_prompt_toolkit_attaches(monkeypatch, kind: str = "Win32Input"):
    import prompt_toolkit.input.defaults as defaults

    attached = type(kind, (), {"closed": False})()
    monkeypatch.setattr(defaults, "create_input", lambda *a, **k: attached)
    return attached


def test_a_redirected_stdin_does_not_turn_the_live_console_off(monkeypatch, tmp_path):
    """The reported failure. stdin is not a tty; everything else works."""
    from rich.console import Console

    from shamsu.cli import repl

    class NotATty:
        def isatty(self) -> bool:
            return False

    monkeypatch.delenv("SHAMSU_LIVE_FEEDBACK", raising=False)
    monkeypatch.setattr(repl.sys, "stdin", NotATty())
    _pretend_prompt_toolkit_attaches(monkeypatch)

    assert repl._build_live_console(tmp_path, Console(force_terminal=True)) is not None


def test_output_that_is_not_a_terminal_turns_it_off_and_says_so(monkeypatch, tmp_path):
    """Nothing to paint on. This is the piped/CI case the gate exists for, and
    it is about OUTPUT - the turn still runs, it just renders as a plain log."""
    from rich.console import Console

    from shamsu.cli import repl

    monkeypatch.delenv("SHAMSU_LIVE_FEEDBACK", raising=False)
    _pretend_prompt_toolkit_attaches(monkeypatch)

    assert repl._build_live_console(tmp_path, Console(force_terminal=False)) is None
    assert repl._LIVE_CONSOLE_OFF_REASON == "output is not a terminal"


def test_a_pipe_input_is_prompt_toolkits_own_answer_for_no_console(monkeypatch, tmp_path):
    from rich.console import Console

    from shamsu.cli import repl

    monkeypatch.delenv("SHAMSU_LIVE_FEEDBACK", raising=False)
    _pretend_prompt_toolkit_attaches(monkeypatch, kind="Win32PipeInput")

    assert repl._build_live_console(tmp_path, Console(force_terminal=True)) is None
    assert "pipe" in repl._LIVE_CONSOLE_OFF_REASON


def test_the_reason_names_prompt_toolkit_when_it_cannot_attach(monkeypatch, tmp_path):
    """A terminal with no console behind it: say which library refused, so the
    next person does not have to guess between rich and prompt_toolkit."""
    from rich.console import Console

    import prompt_toolkit.input.defaults as defaults

    from shamsu.cli import repl

    def no_console(*args, **kwargs):
        raise OSError("no console")

    monkeypatch.delenv("SHAMSU_LIVE_FEEDBACK", raising=False)
    monkeypatch.setattr(defaults, "create_input", no_console)

    assert repl._build_live_console(tmp_path, Console(force_terminal=True)) is None
    assert "prompt_toolkit" in repl._LIVE_CONSOLE_OFF_REASON
    assert "OSError" in repl._LIVE_CONSOLE_OFF_REASON


def test_it_can_be_forced_on_for_a_terminal_the_check_misjudges(monkeypatch, tmp_path):
    """The escape hatch had only an off switch. Detection was wrong once
    already, and being wrong the other way needs an answer that is not
    "wait for a release"."""
    from rich.console import Console

    from shamsu.cli import repl

    monkeypatch.setenv("SHAMSU_LIVE_FEEDBACK", "1")
    assert repl._build_live_console(tmp_path, Console(force_terminal=False)) is not None


def test_off_still_wins_over_forced_on(monkeypatch, tmp_path):
    from rich.console import Console

    from shamsu.cli import repl

    monkeypatch.setenv("SHAMSU_LIVE_FEEDBACK", "0")
    assert repl._build_live_console(tmp_path, Console(force_terminal=True)) is None
    assert repl._LIVE_CONSOLE_OFF_REASON == "SHAMSU_LIVE_FEEDBACK=0"
