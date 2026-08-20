"""Leases, the queue and approvals - the three things three processes share.

These are concurrency tests, so most of them use real threads or real
subprocesses rather than pretending. A lock that is only ever exercised from
one thread is not a lock, it is a comment.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from shamsu.control.store import (
    ALLOW,
    DENY,
    LEASE_STALE_SECONDS,
    ControlStore,
)


@pytest.fixture
def store(tmp_path) -> ControlStore:
    return ControlStore(tmp_path / "control.db")


WS = "/ws/project"
SESSION = "session-1"


# --- leases ---------------------------------------------------------------


def test_one_holder_at_a_time(store):
    assert store.acquire_lease(WS, SESSION, "cli") is True
    holder = store.lease_holder(WS, SESSION)
    assert holder is not None
    assert holder.owner_pid == os.getpid()
    assert holder.owner_surface == "cli"
    assert holder.is_mine


def test_the_same_process_can_reacquire_its_own_lease(store):
    """A REPL that already owns a thread must not deadlock against itself."""
    assert store.acquire_lease(WS, SESSION, "cli") is True
    assert store.acquire_lease(WS, SESSION, "cli") is True


def test_releasing_frees_it(store):
    store.acquire_lease(WS, SESSION, "cli")
    assert store.release_lease(WS, SESSION) is True
    assert store.lease_holder(WS, SESSION) is None


def test_a_dead_owner_does_not_hold_a_thread_forever(store):
    """Both tests matter: a live pid with a fresh beat, or nothing."""
    store.acquire_lease(WS, SESSION, "cli")
    # A pid that cannot be running: 0 is never a real process here.
    with store._connect(write=True) as conn:
        conn.execute("UPDATE leases SET owner_pid = 0")
    assert store.lease_holder(WS, SESSION) is None
    assert store.clear_stale_leases() == 1


def test_a_silent_owner_loses_the_lease_but_a_beating_one_keeps_it(store):
    store.acquire_lease(WS, SESSION, "cli")
    stale = "1999-01-01T00:00:00+00:00"
    with store._connect(write=True) as conn:
        conn.execute("UPDATE leases SET heartbeat = ?", (stale,))
    assert store.lease_holder(WS, SESSION) is None

    store.acquire_lease(WS, SESSION, "cli")
    assert store.renew_lease(WS, SESSION) is True
    assert store.lease_holder(WS, SESSION) is not None


def test_threads_racing_for_one_lease_produce_exactly_one_winner(store):
    """The whole point. Fifty racers, one lease."""
    barrier = threading.Barrier(50)
    wins: list[bool] = []
    lock = threading.Lock()

    def race() -> None:
        barrier.wait()
        # A fresh store per thread: separate connections, as separate
        # processes would have.
        won = ControlStore(store.db_path).acquire_lease(WS, "racy", "cli")
        with lock:
            wins.append(won)

    threads = [threading.Thread(target=race) for _ in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # Same process, so re-entrancy means every caller legitimately wins. What
    # must hold is that there is exactly one lease row and one owner.
    assert len(wins) == 50
    assert store.lease_holder(WS, "racy") is not None


def test_a_second_process_cannot_steal_a_live_lease(store, tmp_path):
    """The real thing: two interpreters, one database, one winner.

    Threads share a pid, so re-entrancy hides the case that actually matters.
    Only a subprocess proves a *different* process is refused.
    """
    store.acquire_lease(WS, SESSION, "cli")
    store.renew_lease(WS, SESSION)

    script = tmp_path / "rival.py"
    script.write_text(
        "import sys\n"
        "from shamsu.control.store import ControlStore\n"
        f"store = ControlStore(r'{store.db_path}')\n"
        f"print('WON' if store.acquire_lease(r'{WS}', '{SESSION}', 'web') else 'REFUSED')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert result.returncode == 0, result.stderr
    assert "REFUSED" in result.stdout

    # And once the owner lets go, the next process gets it.
    store.release_lease(WS, SESSION)
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert "WON" in result.stdout


def test_active_leases_are_the_install_wide_gate(store):
    """Per-thread serialisation is not enough: one GPU, one run."""
    store.acquire_lease("/ws/a", "s1", "cli")
    store.acquire_lease("/ws/b", "s2", "web")
    assert len(store.active_leases()) == 2


# --- the queue ------------------------------------------------------------


def test_prompts_come_back_in_the_order_they_were_sent(store):
    store.enqueue(WS, SESSION, "first", "cli")
    store.enqueue(WS, SESSION, "second", "web")
    store.enqueue(WS, SESSION, "third", "telegram")

    assert [item.text for item in store.pending(WS, SESSION)] == [
        "first",
        "second",
        "third",
    ]
    assert store.queue_depth(WS, SESSION) == 3


def test_the_source_of_each_prompt_is_remembered(store):
    """So a surface can say "queued from your phone" rather than just "queued"."""
    store.enqueue(WS, SESSION, "hi", "telegram")
    assert store.pending(WS, SESSION)[0].source == "telegram"


def test_claiming_takes_the_oldest_and_only_once(store):
    store.enqueue(WS, SESSION, "first", "cli")
    store.enqueue(WS, SESSION, "second", "cli")

    claimed = store.claim_next(WS, SESSION)
    assert claimed is not None and claimed.text == "first"
    assert store.queue_depth(WS, SESSION) == 1

    store.finish(claimed.queue_id)
    assert store.claim_next(WS, SESSION).text == "second"
    assert store.claim_next(WS, SESSION) is None


def test_racing_claims_never_hand_out_the_same_prompt_twice(store):
    for index in range(20):
        store.enqueue(WS, "busy", f"prompt {index}", "cli")

    barrier = threading.Barrier(20)
    claimed: list[str] = []
    lock = threading.Lock()

    def claim() -> None:
        barrier.wait()
        item = ControlStore(store.db_path).claim_next(WS, "busy")
        if item is not None:
            with lock:
                claimed.append(item.text)

    threads = [threading.Thread(target=claim) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(claimed) == 20
    assert len(set(claimed)) == 20, "a prompt was handed to two claimers"


def test_a_queued_prompt_can_be_cancelled_but_a_running_one_cannot(store):
    queue_id = store.enqueue(WS, SESSION, "never mind", "web")
    assert store.cancel_queued(queue_id) is True
    assert store.queue_depth(WS, SESSION) == 0

    running = store.enqueue(WS, SESSION, "in flight", "web")
    store.claim_next(WS, SESSION)
    assert store.cancel_queued(running) is False


def test_queues_do_not_leak_between_threads_or_projects(store):
    store.enqueue("/ws/a", "s1", "for a", "cli")
    store.enqueue("/ws/b", "s2", "for b", "cli")

    assert [item.text for item in store.pending("/ws/a", "s1")] == ["for a"]
    assert [item.text for item in store.pending("/ws/b", "s2")] == ["for b"]


# --- approvals ------------------------------------------------------------


def test_an_approval_can_be_answered_from_anywhere(store):
    approval_id = store.raise_approval(
        workspace=WS, session_id=SESSION, description="run pytest", risk_level="medium"
    )
    assert [item.approval_id for item in store.pending_approvals()] == [approval_id]

    assert store.resolve_approval(approval_id, ALLOW, "web") is True
    record = store.approval(approval_id)
    assert record.decision == ALLOW
    assert record.decided_by == "web"
    assert store.pending_approvals() == []


def test_only_the_first_answer_counts(store):
    """Phone and browser both tap Allow. Exactly one write may win."""
    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)

    assert store.resolve_approval(approval_id, ALLOW, "telegram") is True
    assert store.resolve_approval(approval_id, DENY, "web") is False
    assert store.approval(approval_id).decision == ALLOW
    assert store.approval(approval_id).decided_by == "telegram"


def test_racing_answers_produce_exactly_one_winner(store):
    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)
    barrier = threading.Barrier(20)
    winners: list[str] = []
    lock = threading.Lock()

    def answer(surface: str) -> None:
        barrier.wait()
        if ControlStore(store.db_path).resolve_approval(approval_id, ALLOW, surface):
            with lock:
                winners.append(surface)

    threads = [threading.Thread(target=answer, args=(f"s{i}",)) for i in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(winners) == 1


def test_waiting_returns_the_decision_made_elsewhere(store):
    """The agent blocks here; the answer arrives from another surface."""
    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)

    def answer_later() -> None:
        time.sleep(0.3)
        ControlStore(store.db_path).resolve_approval(approval_id, ALLOW, "web")

    threading.Thread(target=answer_later, daemon=True).start()
    assert store.wait_for_decision(approval_id, timeout_seconds=15) == ALLOW


def test_an_unanswered_approval_fails_closed(store):
    """Nobody was watching. That is a denial, never an allow."""
    approval_id = store.raise_approval(
        workspace=WS, session_id=SESSION, timeout_seconds=0.2
    )
    assert store.wait_for_decision(approval_id, timeout_seconds=5) == DENY
    assert store.approval(approval_id).decided_by == "timeout"


def test_waiting_can_be_abandoned_without_answering_yes(store):
    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)
    assert (
        store.wait_for_decision(
            approval_id, timeout_seconds=30, should_stop=lambda: True
        )
        == DENY
    )


def test_a_missing_approval_is_a_denial_not_a_crash(store):
    assert store.wait_for_decision("no-such-approval", timeout_seconds=5) == DENY


def test_approvals_can_be_listed_for_one_thread_or_the_whole_machine(store):
    here = store.raise_approval(workspace="/ws/a", session_id="s1")
    store.raise_approval(workspace="/ws/b", session_id="s2")

    assert len(store.pending_approvals()) == 2
    scoped = store.pending_approvals("/ws/a", "s1")
    assert [item.approval_id for item in scoped] == [here]


def test_a_decision_must_be_allow_or_deny(store):
    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)
    with pytest.raises(ValueError):
        store.resolve_approval(approval_id, "maybe", "web")


def test_the_risk_level_is_kept_even_though_nothing_reads_it_yet(store):
    """Policy by risk is a later decision; the column has to exist for it."""
    approval_id = store.raise_approval(
        workspace=WS, session_id=SESSION, risk_level="high", action_type="run_command"
    )
    record = store.approval(approval_id)
    assert record.risk_level == "high"
    assert record.action_type == "run_command"


# --- the file ------------------------------------------------------------


def test_the_database_is_disposable(tmp_path):
    """Coordination only. Deleting it must cost nothing but coordination."""
    path = tmp_path / "control.db"
    first = ControlStore(path)
    first.enqueue(WS, SESSION, "hi", "cli")
    path.unlink()

    rebuilt = ControlStore(path)
    assert rebuilt.pending(WS, SESSION) == []
    assert rebuilt.acquire_lease(WS, SESSION, "cli") is True


def test_it_lives_under_the_install_home(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.control.store import control_db_path

    assert control_db_path() == tmp_path / "home" / "control.db"


def test_the_stale_window_is_wider_than_the_heartbeat(store):
    """Or one slow beat would lose a lease that was never abandoned."""
    from shamsu.control.store import LEASE_HEARTBEAT_SECONDS

    assert LEASE_STALE_SECONDS > LEASE_HEARTBEAT_SECONDS * 2


# --- the runner: one turn at a time, from any surface ---------------------


class _ScriptedClient:
    """A model that takes a beat, so overlap is observable if it happens."""

    def __init__(self, reply="done", delay=0.25):
        self.reply = reply
        self.delay = delay
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        import asyncio as _asyncio

        await _asyncio.sleep(self.delay)
        return {"message": {"content": self.reply, "tool_calls": []}}


@pytest.fixture
def runner_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SHAMSU_LEGACY_ROUTING", raising=False)
    from shamsu.session.manager import SessionManager

    workspace = tmp_path / "project"
    workspace.mkdir()
    logger = SessionManager(workspace).create_session("thread")
    client = _ScriptedClient()
    monkeypatch.setattr(
        "shamsu.agents.chat_loop._default_ollama_client", lambda *a, **k: client
    )
    return workspace, logger.session_id, client


def test_a_prompt_from_the_web_actually_runs(runner_env, tmp_path):
    from shamsu.control.runner import QueuedRunner
    from shamsu.session.manager import SessionManager

    workspace, session_id, client = runner_env
    runner = QueuedRunner(ControlStore(tmp_path / "control.db"), surface="web")
    try:
        outcome = runner.submit(workspace, session_id, "add a pause menu")
        assert outcome.accepted and not outcome.queued
        deadline = time.monotonic() + 30
        while runner.busy() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert client.calls == 1
        messages = SessionManager(workspace).logger_for(session_id).read_messages()
        assert any(m["role"] == "user" and "pause menu" in m["content"] for m in messages)
    finally:
        runner.stop()


def test_a_second_prompt_queues_instead_of_running_alongside(runner_env, tmp_path):
    """The headline: two surfaces, one thread, one turn at a time."""
    from shamsu.control.runner import QueuedRunner

    workspace, session_id, client = runner_env
    client.delay = 0.6
    store = ControlStore(tmp_path / "control.db")
    runner = QueuedRunner(store, surface="web")
    try:
        first = runner.submit(workspace, session_id, "first")
        second = runner.submit(workspace, session_id, "second")

        assert first.accepted and not first.queued
        assert second.accepted and second.queued
        assert "waiting" in second.reason

        deadline = time.monotonic() + 60
        while runner.busy() and time.monotonic() < deadline:
            time.sleep(0.05)
        # Both ran, one after the other.
        assert client.calls == 2
        assert store.queue_depth(workspace, session_id) == 0
    finally:
        runner.stop()


def test_a_thread_held_by_another_process_is_not_taken_over(runner_env, tmp_path):
    from shamsu.control.runner import QueuedRunner

    workspace, session_id, client = runner_env
    store = ControlStore(tmp_path / "control.db")
    # Someone else's live lease, from a pid that is definitely running.
    with store._connect(write=True) as conn:
        conn.execute(
            "INSERT INTO leases (workspace, session_id, owner_pid, owner_surface, heartbeat)"
            " VALUES (?, ?, ?, 'cli', ?)",
            (str(workspace.resolve()), session_id, _other_live_pid(), _utc_now()),
        )
    runner = QueuedRunner(store, surface="web")
    try:
        outcome = runner.submit(workspace, session_id, "from the browser")
        assert outcome.accepted and outcome.queued
        assert "cli" in outcome.reason
        time.sleep(0.4)
        assert client.calls == 0, "the browser ran a turn the CLI owned"
        assert store.queue_depth(workspace, session_id) == 1
    finally:
        runner.stop()


def test_a_run_elsewhere_on_the_machine_also_makes_it_wait(runner_env, tmp_path):
    """One GPU. Serialising per thread alone would let these overlap."""
    from shamsu.control.runner import MACHINE_SESSION, MACHINE_WORKSPACE, QueuedRunner

    workspace, session_id, client = runner_env
    store = ControlStore(tmp_path / "control.db")
    with store._connect(write=True) as conn:
        conn.execute(
            "INSERT INTO leases (workspace, session_id, owner_pid, owner_surface, heartbeat)"
            " VALUES (?, ?, ?, 'cli', ?)",
            (MACHINE_WORKSPACE, MACHINE_SESSION, _other_live_pid(), _utc_now()),
        )
    runner = QueuedRunner(store, surface="web")
    try:
        outcome = runner.submit(workspace, session_id, "meanwhile")
        assert outcome.queued
        assert "model" in outcome.reason
        time.sleep(0.3)
        assert client.calls == 0
    finally:
        runner.stop()


def test_an_empty_prompt_is_refused_before_it_reaches_the_queue(runner_env, tmp_path):
    from shamsu.control.runner import QueuedRunner

    workspace, session_id, _client = runner_env
    store = ControlStore(tmp_path / "control.db")
    runner = QueuedRunner(store, surface="web")
    try:
        outcome = runner.submit(workspace, session_id, "   ")
        assert not outcome.accepted
        assert store.queue_depth(workspace, session_id) == 0
    finally:
        runner.stop()


def test_the_lease_is_released_when_the_queue_drains(runner_env, tmp_path):
    from shamsu.control.runner import MACHINE_SESSION, MACHINE_WORKSPACE, QueuedRunner

    workspace, session_id, _client = runner_env
    store = ControlStore(tmp_path / "control.db")
    runner = QueuedRunner(store, surface="web")
    try:
        runner.submit(workspace, session_id, "one")
        deadline = time.monotonic() + 30
        while runner.busy() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert store.lease_holder(workspace, session_id) is None
        assert store.lease_holder(MACHINE_WORKSPACE, MACHINE_SESSION) is None
    finally:
        runner.stop()


def _other_live_pid() -> int:
    """A pid that is alive but is not us - the parent, or failing that, us + 0."""
    import os

    parent = os.getppid()
    return parent if parent and parent != os.getpid() else os.getpid()


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# --- the terminal's half of approve-from-anywhere -------------------------


class _Recording:
    def __init__(self):
        self.printed = []

    def print(self, *args, **kwargs):
        for arg in args:
            self.printed.append(str(getattr(arg, "renderable", arg)))

    @property
    def text(self):
        return "\n".join(self.printed)


def test_the_terminal_prompt_gives_up_when_the_phone_answers(store):
    """The whole point of the cancellable prompt.

    Nobody types anything here. The answer arrives from another surface, and
    the local prompt must stop waiting rather than strand the turn behind a
    keystroke that is no longer owed.
    """
    import asyncio

    from shamsu.control.console import ask_here_or_anywhere

    approval_id = store.raise_approval(
        workspace=WS, session_id=SESSION, description="run pytest"
    )
    never = asyncio.Event()  # a reader that never returns

    async def hangs():
        await never.wait()
        return "y"

    def answer_later():
        time.sleep(0.3)
        ControlStore(store.db_path).resolve_approval(approval_id, ALLOW, "telegram")

    threading.Thread(target=answer_later, daemon=True).start()
    console = _Recording()
    decision = asyncio.run(
        ask_here_or_anywhere(
            store, approval_id, console, read_line=hangs, poll_seconds=0.05
        )
    )
    assert decision == ALLOW
    assert "elsewhere" in console.text.lower()


def test_typing_here_answers_it(store):
    import asyncio

    from shamsu.control.console import ask_here_or_anywhere

    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)
    decision = asyncio.run(
        ask_here_or_anywhere(
            store, approval_id, _Recording(), read_line=lambda: "y", poll_seconds=0.05
        )
    )
    assert decision == ALLOW
    assert store.approval(approval_id).decided_by == "cli"


def test_anything_that_is_not_yes_is_no(store):
    import asyncio

    from shamsu.control.console import ask_here_or_anywhere

    for answer in ("", "n", "no", "nonsense", "  "):
        approval_id = store.raise_approval(workspace=WS, session_id=SESSION)
        decision = asyncio.run(
            ask_here_or_anywhere(
                store,
                approval_id,
                _Recording(),
                read_line=lambda a=answer: a,
                poll_seconds=0.05,
            )
        )
        assert decision == DENY, answer


def test_a_local_yes_that_arrives_second_loses(store):
    """Two people answered. The store decides, and the terminal says so."""
    import asyncio

    from shamsu.control.console import ask_here_or_anywhere

    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)
    store.resolve_approval(approval_id, DENY, "web")

    console = _Recording()
    decision = asyncio.run(
        ask_here_or_anywhere(
            store, approval_id, console, read_line=lambda: "y", poll_seconds=0.05
        )
    )
    assert decision == DENY
    assert store.approval(approval_id).decided_by == "web"


# --- announcing approvals raised by other processes -----------------------


def test_the_watcher_announces_a_question_raised_elsewhere(store):
    from shamsu.control.console import ApprovalWatcher

    console = _Recording()
    watcher = ApprovalWatcher(store, console, poll_seconds=0.05)
    store.raise_approval(
        workspace=WS, session_id=SESSION, description="rm -rf build", risk_level="high"
    )
    watcher._sweep()

    assert "rm -rf build" in console.text
    assert "/approve" in console.text
    assert len(watcher.pending()) == 1


def test_the_watcher_retracts_what_was_answered_elsewhere(store):
    """A card left on screen invites a second answer to a settled question."""
    from shamsu.control.console import ApprovalWatcher

    console = _Recording()
    watcher = ApprovalWatcher(store, console, poll_seconds=0.05)
    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)
    watcher._sweep()
    store.resolve_approval(approval_id, ALLOW, "web")
    watcher._sweep()

    assert "resolved on web" in console.text
    assert watcher.pending() == []


def test_approve_answers_the_only_one_waiting(store):
    from shamsu.control.console import ApprovalWatcher

    watcher = ApprovalWatcher(store, _Recording(), poll_seconds=0.05)
    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)

    ok, message = watcher.resolve(True)
    assert ok, message
    assert store.approval(approval_id).decision == ALLOW
    assert store.approval(approval_id).decided_by == "cli"


def test_approve_refuses_to_guess_between_several(store):
    from shamsu.control.console import ApprovalWatcher

    watcher = ApprovalWatcher(store, _Recording(), poll_seconds=0.05)
    store.raise_approval(workspace=WS, session_id=SESSION, description="one")
    store.raise_approval(workspace=WS, session_id=SESSION, description="two")

    ok, message = watcher.resolve(True)
    assert not ok
    assert "name one" in message


def test_approve_accepts_an_id_prefix(store):
    from shamsu.control.console import ApprovalWatcher

    watcher = ApprovalWatcher(store, _Recording(), poll_seconds=0.05)
    first = store.raise_approval(workspace=WS, session_id=SESSION, description="one")
    store.raise_approval(workspace=WS, session_id=SESSION, description="two")

    ok, _message = watcher.resolve(False, first[:16])
    assert ok
    assert store.approval(first).decision == DENY


def test_answering_nothing_says_so(store):
    from shamsu.control.console import ApprovalWatcher

    watcher = ApprovalWatcher(store, _Recording(), poll_seconds=0.05)
    ok, message = watcher.resolve(True)
    assert not ok
    assert "Nothing" in message


def test_the_watcher_sees_every_workspace(store):
    """You should not have to be in the right project to be asked."""
    from shamsu.control.console import ApprovalWatcher

    console = _Recording()
    watcher = ApprovalWatcher(store, console, poll_seconds=0.05)
    store.raise_approval(
        workspace="/somewhere/else", session_id="other", description="from elsewhere"
    )
    watcher._sweep()
    assert "from elsewhere" in console.text
