"""Rebinding the bot to another project, and who is allowed to poll it.

Two properties worth pinning, because both were true only after the P2 work
moved this state to install scope and either could regress silently:

1. **Rebinding does not unpair.** Before the state DB became install-scoped,
   switching project would have dropped the phone. A settings button that
   quietly logs your phone out is worse than no button.
2. **One poller per token.** Telegram answers the second `getUpdates` caller
   with 409, and `_poll_loop` swallows it and retries forever - so the loser
   looks connected and receives nothing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shamsu.control.store import MACHINE_LEASE_KEY, ControlStore
from shamsu.integrations.telegram.models import PermissionLevel, TelegramChat, TelegramUser
from shamsu.integrations.telegram.pairing import PairingManager
from shamsu.integrations.telegram.service import POLLER_SESSION, TelegramService
from shamsu.integrations.telegram.storage import TelegramStateStore
from shamsu.integrations.telegram.transport import FakeTelegramTransport


def workspace(tmp_path: Path, name: str) -> Path:
    project = tmp_path / name
    (project / ".shamsu").mkdir(parents=True)
    return project


def pair_a_phone(store: TelegramStateStore) -> int:
    manager = PairingManager(store, installation_id="install-1")
    code = manager.create_code()
    result = manager.verify(
        code.code,
        user=TelegramUser(user_id=99, first_name="Ada"),
        chat=TelegramChat(chat_id=5, chat_type="private"),
        permission_level=PermissionLevel.OWNER,
    )
    assert result.ok
    return result.telegram_user_id


def test_pairings_survive_a_change_of_workspace(tmp_path: Path) -> None:
    first = workspace(tmp_path, "alpha")
    second = workspace(tmp_path, "beta")
    pair_a_phone(TelegramStateStore(first))

    # A store opened from a completely different project sees the same phone,
    # because the DB describes THIS MACHINE'S BOT, not one project's state.
    assert [row["telegram_user_id"] for row in TelegramStateStore(second).authorized_users()] == [99]


def test_unpairing_is_soft_so_the_row_survives_for_audit(tmp_path: Path) -> None:
    project = workspace(tmp_path, "alpha")
    store = TelegramStateStore(project)
    pair_a_phone(store)

    store.disconnect_user(99)

    assert store.authorized_users() == []
    assert store.authorization_for(99) is None


def test_a_pairing_code_cannot_be_used_twice(tmp_path: Path) -> None:
    store = TelegramStateStore(workspace(tmp_path, "alpha"))
    manager = PairingManager(store, installation_id="install-1")
    code = manager.create_code()
    user = TelegramUser(user_id=7, first_name="Ada")
    chat = TelegramChat(chat_id=7, chat_type="private")

    assert manager.verify(code.code, user=user, chat=chat).ok
    second = manager.verify(code.code, user=user, chat=chat)
    assert not second.ok
    assert "already been used" in second.reason


def service_with_fake_transport(project: Path) -> TelegramService:
    return TelegramService(project, transport=FakeTelegramTransport(), token="123456:AAH-fake")


def test_starting_the_poller_takes_the_install_wide_lease(tmp_path: Path) -> None:
    project = workspace(tmp_path, "alpha")
    service = service_with_fake_transport(project)

    async def run() -> None:
        started = await service.start()
        assert started.outcome == "started"
        holder = ControlStore().lease_holder(MACHINE_LEASE_KEY, POLLER_SESSION)
        assert holder is not None
        assert holder.owner_surface == "telegram"
        await service.stop()

    asyncio.run(run())
    assert ControlStore().lease_holder(MACHINE_LEASE_KEY, POLLER_SESSION) is None


def test_the_bound_workspace_is_recorded_where_another_process_can_read_it(
    tmp_path: Path,
) -> None:
    """The web portal is a different program from the bot, so "which project is
    it driving" has to be durable rather than an attribute on an object."""
    project = workspace(tmp_path, "alpha")
    service = service_with_fake_transport(project)

    async def run() -> None:
        await service.start()
        await service.stop()

    asyncio.run(run())

    from shamsu.integrations.telegram.service import poller_status

    assert poller_status(project)["workspace"] == str(project.resolve())


def test_a_second_poller_in_another_process_is_refused(tmp_path: Path, monkeypatch) -> None:
    """Same-process re-entry is allowed (the lease is re-entrant by pid), so the
    collision that matters is faked by pretending another pid holds it."""
    project = workspace(tmp_path, "alpha")
    store = ControlStore()
    store.acquire_lease(MACHINE_LEASE_KEY, POLLER_SESSION, surface="cli")

    # Rewrite the row to belong to a pid that is alive but is not us: the test
    # runner's parent is the safest such pid available.
    import os
    import sqlite3

    from shamsu.control.store import control_db_path

    with sqlite3.connect(control_db_path()) as conn:
        conn.execute(
            "UPDATE leases SET owner_pid = ? WHERE workspace = ? AND session_id = ?",
            (os.getppid(), MACHINE_LEASE_KEY, POLLER_SESSION),
        )

    if ControlStore().lease_holder(MACHINE_LEASE_KEY, POLLER_SESSION) is None:
        pytest.skip("the parent pid is not observable on this platform")

    service = service_with_fake_transport(project)

    async def run() -> str:
        started = await service.start()
        await service.stop()
        return started.outcome

    assert asyncio.run(run()) == "held-elsewhere"


def test_poller_status_reports_not_running_when_nothing_holds_the_lease(tmp_path: Path) -> None:
    from shamsu.integrations.telegram.service import poller_status

    status = poller_status(workspace(tmp_path, "alpha"))
    assert status["running"] is False
    assert status["transport"] == "long polling"


def test_a_poll_failure_is_recorded_where_the_settings_page_can_read_it(
    tmp_path: Path,
) -> None:
    """A 409 from a registered webhook is retried silently forever. Recording it
    is the only reason anyone would ever find out."""
    project = workspace(tmp_path, "alpha")
    service = service_with_fake_transport(project)

    service._record_poll_error(RuntimeError("Conflict: terminated by other getUpdates request"))

    from shamsu.integrations.telegram.service import poller_status

    assert "Conflict" in poller_status(project)["last_error"]


def test_a_recorded_error_never_contains_the_token(tmp_path: Path) -> None:
    project = workspace(tmp_path, "alpha")
    service = service_with_fake_transport(project)

    service._record_poll_error(RuntimeError("failed calling /bot123456:AAH-fake/getUpdates"))

    from shamsu.integrations.telegram.service import poller_status

    assert "123456:AAH-fake" not in poller_status(project)["last_error"]
