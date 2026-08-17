from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from shamsu.action_ledger import store as action_store
from shamsu.agents.chat_loop import AgentLoopResult
import shamsu.integrations.telegram.sessions as telegram_sessions
from shamsu.integrations.telegram.authentication import TelegramAuthenticator
from shamsu.integrations.telegram.callbacks import CallbackRegistry
from shamsu.integrations.telegram.commands import parse_command
from shamsu.integrations.telegram.controller import TelegramController
from shamsu.integrations.telegram.files import TelegramFileStager, sanitize_filename
from shamsu.integrations.telegram.formatter import TelegramFormatter
from shamsu.integrations.telegram.models import (
    CallbackAction,
    TelegramCallbackQuery,
    TelegramChat,
    TelegramFile,
    TelegramInboundMetadata,
    TelegramMessage,
    TelegramSessionSummary,
    TelegramUpdate,
    TelegramUser,
)
from shamsu.integrations.telegram.notifications import NotificationFilter
from shamsu.integrations.telegram.pairing import PairingManager
from shamsu.integrations.telegram.sessions import RoutedMessageResult
from shamsu.integrations.telegram.local import redact_remote_control_command
from shamsu.integrations.telegram.service import (
    TelegramService,
    configure_telegram_bot_token,
    load_telegram_bot_token,
)
from shamsu.integrations.telegram.storage import TelegramStateStore
from shamsu.integrations.telegram.transport import FakeTelegramTransport
from shamsu.runtime.run_control import complete_run, get_run_status, pause_run, register_run
from shamsu.session.manager import SessionManager
from shamsu.types import RunStatus


USER = TelegramUser(telegram_user_id := 1001, first_name="Ada", username="ada")
CHAT = TelegramChat(chat_id=2002, chat_type="private")


@dataclass
class FakeGateway:
    sessions: list[TelegramSessionSummary]
    routed: list[tuple[str, TelegramInboundMetadata]]
    cancelled: list[str]
    paused: list[str]
    resumed: list[str]

    def list_sessions(self) -> list[TelegramSessionSummary]:
        return list(self.sessions)

    def ensure_default_session(self) -> str:
        if not self.sessions:
            self.sessions.append(
                TelegramSessionSummary(
                    session_id="session-1",
                    display_name="auth-refactor",
                    workspace="workspace",
                    status="idle",
                )
            )
        return self.sessions[0].session_id

    def switch_session(self, session_id: str) -> TelegramSessionSummary:
        for session in self.sessions:
            if session.session_id == session_id:
                return session
        raise ValueError(session_id)

    def create_session(self, title: str | None = None) -> TelegramSessionSummary:
        session = TelegramSessionSummary(
            session_id=f"session-{len(self.sessions) + 1}",
            display_name=title or "Untitled Session",
            workspace="workspace",
        )
        self.sessions.append(session)
        return session

    def status(self, session_id: str):
        from shamsu.integrations.telegram.models import TelegramRuntimeStatus

        session = self.switch_session(session_id)
        return TelegramRuntimeStatus(
            session=session,
            status=session.status or "idle",
            task=session.current_task,
            phase="AUTHOR" if session.current_run else "",
            run_id=session.current_run,
        )

    def plan(self, session_id: str):
        return "OAuth Login", [("completed", "Inspect auth"), ("active", "Patch route")]

    def changes(self, session_id: str) -> str:
        return "2 files changed\n- app.py\n- tests/test_app.py"

    def tests(self, session_id: str) -> str:
        return "42 passed"

    def logs(self, session_id: str) -> list[str]:
        return ["09:21 Read app.py", "09:22 Ran tests"]

    async def route_user_message(self, text: str, *, metadata: TelegramInboundMetadata):
        self.routed.append((text, metadata))
        return RoutedMessageResult("Task accepted by SHAMSU.", run_id="run-1", status="running")

    def cancel_current_run(self, session_id: str) -> bool:
        self.cancelled.append(session_id)
        return True

    def pause_current_run(self, session_id: str) -> bool:
        self.paused.append(session_id)
        return True

    def resume_current_run(self, session_id: str) -> bool:
        self.resumed.append(session_id)
        return True


@dataclass
class BlockingGateway(FakeGateway):
    started: asyncio.Event
    release: asyncio.Event

    async def route_user_message(self, text: str, *, metadata: TelegramInboundMetadata):
        self.routed.append((text, metadata))
        self.started.set()
        await self.release.wait()
        return RoutedMessageResult("Finished slow work.", run_id="run-bg", status="success")


def _store(tmp_path: Path) -> TelegramStateStore:
    return TelegramStateStore(tmp_path, db_path=tmp_path / "telegram.db")


def _controller(tmp_path: Path):
    store = _store(tmp_path)
    pairing = PairingManager(store, installation_id="install-test")
    callbacks = CallbackRegistry(store)
    gateway = FakeGateway(
        sessions=[
            TelegramSessionSummary(
                session_id="session-1",
                display_name="auth-refactor",
                workspace=str(tmp_path),
                status="running",
                current_task="Add OAuth",
                current_run="run-1",
            ),
            TelegramSessionSummary(
                session_id="session-2",
                display_name="dashboard",
                workspace=str(tmp_path),
                status="idle",
            ),
        ],
        routed=[],
        cancelled=[],
        paused=[],
        resumed=[],
    )
    controller = TelegramController(
        store=store,
        authenticator=TelegramAuthenticator(store),
        pairing=pairing,
        callbacks=callbacks,
        gateway=gateway,
        workspace=tmp_path,
        approval_resolver=lambda approval_id, approved: approval_id == "approval-1" and approved,
    )
    return store, pairing, callbacks, gateway, controller


def _message(text: str, *, user: TelegramUser = USER, chat: TelegramChat = CHAT, message_id: int = 1):
    return TelegramMessage(message_id=message_id, user=user, chat=chat, text=text)


async def _drain_service_background(service: TelegramService) -> None:
    for _ in range(20):
        tasks = list(service._background_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks)
    raise AssertionError("Telegram background tasks did not finish")


def test_command_parsing() -> None:
    assert parse_command("/status").name == "/status"
    assert parse_command("/status details").argument == "details"
    assert parse_command("hello") is None


def test_pairing_authorizes_stable_user_id_not_username(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pairing = PairingManager(store, installation_id="install-test")
    code = pairing.create_code()
    assert pairing.verify(code.code, user=USER, chat=CHAT).ok

    spoof = TelegramUser(9999, first_name="Mallory", username="ada")
    result = TelegramAuthenticator(store).authorize(spoof, CHAT)
    assert not result.ok


def test_expired_and_reused_pairing_codes_fail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    expired = PairingManager(store, installation_id="install-test", ttl_seconds=-1).create_code()
    assert "expired" in PairingManager(store, installation_id="install-test").verify(expired.code, user=USER, chat=CHAT).reason

    pairing = PairingManager(store, installation_id="install-test")
    code = pairing.create_code()
    assert pairing.verify(code.code, user=USER, chat=CHAT).ok
    assert "already" in pairing.verify(code.code, user=USER, chat=CHAT).reason


def test_group_chat_pairing_and_authorization_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pairing = PairingManager(store, installation_id="install-test")
    code = pairing.create_code()
    group = TelegramChat(chat_id=3003, chat_type="group")
    assert not pairing.verify(code.code, user=USER, chat=group).ok
    assert not TelegramAuthenticator(store).authorize(USER, group).ok


def test_unauthorized_user_cannot_control_shamsu(tmp_path: Path) -> None:
    _store_obj, _pairing, _callbacks, gateway, controller = _controller(tmp_path)
    replies = asyncio.run(controller.handle_update(TelegramUpdate(1, message=_message("Implement auth"))))
    assert "not paired" in replies[0].text
    assert gateway.routed == []


def test_telegram_message_routes_to_active_shamsu_session(tmp_path: Path) -> None:
    store, pairing, _callbacks, gateway, controller = _controller(tmp_path)
    code = pairing.create_code()
    assert pairing.verify(code.code, user=USER, chat=CHAT).ok
    store.set_active_session(USER.user_id, "session-1")

    replies = asyncio.run(controller.handle_update(TelegramUpdate(1, message=_message("Implement auth"))))

    assert replies[0].text == "Task accepted by SHAMSU."
    assert gateway.routed[0][0] == "Implement auth"
    assert gateway.routed[0][1].source == "telegram"
    assert gateway.routed[0][1].session_id == "session-1"


def test_duplicate_update_is_ignored(tmp_path: Path) -> None:
    store, pairing, _callbacks, gateway, controller = _controller(tmp_path)
    assert pairing.verify(pairing.create_code().code, user=USER, chat=CHAT).ok
    store.set_active_session(USER.user_id, "session-1")
    update = TelegramUpdate(7, message=_message("run once"))

    asyncio.run(controller.handle_update(update))
    second = asyncio.run(controller.handle_update(update))

    assert second == []
    assert len(gateway.routed) == 1
    assert store.metrics()["telegram_duplicate_updates"] == 1


def test_session_switching_uses_opaque_callback(tmp_path: Path) -> None:
    store, pairing, callbacks, gateway, controller = _controller(tmp_path)
    assert pairing.verify(pairing.create_code().code, user=USER, chat=CHAT).ok
    action_id = callbacks.create(
        CallbackAction.SWITCH_SESSION,
        telegram_user_id=USER.user_id,
        telegram_chat_id=CHAT.chat_id,
        session_id="session-2",
        payload={"session_id": "session-2"},
    )
    callback = TelegramCallbackQuery("cb-1", USER, _message("", message_id=33), action_id)

    replies = asyncio.run(controller.handle_update(TelegramUpdate(8, callback_query=callback)))

    assert store.active_session_for(USER.user_id) == "session-2"
    assert replies[0].edit_message_id == 33
    assert gateway.switch_session("session-2").display_name == "dashboard"


def test_callback_wrong_user_wrong_run_and_duplicate_are_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    callbacks = CallbackRegistry(store)
    action_id = callbacks.create(
        CallbackAction.CANCEL_RUN,
        telegram_user_id=USER.user_id,
        telegram_chat_id=CHAT.chat_id,
        session_id="session-1",
        run_id="run-1",
    )
    wrong_user = TelegramCallbackQuery(
        "cb-wrong",
        TelegramUser(404, username="ada"),
        _message("", message_id=2),
        action_id,
    )
    assert not callbacks.validate(wrong_user).ok
    callback = TelegramCallbackQuery("cb-ok", USER, _message("", message_id=2), action_id)
    assert not callbacks.validate(callback, expected_run_id="run-2").ok
    assert callbacks.validate(callback, expected_run_id="run-1").ok
    assert not callbacks.validate(callback, expected_run_id="run-1").ok


def test_approval_callback_validation_and_duplicate_resolution(tmp_path: Path) -> None:
    store, pairing, callbacks, _gateway, controller = _controller(tmp_path)
    assert pairing.verify(pairing.create_code().code, user=USER, chat=CHAT).ok
    action_id = callbacks.create(
        CallbackAction.APPROVE,
        telegram_user_id=USER.user_id,
        telegram_chat_id=CHAT.chat_id,
        session_id="session-1",
        run_id="run-1",
        approval_id="approval-1",
    )
    callback = TelegramCallbackQuery("cb-approval", USER, _message("", message_id=4), action_id)

    first = asyncio.run(controller.handle_update(TelegramUpdate(10, callback_query=callback)))
    duplicate = asyncio.run(controller.handle_update(TelegramUpdate(11, callback_query=callback)))

    assert "Approved" in first[0].text
    assert "already" in duplicate[0].text.lower()


def test_cancel_pause_resume_commands_use_gateway(tmp_path: Path) -> None:
    store, pairing, _callbacks, gateway, controller = _controller(tmp_path)
    assert pairing.verify(pairing.create_code().code, user=USER, chat=CHAT).ok
    store.set_active_session(USER.user_id, "session-1")

    asyncio.run(controller.handle_update(TelegramUpdate(12, message=_message("/pause"))))
    asyncio.run(controller.handle_update(TelegramUpdate(13, message=_message("/resume"))))
    cancel_prompt = asyncio.run(controller.handle_update(TelegramUpdate(14, message=_message("/cancel"))))

    assert gateway.paused == ["session-1"]
    assert gateway.resumed == ["session-1"]
    assert "Cancel current task" in cancel_prompt[0].text


def test_real_run_control_pause_and_cancel_for_session(tmp_path: Path) -> None:
    logger = SessionManager(tmp_path).create_session("remote")
    run = register_run("run-telegram-test", session_logger=logger)
    gateway = __import__(
        "shamsu.integrations.telegram.sessions",
        fromlist=["LocalShamsuSessionGateway"],
    ).LocalShamsuSessionGateway(tmp_path)

    assert gateway.pause_current_run(logger.session_id)
    assert get_run_status(run.run_id)["paused"] is True
    assert pause_run(run.run_id)
    assert gateway.cancel_current_run(logger.session_id)
    assert get_run_status(run.run_id)["cancel_requested"] is True
    complete_run(run.run_id, RunStatus.CANCELLED, "done")


def test_local_gateway_free_text_closes_action_ledger_without_false_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    logger = SessionManager(tmp_path).create_session("remote")

    class FakeAgentChatLoop:
        def __init__(self, *_args, action_ledger=None, **_kwargs) -> None:
            self.action_ledger = action_ledger

        async def run(self, text: str) -> AgentLoopResult:
            return AgentLoopResult(
                final=f"Echo: {text}",
                run_id=self.action_ledger.run_id,
                status=RunStatus.COMPLETED,
            )

    monkeypatch.setattr(telegram_sessions, "AgentChatLoop", FakeAgentChatLoop)
    gateway = telegram_sessions.LocalShamsuSessionGateway(tmp_path)

    result = asyncio.run(
        gateway.route_user_message(
            "Hi",
            metadata=TelegramInboundMetadata(
                source="telegram",
                telegram_user_id=USER.user_id,
                telegram_chat_id=CHAT.chat_id,
                telegram_message_id=44,
                session_id=logger.session_id,
                timestamp=datetime.now(timezone.utc),
            ),
        )
    )

    assert result.text == "Echo: Hi"
    assert result.status == "success"
    manifest = action_store.load_manifest(tmp_path, result.run_id)
    assert manifest is not None
    assert manifest["status"] == "success"
    reloaded = SessionManager(tmp_path).resolve(logger.session_id)
    assert reloaded.last_user_prompt == "Hi"


def test_local_gateway_sends_telegram_progress_notifications(
    tmp_path: Path,
    monkeypatch,
) -> None:
    logger = SessionManager(tmp_path).create_session("remote")
    sent = []

    class FakeApprovalBroker:
        decision_timeout_seconds = 900.0

        def notify(self, message):
            sent.append(message)

        def approval_func(self, **_kwargs):
            return lambda _request: True

    class FakeAgentChatLoop:
        timeout_seen = 0.0

        def __init__(self, *_args, progress=None, max_runtime_seconds=None, action_ledger=None, **_kwargs) -> None:
            self.progress = progress
            self.action_ledger = action_ledger
            FakeAgentChatLoop.timeout_seen = float(max_runtime_seconds or 0)

        async def run(self, text: str) -> AgentLoopResult:
            self.progress.step("Thinking... choosing action 1/4")
            self.progress.tool_start("file.patch", "file=primes.py")
            return AgentLoopResult(
                final=f"Echo: {text}",
                run_id=self.action_ledger.run_id,
                status=RunStatus.COMPLETED,
            )

    monkeypatch.setattr(telegram_sessions, "AgentChatLoop", FakeAgentChatLoop)
    gateway = telegram_sessions.LocalShamsuSessionGateway(
        tmp_path,
        approval_broker=FakeApprovalBroker(),
    )

    result = asyncio.run(
        gateway.route_user_message(
            "Hi",
            metadata=TelegramInboundMetadata(
                source="telegram",
                telegram_user_id=USER.user_id,
                telegram_chat_id=CHAT.chat_id,
                telegram_message_id=45,
                session_id=logger.session_id,
                timestamp=datetime.now(timezone.utc),
            ),
        )
    )

    assert result.text == "Echo: Hi"
    assert FakeAgentChatLoop.timeout_seen == 1800.0
    assert any("SHAMSU remote task started" in message.text for message in sent)
    assert any("Using tool: file.patch" in message.text for message in sent)


def test_message_formatting_and_pagination_redact_secrets() -> None:
    formatter = TelegramFormatter()
    pages = formatter.paginate("token=abc123\n" + ("x" * 5000), max_chars=1000)
    assert len(pages) > 1
    assert "abc123" not in pages[0].text


def test_settings_and_notification_filter() -> None:
    from shamsu.integrations.telegram.models import TelegramSettings

    filt = NotificationFilter()
    settings = TelegramSettings(tool_by_tool_updates=False)
    assert filt.should_send("approval_required", settings)
    assert not filt.should_send("tool", settings)


def test_file_upload_stages_safely_and_sanitizes_names(tmp_path: Path) -> None:
    stager = TelegramFileStager(tmp_path)
    assert sanitize_filename("../../secret.env") == "secret.env"
    staged = stager.stage_metadata_only(
        TelegramFile("file-1", "../../secret.env", file_size=3),
        content=b"abc",
    )
    assert staged.ok
    assert staged.path is not None
    assert staged.path.read_bytes() == b"abc"
    assert tmp_path.resolve() in staged.path.resolve().parents


def test_bot_token_never_appears_in_local_panel_or_audit(tmp_path: Path) -> None:
    secret = "123456:SUPER_SECRET_TOKEN"
    service = TelegramService(tmp_path, token=secret, transport=FakeTelegramTransport())
    panel = service.local_panel("status")
    assert secret not in panel.message
    service.store.audit(action="test", result="ok", payload={"bot_token": secret})
    raw = service.store.list_audit()[0]["payload"]
    assert secret not in raw
    assert "[REDACTED]" in raw


def test_token_can_load_from_ignored_local_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SHAMSU_TELEGRAM_BOT_TOKEN", raising=False)
    (tmp_path / ".env").write_text(
        "SHAMSU_TELEGRAM_BOT_TOKEN=from-dotenv\n",
        encoding="utf-8",
    )
    token, source = load_telegram_bot_token(tmp_path)
    assert token == "from-dotenv"
    assert source == ".env"

    shamsu_dir = tmp_path / ".shamsu"
    shamsu_dir.mkdir()
    (shamsu_dir / "telegram.env").write_text(
        "SHAMSU_TELEGRAM_BOT_TOKEN='from-shamsu-file'\n",
        encoding="utf-8",
    )
    token, source = load_telegram_bot_token(tmp_path)
    assert token == "from-shamsu-file"
    assert source == ".shamsu/telegram.env"


def test_environment_token_wins_over_local_files(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "SHAMSU_TELEGRAM_BOT_TOKEN=from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHAMSU_TELEGRAM_BOT_TOKEN", "from-env")

    token, source = load_telegram_bot_token(tmp_path)

    assert token == "from-env"
    assert source == "environment"


def test_remote_control_configure_writes_ignored_local_token_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SHAMSU_TELEGRAM_BOT_TOKEN", raising=False)
    token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"

    path = configure_telegram_bot_token(tmp_path, token)
    loaded, source = load_telegram_bot_token(tmp_path)

    assert path == tmp_path / ".shamsu" / "telegram.env"
    assert loaded == token
    assert source == ".shamsu/telegram.env"


def test_remote_control_configure_command_redacts_logged_token() -> None:
    token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
    assert redact_remote_control_command(f"/remote_control configure {token}") == (
        "/remote_control configure [REDACTED]"
    )
    assert redact_remote_control_command(f"remote_control configure {token}") == (
        "remote_control configure [REDACTED]"
    )


def test_invalid_telegram_token_is_rejected(tmp_path: Path) -> None:
    try:
        configure_telegram_bot_token(tmp_path, "not-a-token")
    except ValueError as exc:
        assert "Telegram bot token" in str(exc)
    else:
        raise AssertionError("invalid token was accepted")


def test_service_sends_shamsu_response_to_telegram(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pairing = PairingManager(store, installation_id="install-test")
    gateway = FakeGateway(
        sessions=[TelegramSessionSummary("session-1", "auth-refactor", str(tmp_path))],
        routed=[],
        cancelled=[],
        paused=[],
        resumed=[],
    )
    transport = FakeTelegramTransport()
    service = TelegramService(
        tmp_path,
        token="fake-token",
        transport=transport,
        store=store,
        gateway=gateway,
        installation_id="install-test",
    )
    assert pairing.verify(pairing.create_code().code, user=USER, chat=CHAT).ok
    store.set_active_session(USER.user_id, "session-1")

    async def scenario() -> None:
        await service.process_update(TelegramUpdate(50, message=_message("Do work")))
        await _drain_service_background(service)

    asyncio.run(scenario())

    assert transport.sent[-1].text == "Task accepted by SHAMSU."
    assert gateway.routed[0][0] == "Do work"


def test_service_processes_status_while_task_is_running(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = _store(tmp_path)
        gateway = BlockingGateway(
            sessions=[
                TelegramSessionSummary(
                    "session-1",
                    "auth-refactor",
                    str(tmp_path),
                    status="running",
                    current_task="Do slow work",
                    current_run="run-bg",
                )
            ],
            routed=[],
            cancelled=[],
            paused=[],
            resumed=[],
            started=asyncio.Event(),
            release=asyncio.Event(),
        )
        transport = FakeTelegramTransport()
        service = TelegramService(
            tmp_path,
            token="fake-token",
            transport=transport,
            store=store,
            gateway=gateway,
            installation_id="install-test",
        )
        assert service.pairing.verify(service.pairing.create_code().code, user=USER, chat=CHAT).ok
        store.set_active_session(USER.user_id, "session-1")

        await service.process_update(TelegramUpdate(60, message=_message("Do slow work")))
        assert transport.sent[0].text.startswith("Task received.")
        await asyncio.wait_for(gateway.started.wait(), timeout=1)

        await service.process_update(TelegramUpdate(61, message=_message("/status", message_id=2)))

        assert any(message.text.startswith("SHAMSU - Running") for message in transport.sent)
        assert not gateway.release.is_set()

        gateway.release.set()
        await _drain_service_background(service)

        assert transport.sent[-1].text == "Finished slow work."

    asyncio.run(scenario())


def test_service_mirrors_authorized_telegram_messages_to_cli(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gateway = FakeGateway(
        sessions=[TelegramSessionSummary("session-1", "auth-refactor", str(tmp_path))],
        routed=[],
        cancelled=[],
        paused=[],
        resumed=[],
    )
    transport = FakeTelegramTransport()
    mirrored: list[tuple[str, str]] = []
    service = TelegramService(
        tmp_path,
        token="fake-token",
        transport=transport,
        store=store,
        gateway=gateway,
        installation_id="install-test",
        cli_mirror=lambda title, text: mirrored.append((title, text)),
    )
    assert service.pairing.verify(service.pairing.create_code().code, user=USER, chat=CHAT).ok
    store.set_active_session(USER.user_id, "session-1")

    async def scenario() -> None:
        await service.process_update(TelegramUpdate(51, message=_message("Do work")))
        await _drain_service_background(service)

    asyncio.run(scenario())

    assert ("Telegram Ada", "Do work") in mirrored
    assert ("SHAMSU -> Telegram", "Task accepted by SHAMSU.") in mirrored


def test_service_masks_pairing_code_in_cli_mirror(tmp_path: Path) -> None:
    store = _store(tmp_path)
    gateway = FakeGateway(
        sessions=[],
        routed=[],
        cancelled=[],
        paused=[],
        resumed=[],
    )
    mirrored: list[tuple[str, str]] = []
    service = TelegramService(
        tmp_path,
        token="fake-token",
        transport=FakeTelegramTransport(),
        store=store,
        gateway=gateway,
        installation_id="install-test",
        cli_mirror=lambda title, text: mirrored.append((title, text)),
    )
    code = service.pairing.create_code().code

    asyncio.run(service.process_update(TelegramUpdate(52, message=_message(code))))

    assert ("Telegram Ada", "entered a pairing code.") in mirrored
    assert not any(code in text for _title, text in mirrored)
    assert any("Connected to this SHAMSU installation." in text for _title, text in mirrored)


def test_bot_restart_keeps_pairing_active_session_and_offset(tmp_path: Path) -> None:
    db_path = tmp_path / "telegram.db"
    store = TelegramStateStore(tmp_path, db_path=db_path)
    pairing = PairingManager(store, installation_id="install-test")
    assert pairing.verify(pairing.create_code().code, user=USER, chat=CHAT).ok
    store.set_active_session(USER.user_id, "session-1")
    store.set_meta("telegram_update_offset", "123")

    reloaded = TelegramStateStore(tmp_path, db_path=db_path)

    assert TelegramAuthenticator(reloaded).authorize(USER, CHAT).ok
    assert reloaded.active_session_for(USER.user_id) == "session-1"
    assert reloaded.get_meta("telegram_update_offset") == "123"


def test_expired_callback_fails(tmp_path: Path) -> None:
    store = _store(tmp_path)
    callbacks = CallbackRegistry(store, ttl_seconds=1)
    action_id = callbacks.create(
        CallbackAction.CANCEL_RUN,
        telegram_user_id=USER.user_id,
        telegram_chat_id=CHAT.chat_id,
        session_id="session-1",
        run_id="run-1",
    )
    store.set_meta("unused", "value")
    with store._connect() as conn:
        conn.execute(
            "UPDATE callback_actions SET expires_at = ? WHERE action_id = ?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), action_id),
        )
    callback = TelegramCallbackQuery("cb-expired", USER, _message("", message_id=9), action_id)
    assert not callbacks.validate(callback).ok
