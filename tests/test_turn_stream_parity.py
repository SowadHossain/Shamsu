"""One turn stream, three renderers - and the parity that proves it.

The complaint this answers: the CLI shows every line of a turn and Telegram
showed almost none of them. The strings were never the problem; delivery was.
So the headline test here does not check formatting - it runs ONE loop, feeds
both renderers from the same stream, and asserts the ordered CLI lines equal
the ordered Telegram card lines. Anything that drops an event on the Telegram
side to protect the API fails it.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
from pathlib import Path

import pytest
from rich.console import Console

from shamsu.agents.simple_state import ChatState
from shamsu.agents.simple_chat import SimpleChatLoop
from shamsu.agents.simple_prompt import simple_system_prompt
from shamsu.cli.turn_render import CliTurnRenderer
from shamsu.integrations.telegram.models import OutboundMessage
from shamsu.integrations.telegram.turn_card import TelegramTurnCard
from shamsu.runtime.turn_stream import TurnEvent, TurnStream, activity_path, body_kinds
from shamsu.tools.agent_tools import AgentToolRegistry

CHAT_ID = 4242


class FakeClient:
    """Replays scripted model turns. Same shape as tests/test_simple_chat.py."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.turns:
            return {"message": {"content": "done", "tool_calls": []}}
        return self.turns.pop(0)


def _text(content: str) -> dict:
    return {"message": {"content": content, "tool_calls": []}}


def _tool(name: str, **arguments) -> dict:
    return {
        "message": {
            "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
        }
    }


class FakeSender:
    """Stands in for the Telegram transport: records, hands back message ids."""

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self._next_id = 100

    def __call__(self, message: OutboundMessage) -> int:
        self.sent.append(message)
        if message.edit_message_id is not None:
            return int(message.edit_message_id)
        self._next_id += 1
        return self._next_id

    @property
    def message_ids(self) -> list[int]:
        seen: list[int] = []
        current = 100
        for message in self.sent:
            if message.edit_message_id is None:
                current += 1
                seen.append(current)
        return seen


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    # Wide and colourless so the assertion is about the LINES, not about how
    # rich decided to wrap or paint them.
    return Console(file=buffer, width=200, no_color=True, highlight=False), buffer


def _loop(tmp_path: Path, turns, **kwargs) -> SimpleChatLoop:
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    return SimpleChatLoop(
        tmp_path,
        client=FakeClient(turns),
        tools=tools,
        state=state,
        model_name="qwen3:8b",
        **kwargs,
    )


def _event(seq: int, kind: str, text: str = "", **data) -> TurnEvent:
    return TurnEvent(
        seq=seq,
        kind=kind,
        text=text,
        data=data,
        turn_id="turn-test",
        session_id="s-1",
        workspace="/ws",
        source="telegram",
    )


# --- the headline: one loop, two renderers, identical lines ---------------


def test_cli_lines_and_telegram_card_lines_are_the_same_list(tmp_path):
    """G1, executable. Same turn, same order, nothing dropped on the phone."""
    (tmp_path / "game.js").write_text("const x = 1;\n", encoding="utf-8")
    console, buffer = _console()
    sender = FakeSender()
    clock = FakeClock()

    stream = TurnStream(tmp_path, "s-parity")
    cli = CliTurnRenderer(console)
    card = TelegramTurnCard(
        chat_id=CHAT_ID,
        send=sender,
        prompt="add a pause menu",
        clock=clock,
    )
    stream.add_renderer(cli)
    stream.add_renderer(card)

    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="game.js"), _text("Added the pause menu.")],
        emit=stream.publish,
        source="telegram",
    )
    asyncio.run(loop.run("add a pause menu"))

    assert cli.lines, "the CLI renderer printed nothing at all"
    assert cli.lines == card.all_lines
    assert "read_file game.js" in cli.lines
    # And the CLI's real console output still carries every one of them. Not
    # verbatim: a row is painted with an icon, a verb and a duration
    # (`▤ Reading game.js · 5ms`), so what must survive is the SUBJECT of the
    # line, not the raw string the bus carried.
    printed = buffer.getvalue()
    for line in cli.lines:
        subject = line.split()[-1] if line.split() else line
        assert subject in printed, f"{line!r} left no trace in the output"


def test_a_repeated_line_survives_on_telegram(tmp_path):
    """The exact shape the old throttle ate: same text, twice, <8s apart."""
    console, _ = _console()
    sender = FakeSender()
    stream = TurnStream(tmp_path, "s-repeat")
    cli = CliTurnRenderer(console)
    card = TelegramTurnCard(chat_id=CHAT_ID, send=sender, prompt="p", clock=FakeClock())
    stream.add_renderer(cli)
    stream.add_renderer(card)

    loop = _loop(
        tmp_path,
        [_tool("list_files"), _tool("list_files"), _text("done")],
        emit=stream.publish,
    )
    asyncio.run(loop.run("look around"))

    responded = [line for line in card.all_lines if line.startswith("model responded")]
    assert len(responded) >= 2
    assert cli.lines == card.all_lines


# --- the event and the bus -----------------------------------------------


def test_activity_jsonl_records_the_turn_and_replays_in_order(tmp_path):
    stream = TurnStream(tmp_path, "s-file")
    for index, kind in enumerate(("turn.start", "activity", "status", "turn.end"), 1):
        stream.publish(_event(index, kind, f"line {index}"))

    path = activity_path(tmp_path, "s-file")
    assert path.exists()
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["kind"] for record in records] == [
        "turn.start",
        "activity",
        "status",
        "turn.end",
    ]
    assert [record["seq"] for record in records] == [1, 2, 3, 4]

    replayed = stream.replay()
    assert [event.text for event in replayed] == ["line 1", "line 2", "line 3", "line 4"]
    assert all(isinstance(event, TurnEvent) for event in replayed)


def test_a_subscriber_replays_from_disk_then_tails_live(tmp_path):
    """A phone that was locked, or a tab opened mid-turn, catches up."""
    stream = TurnStream(tmp_path, "s-tail")
    stream.publish(_event(1, "activity", "first"))
    stream.publish(_event(2, "activity", "second"))

    subscription = stream.subscribe(since_seq=1)
    stream.publish(_event(3, "activity", "third"))

    assert [event.text for event in subscription.drain()] == ["second", "third"]
    subscription.close()


def test_overflow_drops_status_first_and_never_a_tool_call(tmp_path):
    stream = TurnStream(tmp_path, "s-bound", max_queue=4)
    subscription = stream.subscribe()
    for index in range(1, 4):
        stream.publish(_event(index, "status", f"tick {index}"))
    stream.publish(_event(4, "tool.call", "read_file a.py"))
    stream.publish(_event(5, "tool.result", "read_file ok"))
    stream.publish(_event(6, "turn.end", "done"))

    kinds = [event.kind for event in subscription.drain()]
    assert "tool.call" in kinds
    assert "tool.result" in kinds
    assert "turn.end" in kinds
    assert subscription.dropped > 0
    assert kinds.count("status") < 3
    subscription.close()


def test_a_broken_renderer_never_breaks_the_turn(tmp_path):
    stream = TurnStream(tmp_path, "s-broken")
    seen: list[str] = []

    def explode(_event: TurnEvent) -> None:
        raise RuntimeError("renderer is on fire")

    stream.add_renderer(explode)
    stream.add_renderer(lambda event: seen.append(event.text))
    stream.publish(_event(1, "activity", "still fine"))

    assert seen == ["still fine"]


def test_the_loop_brackets_a_turn_with_start_and_end(tmp_path):
    events: list[TurnEvent] = []
    loop = _loop(tmp_path, [_text("hello")], emit=events.append, source="web")
    asyncio.run(loop.run("hi"))

    kinds = [event.kind for event in events]
    assert kinds[0] == "turn.start"
    assert kinds[-1] == "turn.end"
    assert "assistant" in kinds
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    assert {event.turn_id for event in events} == {events[0].turn_id}
    assert events[0].turn_id
    assert {event.source for event in events} == {"web"}


def test_a_tool_call_emits_its_own_kind_and_its_result(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    events: list[TurnEvent] = []
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="a.py"), _text("read it")],
        emit=events.append,
    )
    asyncio.run(loop.run("read a.py"))

    calls = [event for event in events if event.kind == "tool.call"]
    results = [event for event in events if event.kind == "tool.result"]
    assert [event.data.get("tool") for event in calls] == ["read_file"]
    assert calls[0].text == "read_file a.py"
    assert results and results[0].data.get("ok") is True
    # The line is emitted ONCE, as tool.call - not also as a plain activity.
    assert calls[0].text not in [
        event.text for event in events if event.kind == "activity"
    ]


def test_the_legacy_callbacks_still_fire_alongside_emit(tmp_path):
    """Nothing that builds the loop today breaks: the shims stay."""
    activity: list[str] = []
    status: list[str] = []
    events: list[TurnEvent] = []
    loop = _loop(
        tmp_path,
        [_text("ok")],
        emit=events.append,
        on_activity=activity.append,
        on_status=status.append,
    )
    asyncio.run(loop.run("hi"))

    assert any("model responded" in line for line in activity)
    assert [event.text for event in events if event.kind == "activity"] == activity


# --- the Telegram card ----------------------------------------------------


def test_the_card_header_echoes_the_prompt_like_a_terminal():
    sender = FakeSender()
    card = TelegramTurnCard(
        chat_id=CHAT_ID,
        send=sender,
        prompt="add a pause menu",
        title="asteroids",
        clock=FakeClock(),
    )
    card(_event(1, "turn.start", "add a pause menu"))

    assert sender.sent
    # The THREAD's name, not the surface's. Telling a Telegram reader they are
    # on Telegram spends the header on the one fact they already have; the
    # thread they are driving is what they cannot see from this chat, and what
    # they switch between without leaving it.
    assert "shamsu (asteroids) telegram&gt; add a pause menu" in sender.sent[0].text


def test_the_card_is_html_and_escapes_the_body():
    sender = FakeSender()
    clock = FakeClock()
    card = TelegramTurnCard(chat_id=CHAT_ID, send=sender, prompt="a & b", clock=clock)
    card(_event(1, "turn.start", "a & b"))
    card(_event(2, "activity", "read_file <script>.js"))
    clock.advance(10)
    card(_event(3, "turn.end", "done"))

    assert all(message.parse_mode == "HTML" for message in sender.sent)
    last = sender.sent[-1].text
    assert "<pre>" in last and "</pre>" in last
    assert "&lt;script&gt;.js" in last
    assert "<script>" not in last
    assert "a &amp; b" in last


def test_one_card_is_edited_rather_than_a_message_per_line():
    sender = FakeSender()
    clock = FakeClock()
    card = TelegramTurnCard(chat_id=CHAT_ID, send=sender, prompt="p", clock=clock)
    card(_event(1, "turn.start", "p"))
    for index in range(2, 22):
        clock.advance(2.0)
        card(_event(index, "activity", f"step {index}"))

    creates = [m for m in sender.sent if m.edit_message_id is None]
    edits = [m for m in sender.sent if m.edit_message_id is not None]
    assert len(creates) == 1
    assert edits
    assert all(m.edit_message_id == 101 for m in edits)


def test_edits_are_rate_limited_and_no_line_is_lost():
    """The inversion: bound the API rate, never the information."""
    sender = FakeSender()
    clock = FakeClock()
    card = TelegramTurnCard(
        chat_id=CHAT_ID, send=sender, prompt="p", clock=clock, flush_interval=1.5
    )
    card(_event(1, "turn.start", "p"))
    # 40 tool lines in 20 seconds - the shape that used to lose 38 of them.
    for index in range(2, 42):
        clock.advance(0.5)
        card(_event(index, "activity", f"tool {index}"))
    clock.advance(1.5)
    card(_event(42, "turn.end", "done in 20s"))

    duration = 40 * 0.5 + 1.5
    # +2: the card creation and the forced final flush.
    assert len(sender.sent) <= math.ceil(duration / 1.5) + 2
    assert len(sender.sent) < 40
    assert card.all_lines == [f"tool {index}" for index in range(2, 42)]
    body = sender.sent[-1].text
    for index in range(2, 42):
        assert f"tool {index}" in body


def test_a_status_replaces_the_footer_instead_of_appending():
    sender = FakeSender()
    clock = FakeClock()
    card = TelegramTurnCard(chat_id=CHAT_ID, send=sender, prompt="p", clock=clock)
    card(_event(1, "turn.start", "p"))
    for index, seconds in enumerate((5, 10, 15), start=2):
        clock.advance(2.0)
        card(_event(index, "status", f"thinking {seconds}s"))

    assert card.all_lines == []
    last = sender.sent[-1].text
    assert "thinking 15s" in last
    assert "thinking 5s" not in last


def test_turn_end_seals_the_card_with_a_verdict():
    sender = FakeSender()
    clock = FakeClock()
    card = TelegramTurnCard(chat_id=CHAT_ID, send=sender, prompt="p", clock=clock)
    card(_event(1, "turn.start", "p"))
    card(_event(2, "status", "running write_file... 3s"))
    card(_event(3, "turn.end", "done in 6m12s - 2 files changed"))

    last = sender.sent[-1].text
    assert "done in 6m12s - 2 files changed" in last
    assert "running write_file" not in last
    assert card.sealed


def test_turn_end_flushes_immediately_even_inside_the_interval():
    sender = FakeSender()
    clock = FakeClock()
    card = TelegramTurnCard(chat_id=CHAT_ID, send=sender, prompt="p", clock=clock)
    card(_event(1, "turn.start", "p"))
    before = len(sender.sent)
    clock.advance(0.1)
    card(_event(2, "turn.end", "done in 0s"))

    assert len(sender.sent) > before


def test_a_long_turn_overflows_into_a_continuation_card():
    sender = FakeSender()
    clock = FakeClock()
    card = TelegramTurnCard(
        chat_id=CHAT_ID, send=sender, prompt="p", clock=clock, max_chars=600
    )
    card(_event(1, "turn.start", "p"))
    for index in range(2, 40):
        clock.advance(2.0)
        card(_event(index, "activity", f"line {index} " + "x" * 40))
    clock.advance(2.0)
    card(_event(40, "turn.end", "done"))

    creates = [m for m in sender.sent if m.edit_message_id is None]
    assert len(creates) >= 2, "the card never overflowed into a continuation"
    assert any("continued" in m.text for m in creates[1:])
    assert all(len(m.text) <= 600 for m in sender.sent)
    # Nothing is lost across the seam.
    assert card.all_lines == [f"line {index} " + "x" * 40 for index in range(2, 40)]
    everything = "\n".join(m.text for m in sender.sent)
    for index in range(2, 40):
        assert f"line {index} " in everything


def test_a_single_line_longer_than_the_card_is_truncated_not_dropped():
    sender = FakeSender()
    clock = FakeClock()
    card = TelegramTurnCard(
        chat_id=CHAT_ID, send=sender, prompt="p", clock=clock, max_chars=300
    )
    card(_event(1, "turn.start", "p"))
    card(_event(2, "activity", "y" * 5000))
    clock.advance(5)
    card(_event(3, "turn.end", "done"))

    assert len(card.all_lines) == 1
    assert all(len(m.text) <= 300 for m in sender.sent)
    assert "yyy" in sender.sent[-1].text


def test_a_typing_action_is_sent_while_the_model_thinks():
    sender = FakeSender()
    clock = FakeClock()
    typing: list[int] = []
    card = TelegramTurnCard(
        chat_id=CHAT_ID,
        send=sender,
        prompt="p",
        clock=clock,
        typing=lambda: typing.append(1),
        typing_interval=4.0,
    )
    card(_event(1, "turn.start", "p"))
    for index in range(2, 8):
        clock.advance(5.0)
        card(_event(index, "status", f"thinking {index * 5}s"))

    assert len(typing) >= 5
    clock.advance(5.0)
    card(_event(20, "turn.end", "done"))
    settled = len(typing)
    card(_event(21, "status", "ignored"))
    assert len(typing) == settled


def test_a_failed_edit_keeps_the_lines_for_the_next_flush():
    """Never drop an event to protect the API - not even when it says no."""
    clock = FakeClock()
    failures = {"count": 0}

    def flaky(message: OutboundMessage) -> int:
        if message.edit_message_id is not None and failures["count"] < 1:
            failures["count"] += 1
            raise RuntimeError("Telegram API failed: Too Many Requests: retry after 3")
        return int(message.edit_message_id or 101)

    card = TelegramTurnCard(chat_id=CHAT_ID, send=flaky, prompt="p", clock=clock)
    card(_event(1, "turn.start", "p"))
    clock.advance(2.0)
    card(_event(2, "activity", "first"))
    clock.advance(5.0)
    card(_event(3, "activity", "second"))
    clock.advance(5.0)
    card(_event(4, "turn.end", "done"))

    assert card.all_lines == ["first", "second"]
    assert card.send_failures == 1


def test_the_card_carries_the_same_kinds_the_cli_prints():
    assert body_kinds("normal") == frozenset({"activity", "tool.call"})
    assert "tool.result" in body_kinds("verbose")
    assert "activity" not in body_kinds("quiet")


# --- the CLI renderer, unchanged in what it shows -------------------------


def test_the_cli_renderer_prints_dim_activity_and_updates_the_status():
    console, buffer = _console()
    updates: list[str] = []
    renderer = CliTurnRenderer(console, status_updater=updates.append)

    renderer(_event(1, "turn.start", "hello"))
    renderer(_event(2, "activity", "model responded in 3s"))
    renderer(_event(3, "status", "running read_file... 2s"))
    renderer(_event(4, "tool.call", "read_file a.py", tool="read_file"))
    renderer(_event(5, "tool.result", "read_file ok", tool="read_file", ok=True))
    renderer(_event(6, "assistant", "the answer"))
    renderer(_event(7, "turn.end", "done in 3s"))

    printed = buffer.getvalue()
    assert "model responded in 3s" in printed
    # The row is enriched, so the tool's SUBJECT is what has to survive - and
    # it has to survive even though this call never set `target`, which is the
    # case that made a richer row carry less than the dim one it replaced.
    assert "a.py" in printed
    assert "Reading" in printed
    # The status line belongs to the spinner, and the CLI still never prints a
    # tool result or the assistant's answer as its own dim line.
    assert "running read_file" not in printed
    assert "read_file ok" not in printed
    assert "the answer" not in printed
    # The verdict IS printed now, as a badge at the foot of the turn. It was
    # previously dropped entirely, so a turn ended by scrolling off with no
    # statement of whether it had worked.
    assert "SUCCESS" in printed
    assert "done in 3s" in printed
    # The spinner now carries the meter alongside the text when a tick brings
    # one; this tick brought none, so it is the text alone.
    assert updates == ["running read_file... 2s"]
    assert renderer.lines == ["model responded in 3s", "read_file a.py"]


def test_the_cli_renderer_survives_having_no_status_line():
    console, buffer = _console()
    renderer = CliTurnRenderer(console, status_updater=None)
    renderer(_event(1, "status", "thinking 4s"))
    renderer(_event(2, "activity", "kept"))
    assert "kept" in buffer.getvalue()


def test_the_mirror_renderer_echoes_a_remote_prompt_on_the_desktop():
    """G2: a Telegram prompt reads like a terminal line on the desktop too."""
    console, buffer = _console()
    renderer = CliTurnRenderer(console, echo_surface="telegram", echo_title="asteroids")
    renderer(_event(1, "turn.start", "add a pause menu"))
    renderer(_event(2, "activity", "model responded in 9s"))

    printed = buffer.getvalue()
    assert "shamsu (asteroids) telegram> add a pause menu" in printed
    assert "model responded in 9s" in printed


@pytest.mark.parametrize("kind", sorted(body_kinds("normal")))
def test_every_body_kind_reaches_both_renderers(kind, tmp_path):
    console, _ = _console()
    sender = FakeSender()
    stream = TurnStream(tmp_path, "s-kinds")
    cli = CliTurnRenderer(console)
    card = TelegramTurnCard(chat_id=CHAT_ID, send=sender, prompt="p", clock=FakeClock())
    stream.add_renderer(cli)
    stream.add_renderer(card)

    stream.publish(_event(1, "turn.start", "p"))
    stream.publish(_event(2, kind, "a line"))

    assert cli.lines == ["a line"] == card.all_lines


def test_a_second_turn_restarts_seq_without_swallowing_its_first_events(tmp_path):
    """`seq` is per TURN, so a subscriber must not read turn 2's seq 1 as old."""
    stream = TurnStream(tmp_path, "s-turns")
    first = TurnEvent(seq=1, kind="activity", text="turn one line", turn_id="turn-a")
    second = TurnEvent(seq=2, kind="activity", text="turn one again", turn_id="turn-a")
    stream.publish(first)
    stream.publish(second)

    subscription = stream.subscribe(since_seq=2)
    stream.publish(TurnEvent(seq=1, kind="turn.start", text="ask", turn_id="turn-b"))
    stream.publish(TurnEvent(seq=2, kind="activity", text="turn two line", turn_id="turn-b"))

    texts = [event.text for event in subscription.drain()]
    assert texts == ["ask", "turn two line"]
    subscription.close()


def test_subscribing_mid_publish_delivers_each_event_once(tmp_path):
    """Replay and live tail are one atomic handover, not two overlapping ones."""
    stream = TurnStream(tmp_path, "s-once")
    for index in range(1, 6):
        stream.publish(TurnEvent(seq=index, kind="activity", text=f"l{index}", turn_id="t"))

    subscription = stream.subscribe()
    stream.publish(TurnEvent(seq=6, kind="activity", text="l6", turn_id="t"))

    texts = [event.text for event in subscription.drain()]
    assert texts == ["l1", "l2", "l3", "l4", "l5", "l6"]
    assert len(texts) == len(set(texts))
    subscription.close()


# --- three-way parity: terminal, phone, and the file on disk --------------


def test_the_terminal_the_phone_and_the_session_log_agree(tmp_path):
    """The headline, extended to the third surface.

    The terminal renders from the turn stream and `log-summary.md` is written
    from the ActionLedger - two buses, populated from the same points in the
    loop. Nothing structural stops them drifting, and a terminal that disagrees
    with the log you read afterwards is worse than either alone, because you
    cannot tell which one lied. So the agreement is asserted, not assumed.

    Compared on the ACTIONS, not the strings: each surface words a row for its
    own medium - "read_file game.js" on the bus, a coloured icon row in the
    terminal, a linked bullet in the file. What must match is which tools ran,
    and in which order.
    """
    from shamsu.action_ledger.ledger import start_run
    from shamsu.session.manager import SessionManager

    (tmp_path / "game.js").write_text("const x = 1;\n", encoding="utf-8")
    console, _buffer = _console()
    sender = FakeSender()
    clock = FakeClock()

    logger = SessionManager(tmp_path).create_session()
    ledger = start_run(tmp_path, "add a pause menu", session_logger=logger)
    stream = TurnStream(tmp_path, logger.session_id)
    cli = CliTurnRenderer(console)
    card = TelegramTurnCard(
        chat_id=CHAT_ID, send=sender, prompt="add a pause menu", clock=clock
    )
    stream.add_renderer(cli)
    stream.add_renderer(card)

    loop = _loop(
        tmp_path,
        [
            _tool("read_file", filepath="game.js"),
            _tool("write_file", filepath="menu.js", content="export const menu = 1;\n"),
            _text("Added the pause menu."),
        ],
        emit=stream.publish,
        source="cli",
        action_ledger=ledger,
    )
    asyncio.run(loop.run("add a pause menu"))
    ledger.finish("Added the pause menu.", status="success")

    # Surfaces 1 and 2: the bus, unchanged.
    assert cli.lines == card.all_lines

    # Surface 3: the file. Every tool the bus reported is a row in the log.
    summary = ledger.summary_log_path.read_text(encoding="utf-8")
    for tool in ("read_file", "write_file"):
        assert any(tool in line for line in cli.lines), f"{tool} missing from the bus"
    assert "game.js" in summary and "menu.js" in summary
    # ...and in the same order the bus reported them.
    assert summary.index("game.js") < summary.index("menu.js")


# --- the rendering itself -------------------------------------------------


def test_a_run_of_identical_calls_collapses_to_one_row():
    """Live 2026-08-21 a 3B called `contract_status` eight times in a row.
    Eight identical lines is a wall you have to count; one row saying x8 is a
    loop you can see."""
    console, buffer = _console()
    updates: list[str] = []
    renderer = CliTurnRenderer(console, status_updater=updates.append)

    renderer(_event(1, "turn.start", "go"))
    for seq in range(2, 10):
        renderer(_event(seq, "tool.call", "contract_status", tool="contract_status"))
        renderer(
            _event(
                seq + 100, "tool.result", "contract_status ok",
                tool="contract_status", ok=True,
            )
        )
    renderer(_event(50, "turn.end", "done in 3s", status="done"))

    printed = buffer.getvalue()
    # Two lines for eight calls: the first, painted in full and immediately so
    # you are not left watching nothing, and the count that closes the run.
    assert printed.count("contract_status") == 2, printed
    assert "x8" in printed
    # ...and the count was visible on the spinner while it was still climbing.
    assert any("x4" in update for update in updates)
    # The parity list still holds all eight: collapsing is presentation only.
    assert len([line for line in renderer.lines if "contract_status" in line]) == 8


def test_two_different_tools_do_not_collapse_into_each_other():
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    renderer(_event(1, "tool.call", "read_file a.py", tool="read_file"))
    renderer(_event(2, "tool.result", "read_file ok", tool="read_file", ok=True))
    renderer(_event(3, "tool.call", "write_file b.py", tool="write_file"))
    renderer(_event(4, "tool.result", "write_file ok", tool="write_file", ok=True))
    renderer(_event(5, "turn.end", "done", status="done"))

    printed = buffer.getvalue()
    assert "a.py" in printed and "b.py" in printed
    assert "x2" not in printed


def test_a_failed_tool_is_loud():
    """One dim style for everything is why a failed run_tests used to vanish
    among eight successful reads."""
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    renderer(_event(1, "tool.call", "run_tests", tool="run_tests"))
    renderer(
        _event(
            2, "tool.result", "run_tests failed", tool="run_tests", ok=False,
            message="2 failed, 1 passed",
        )
    )
    renderer(_event(3, "turn.end", "done", status="done"))

    printed = buffer.getvalue()
    assert "FAILED" in printed
    assert "2 failed, 1 passed" in printed


def test_a_reasoning_trace_is_shown_but_is_never_an_action_row():
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    renderer(
        _event(
            1, "reasoning", "I should read it first",
            text_full="I should read it first, before changing anything.",
        )
    )
    renderer(_event(2, "turn.end", "done", status="done"))

    printed = buffer.getvalue()
    assert "before changing anything" in printed
    # Not a body line on any surface: a trace belongs to the response that
    # produced it, so it is never counted as a step of its own.
    assert renderer.lines == []


def test_a_write_shows_what_it_changed():
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    diff = "\n".join(
        [
            "--- config.py",
            "+++ config.py",
            "-REDIS = os.environ[KEY]",
            "+REDIS = os.environ.get(KEY, fallback)",
        ]
    )
    renderer(_event(1, "tool.call", "patch_file config.py", tool="patch_file"))
    renderer(
        _event(
            2, "tool.result", "patch_file ok", tool="patch_file", ok=True,
            target="config.py", diff=diff,
        )
    )
    renderer(_event(3, "turn.end", "done", status="done"))

    printed = buffer.getvalue()
    assert "Editing" in printed and "config.py" in printed
    assert "os.environ.get" in printed          # the added line
    assert "os.environ[KEY]" in printed         # the removed line


def test_an_approval_is_announced_and_answered():
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    renderer(
        _event(
            1, "approval", "approval needed: file_edit", phase="requested",
            action_type="file_edit", target="config.py", risk="medium",
        )
    )
    renderer(
        _event(
            2, "approval", "approval granted: file_edit", phase="resolved",
            action_type="file_edit", target="config.py", approved=True,
        )
    )
    renderer(_event(3, "turn.end", "done", status="done"))

    printed = buffer.getvalue()
    assert "approval needed" in printed
    assert "config.py" in printed
    assert "approved" in printed


def test_a_denied_approval_does_not_read_as_approved():
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    renderer(
        _event(
            1, "approval", "approval denied: run_command", phase="resolved",
            action_type="run_command", approved=False,
        )
    )
    renderer(_event(2, "turn.end", "stopped", status="stopped"))

    printed = buffer.getvalue()
    assert "DENIED" in printed
    assert "FAILED" in printed          # the verdict badge, not a success


def test_the_spinner_carries_the_context_meter_and_the_round():
    """Both numbers already existed and were displayed nowhere. You found out
    you had filled the window by watching the run degrade."""
    console, _buffer = _console()
    updates: list[str] = []
    renderer = CliTurnRenderer(console, status_updater=updates.append)

    renderer(
        _event(
            1, "status", "thinking... 4s",
            ctx_pct=68, ctx_text="ctx 68% (22.3k/32.8k)", round=4, max_rounds=25,
        )
    )

    assert updates, "the spinner was never updated"
    line = updates[-1]
    assert "thinking... 4s" in line
    assert "22.3k/32.8k" in line
    assert "rnd 4/25" in line


def test_an_unfinished_call_is_still_printed():
    """A turn that dies mid-tool must not swallow the row for the tool that was
    running when it died - that is the one you want to see."""
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    renderer(_event(1, "tool.call", "run_command npm start", tool="run_command"))
    renderer(_event(2, "error", "timed out after 600s"))

    printed = buffer.getvalue()
    assert "npm start" in printed
    assert "timed out after 600s" in printed


# --- defects the first rendered preview caught ----------------------------


def test_an_approval_does_not_print_its_tool_row_twice():
    """`patch_file` asks before it writes, so the approval fires from INSIDE
    the call that is still running. Flushing the pending row there printed the
    edit once unfinished at 0ms and again when the result arrived."""
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    renderer(_event(1, "tool.call", "patch_file calc.py", tool="patch_file"))
    renderer(
        _event(
            2, "approval", "approval needed: file_edit", phase="requested",
            action_type="file_edit", target="calc.py",
        )
    )
    renderer(
        _event(
            3, "approval", "approval granted: file_edit", phase="resolved",
            action_type="file_edit", target="calc.py", approved=True,
        )
    )
    renderer(
        _event(
            4, "tool.result", "patch_file ok", tool="patch_file", ok=True,
            target="calc.py", duration_ms=38,
        )
    )
    renderer(_event(5, "turn.end", "done", status="done"))

    printed = buffer.getvalue()
    assert printed.count("Editing") == 1, printed
    assert "38ms" in printed
    assert "0ms" not in printed


def test_a_collapsed_row_does_not_claim_one_calls_duration_for_all_of_them():
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    for seq in range(1, 5):
        renderer(_event(seq, "tool.call", "contract_status", tool="contract_status"))
        renderer(
            _event(
                seq + 50, "tool.result", "contract_status ok",
                tool="contract_status", ok=True, duration_ms=6,
            )
        )
    renderer(_event(90, "turn.end", "done", status="done"))

    printed = buffer.getvalue()
    assert "x4" in printed
    # The FIRST call carries its own real duration. The closing count carries
    # none - one call's 6ms printed beside "x4" would read as all four.
    assert printed.count("6ms") == 1, printed
    assert "6ms" not in printed.rsplit("x4", 1)[-1]


def test_a_failure_reads_before_its_duration():
    """How long a failure took is the least interesting thing about it."""
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    renderer(_event(1, "tool.call", "run_tests", tool="run_tests"))
    renderer(
        _event(
            2, "tool.result", "run_tests failed", tool="run_tests", ok=False,
            message="no runner configured", duration_ms=1840,
        )
    )

    printed = buffer.getvalue()
    assert printed.index("FAILED") < printed.index("1.8s")


def test_diff_headers_are_not_shown_as_changes():
    """`---`/`+++`/`@@` name the file you are already looking at, and would
    cost three of the eight lines on offer."""
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    diff = "\n".join(
        [
            "--- calc.py (before)",
            "+++ calc.py (after)",
            "@@ -4,3 +4,5 @@",
            " def divide(a, b):",
            "+    if b == 0:",
            "+        raise ValueError(msg)",
            "     return a / b",
        ]
    )
    renderer(_event(1, "tool.call", "patch_file calc.py", tool="patch_file"))
    renderer(
        _event(
            2, "tool.result", "patch_file ok", tool="patch_file", ok=True,
            target="calc.py", diff=diff,
        )
    )

    printed = buffer.getvalue()
    assert "if b == 0" in printed
    assert "raise ValueError" in printed
    assert "(before)" not in printed
    assert "(after)" not in printed
    assert "@@" not in printed


def test_a_thrash_collapses_across_the_model_lines_between_it():
    """The shape a real contract loop actually has.

    Live 2026-08-21 a 3B ran `contract_status` eight times, each separated by
    its own "model responded" line. Suppressing only the tool rows would leave
    eight model lines behind and the wall would still be a wall, so the replies
    inside a run of identical calls are held too. Sixteen events, three lines.
    """
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    renderer(_event(1, "turn.start", "go"))
    for index in range(8):
        renderer(
            _event(index * 3 + 2, "tool.call", "contract_status", tool="contract_status")
        )
        renderer(
            _event(
                index * 3 + 3, "tool.result", "contract_status ok",
                tool="contract_status", ok=True, duration_ms=6,
            )
        )
        renderer(_event(index * 3 + 4, "activity", "model responded in 1s"))
    renderer(_event(99, "turn.end", "done", status="done"))

    printed = buffer.getvalue()
    rows = [line for line in printed.splitlines() if line.strip()]
    assert len(rows) <= 4, rows          # two tool rows, one badge, one blank
    assert "x8" in printed
    assert printed.count("model responded in 1s") == 1

    # Every one of the sixteen is still on the parity list. Collapsing is a
    # decision about paint, never about what happened.
    assert len([line for line in renderer.lines if "contract_status" in line]) == 8
    assert len([line for line in renderer.lines if "model responded" in line]) == 8


def test_model_lines_outside_a_streak_are_still_printed():
    """The suppression is scoped to a run of identical calls. Ordinary replies
    between different tools are the narration of the turn."""
    console, buffer = _console()
    renderer = CliTurnRenderer(console)

    renderer(_event(1, "activity", "model responded in 3s"))
    renderer(_event(2, "tool.call", "read_file a.py", tool="read_file"))
    renderer(_event(3, "tool.result", "read_file ok", tool="read_file", ok=True))
    renderer(_event(4, "activity", "model responded in 5s"))
    renderer(_event(5, "tool.call", "write_file b.py", tool="write_file"))
    renderer(_event(6, "tool.result", "write_file ok", tool="write_file", ok=True))
    renderer(_event(7, "turn.end", "done", status="done"))

    printed = buffer.getvalue()
    assert "model responded in 3s" in printed
    assert "model responded in 5s" in printed
    assert "x2" not in printed
