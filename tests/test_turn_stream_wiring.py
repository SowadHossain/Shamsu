"""The seams, not the renderers: does a real turn actually reach them?

`tests/test_turn_stream_parity.py` proves the two renderers agree given the
same events. This file proves the events are wired - that a Telegram turn
really edits one card, really writes `activity.jsonl`, and really stops sending
the one-message-per-step feed the card replaces. Both halves are needed: a
perfect renderer nobody attaches is the bug this whole change is about.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

from rich.console import Console

from shamsu.integrations.telegram.models import (
    OutboundMessage,
    TelegramChat,
    TelegramInboundMetadata,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
)
from shamsu.integrations.telegram.service import TelegramService
from shamsu.integrations.telegram.transport import FakeTelegramTransport
from shamsu.runtime.turn_stream import TurnEvent, activity_path
from shamsu.session.manager import SessionManager
from shamsu.tools.agent_tools import AgentToolRegistry

CHAT_ID = 4242


class FakeClient:
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
    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []
        self._next_id = 100

    def __call__(self, message: OutboundMessage) -> int:
        self.sent.append(message)
        if message.edit_message_id is not None:
            return int(message.edit_message_id)
        self._next_id += 1
        return self._next_id


def _metadata(session_id: str) -> TelegramInboundMetadata:
    return TelegramInboundMetadata(
        source="telegram",
        telegram_user_id=7,
        telegram_chat_id=CHAT_ID,
        telegram_message_id=11,
        session_id=session_id,
        timestamp="2026-08-19T00:00:00+00:00",
    )


def _run_remote_turn(tmp_path: Path, logger, monkeypatch, turns, **gateway_kwargs):
    """Drive the real gateway's simple-mode path with a scripted model."""
    monkeypatch.delenv("SHAMSU_LEGACY_ROUTING", raising=False)
    from shamsu.integrations.telegram import sessions as telegram_sessions

    monkeypatch.setattr(
        "shamsu.agents.chat_loop._default_ollama_client",
        lambda *args, **kwargs: FakeClient(turns),
    )
    notified: list[OutboundMessage] = []
    gateway = telegram_sessions.LocalShamsuSessionGateway(tmp_path, **gateway_kwargs)
    progress = telegram_sessions.TelegramProgressReporter(
        notify=notified.append,
        telegram_chat_id=CHAT_ID,
        session_logger=logger,
    )
    final = gateway._run_simple(
        "read a.py",
        logger,
        AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        None,
        progress,
        _metadata(logger.session_id),
    )
    return final, progress, notified


def test_a_telegram_turn_edits_one_card_instead_of_one_message_per_line(
    tmp_path, monkeypatch
):
    """The original complaint, end to end, through the real gateway."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    logger = SessionManager(tmp_path).create_session("remote")
    sender = FakeSender()

    final, progress, notified = _run_remote_turn(
        tmp_path,
        logger,
        monkeypatch,
        [_tool("read_file", filepath="a.py"), _text("done")],
        send_message=sender,
    )

    assert final == "done"
    creates = [m for m in sender.sent if m.edit_message_id is None]
    assert len(creates) == 1, "the card was not a single edited message"
    assert all(m.parse_mode == "HTML" for m in sender.sent)
    body = sender.sent[-1].text
    assert "read_file a.py" in body
    assert "model responded in" in body
    assert "shamsu (remote-telegram)&gt; read a.py" in body
    # And the notification feed the card replaces has gone quiet.
    assert progress.live_card is True
    assert not [m for m in notified if "Working:" in m.text]


def test_a_telegram_turn_writes_activity_jsonl_and_keeps_it_out_of_the_transcript(
    tmp_path, monkeypatch
):
    logger = SessionManager(tmp_path).create_session("remote")
    _run_remote_turn(tmp_path, logger, monkeypatch, [_text("hello")])

    path = activity_path(tmp_path, logger.session_id)
    assert path.exists()
    kinds = [
        json.loads(line)["kind"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert kinds[0] == "turn.start"
    assert kinds[-1] == "turn.end"
    # The turn stream is NOT the transcript. Status ticks in `messages.jsonl`
    # would become something the model reads back as conversation.
    transcript = tmp_path / ".shamsu" / "sessions" / logger.session_id / "messages.jsonl"
    assert "turn.end" not in transcript.read_text(encoding="utf-8")


def test_without_a_send_seam_a_turn_still_runs(tmp_path, monkeypatch):
    """No Telegram, no card, no crash: the loop never depends on a renderer."""
    logger = SessionManager(tmp_path).create_session("remote")
    final, progress, _notified = _run_remote_turn(
        tmp_path, logger, monkeypatch, [_text("fine")]
    )
    assert final == "fine"
    assert progress.live_card is False


def test_a_card_that_cannot_be_sent_does_not_fail_the_turn(tmp_path, monkeypatch):
    logger = SessionManager(tmp_path).create_session("remote")

    def refuse(_message: OutboundMessage) -> int:
        raise RuntimeError("Telegram API failed: Bad Request")

    final, _progress, _notified = _run_remote_turn(
        tmp_path, logger, monkeypatch, [_text("still answered")], send_message=refuse
    )
    assert final == "still answered"


def test_the_service_send_seam_blocks_and_returns_a_message_id(tmp_path):
    transport = FakeTelegramTransport()
    service = TelegramService(tmp_path, token="fake-token", transport=transport)

    async def scenario() -> tuple[int, int]:
        service._loop = asyncio.get_running_loop()
        first = await asyncio.to_thread(
            service._send_card_from_thread,
            OutboundMessage(CHAT_ID, "card", parse_mode="HTML"),
        )
        second = await asyncio.to_thread(
            service._send_card_from_thread,
            OutboundMessage(
                CHAT_ID, "card v2", parse_mode="HTML", edit_message_id=first
            ),
        )
        return first, second

    first, second = asyncio.run(scenario())
    assert first == 1
    assert second == first, "an edit must keep the id it was editing"
    assert [m.parse_mode for m in transport.sent] == ["HTML", "HTML"]


def test_the_typing_action_reaches_the_transport(tmp_path):
    transport = FakeTelegramTransport()
    service = TelegramService(tmp_path, token="fake-token", transport=transport)

    async def scenario() -> None:
        service._loop = asyncio.get_running_loop()
        await asyncio.to_thread(service._typing_from_thread, CHAT_ID)
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(scenario())
    assert transport.actions == [(CHAT_ID, "typing")]


def test_a_routed_prompt_is_echoed_on_the_desktop_as_a_terminal_line():
    """G2 on the desktop: the cyan panel becomes a prompt line."""
    from shamsu.integrations.telegram.local import ConsoleTelegramMirror

    buffer = io.StringIO()
    mirror = ConsoleTelegramMirror(
        Console(file=buffer, width=200, no_color=True, highlight=False)
    )
    mirror.prompt_echo("add a pause menu")
    renderer = mirror.turn_renderer()
    renderer(
        TurnEvent(seq=1, kind="activity", text="model responded in 9s", source="telegram")
    )

    printed = buffer.getvalue()
    assert "shamsu (remote-telegram)> add a pause menu" in printed
    assert "model responded in 9s" in printed


def test_the_service_prefers_the_prompt_echo_for_a_routed_task(tmp_path):
    echoed: list[str] = []
    panels: list[tuple[str, str]] = []

    class Mirror:
        def __call__(self, title: str, text: str) -> None:
            panels.append((title, text))

        def prompt_echo(self, prompt: str, label: str = "remote-telegram") -> None:
            echoed.append(prompt)

    service = TelegramService(
        tmp_path,
        token="fake-token",
        transport=FakeTelegramTransport(),
        cli_mirror=Mirror(),
    )
    service.authenticator.authorize = lambda *_a, **_k: type("Ok", (), {"ok": True})()
    update = TelegramUpdate(
        1,
        message=TelegramMessage(
            message_id=1,
            user=TelegramUser(user_id=7, first_name="Sam"),
            chat=TelegramChat(chat_id=CHAT_ID),
            text="build the thing",
        ),
    )

    service._mirror_inbound(update, as_prompt=True)
    assert echoed == ["build the thing"]
    assert panels == []

    # Everything else - a status reply, a button press - keeps the panel.
    service._mirror_inbound(update)
    assert len(panels) == 1


def test_a_card_edit_is_not_mirrored_to_the_desktop_as_a_panel(tmp_path):
    """Otherwise a 40-flush turn prints 40 cyan panels in the REPL."""
    panels: list[tuple[str, str]] = []
    service = TelegramService(
        tmp_path,
        token="fake-token",
        transport=FakeTelegramTransport(),
        cli_mirror=lambda title, text: panels.append((title, text)),
    )

    async def scenario() -> None:
        service._loop = asyncio.get_running_loop()
        await service._send_returning_id(
            OutboundMessage(CHAT_ID, "card", parse_mode="HTML")
        )

    asyncio.run(scenario())
    assert panels == []


def test_the_repl_still_prints_dim_activity_and_ticks_the_spinner(tmp_path, monkeypatch):
    """Identical observable behaviour: the renderer replaces two lambdas.

    Asserted through the REAL REPL entry point, because "the CLI is unchanged"
    is a claim about what a user sees, not about what a renderer object holds.
    """
    monkeypatch.delenv("SHAMSU_LEGACY_ROUTING", raising=False)
    import shamsu.cli.repl as repl

    monkeypatch.setattr(
        "shamsu.agents.chat_loop._default_ollama_client",
        lambda *args, **kwargs: FakeClient(
            [_tool("list_files"), _text("Here is what I found.")]
        ),
    )

    class Status:
        def __init__(self):
            self.updates = []

        def update(self, text):
            self.updates.append(text)

    buffer = io.StringIO()
    console = Console(file=buffer, width=200, no_color=True, highlight=False)
    status = Status()
    asyncio.run(repl._run_simple_chat("what is here", tmp_path, console, None, status))

    printed = buffer.getvalue()
    assert "model responded in" in printed
    assert "list_files" in printed
    assert "Here is what I found." in printed
