"""The approval bugs found live on 2026-08-24, in the openbazar workspace.

Three phantom `Approval resolved on another surface.` lines appeared in a
terminal whose user had answered nothing. The store held the evidence: 131 of
176 rows sat at `decision = ''` for ever, because the only call to
`resolve_approval` was on the happy path. Fifteen minutes after each was
raised it fell off the pending list unstamped, and the watcher read
"disappeared" as "answered".

Four defects, one per section below.
"""
from __future__ import annotations

import pytest

from shamsu.control.store import ALLOW, DENY, ControlStore

WS = "/tmp/project"
SESSION = "session-1"


@pytest.fixture()
def store(tmp_path):
    return ControlStore(tmp_path / "control.db")


class _Recording:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def print(self, *args, **_kwargs) -> None:
        self.lines.append(" ".join(str(item) for item in args))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


# -- 1. expiry has to WRITE a decision -------------------------------------


def test_expire_approvals_actually_expires_something(store):
    """It walked `pending_approvals()`, which filters expired rows OUT.

    So the loop could never find one to expire, and nothing called it anyway.
    """
    approval_id = store.raise_approval(
        workspace=WS, session_id=SESSION, timeout_seconds=-1
    )
    assert store.expire_approvals() == 1

    record = store.approval(approval_id)
    assert record.decision == DENY, "an unanswered approval must fail closed"
    assert record.decided_by == "timeout", "and must say so, not stay blank"


def test_expiring_twice_does_not_double_count(store):
    store.raise_approval(workspace=WS, session_id=SESSION, timeout_seconds=-1)
    assert store.expire_approvals() == 1
    assert store.expire_approvals() == 0


def test_expiry_never_touches_a_live_question(store):
    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)
    assert store.expire_approvals() == 0
    assert store.approval(approval_id).decision == ""


# -- 2. the watcher must not report a timeout as somebody's answer ---------


def test_a_timed_out_approval_is_not_announced_as_a_decision(store):
    from shamsu.control.console import ApprovalWatcher

    console = _Recording()
    watcher = ApprovalWatcher(store, console, poll_seconds=0.05)
    store.raise_approval(
        workspace=WS, session_id=SESSION, description="npm install", timeout_seconds=60
    )
    watcher._sweep()

    # Time passes and nobody answers.
    with store._connect(write=True) as conn:
        conn.execute("UPDATE approvals SET expires_at = '2000-01-01T00:00:00+00:00'")
    watcher._sweep()

    assert "expired unanswered" in console.text
    assert "another surface" not in console.text, "nobody was on another surface"


def test_a_real_remote_answer_still_names_its_surface(store):
    from shamsu.control.console import ApprovalWatcher

    console = _Recording()
    watcher = ApprovalWatcher(store, console, poll_seconds=0.05)
    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)
    watcher._sweep()
    store.resolve_approval(approval_id, ALLOW, "telegram")
    watcher._sweep()

    assert "allowed on telegram" in console.text


def test_a_question_inherited_from_a_dead_run_says_so(store):
    """Three cards from a closed session read as three live questions."""
    from shamsu.control.console import ApprovalWatcher

    from rich.console import Console

    console = Console(record=True, width=100)
    store.raise_approval(workspace=WS, session_id="an-older-run", description="old")
    watcher = ApprovalWatcher(store, console, poll_seconds=0.05)
    watcher._sweep()

    printed = console.export_text()
    assert "old" in printed, "it is still shown - it might be real"
    assert "before this session opened" in printed


# -- 3. the bridge must close the row on EVERY exit ------------------------


def _bridge(tmp_path, monkeypatch, store, *, answer):
    """The real `_shared_console_approval`, with only the asking stubbed."""
    from shamsu.cli import repl
    from shamsu.control import store as store_module

    monkeypatch.setattr(store_module, "ControlStore", lambda *_a, **_k: store)
    monkeypatch.setattr(repl, "_ask_approval_somewhere", answer)
    return repl._shared_console_approval(tmp_path, None, _Recording())


class _Request:
    action_type = "run_command"
    description = "node --check app.js"
    risk_level = "medium"
    preview = ""


def test_a_raised_approval_is_closed_even_when_the_prompt_falls_back(
    tmp_path, monkeypatch, store
):
    """The fallback answers on the console and never touched the store."""

    def answer(_store, _approval_id, _request, _console):
        return ALLOW  # as the local menu would, bypassing `resolve_approval`

    ask = _bridge(tmp_path, monkeypatch, store, answer=answer)
    assert ask(_Request()) is True

    rows = [item for item in _all(store) if item.decision == ""]
    assert rows == [], "an orphan row is a phantom 'resolved' message in 15 minutes"
    assert _all(store)[0].decision == ALLOW


def test_a_cancelled_turn_closes_the_row_as_a_denial(tmp_path, monkeypatch, store):
    """Ctrl+C raises CancelledError - a BaseException the old code let past."""
    import asyncio

    def answer(_store, _approval_id, _request, _console):
        raise asyncio.CancelledError

    ask = _bridge(tmp_path, monkeypatch, store, answer=answer)
    with pytest.raises(asyncio.CancelledError):
        ask(_Request())

    record = _all(store)[0]
    assert record.decision == DENY, "an abandoned question must fail closed"


def test_the_bridge_does_not_overwrite_an_answer_from_the_phone(
    tmp_path, monkeypatch, store
):
    """The finally must be a no-op when a remote surface already won."""

    def answer(inner_store, approval_id, _request, _console):
        inner_store.resolve_approval(approval_id, ALLOW, "telegram")
        return ALLOW

    ask = _bridge(tmp_path, monkeypatch, store, answer=answer)
    assert ask(_Request()) is True

    record = _all(store)[0]
    assert record.decided_by == "telegram", "the surface that won keeps the credit"


def _all(store):
    with store._connect() as conn:
        rows = conn.execute("SELECT * FROM approvals ORDER BY created_at").fetchall()
    from shamsu.control.store import _approval

    return [_approval(row) for row in rows]


# -- 4. inside a frame, nothing hands the terminal away --------------------


def test_a_framed_approval_never_stands_the_frame_down(tmp_path, monkeypatch, store):
    """The user's report: the approval ejected them into the plain CLI.

    `reading_input()` is what fires `LiveConsole.stand_down` -> `_suspend_frame`
    -> `run_in_terminal`, which drops the alternate screen. On the framed path
    it must not be entered at all.
    """
    from shamsu.cli import repl
    from shamsu.control import console as control_console
    from shamsu.safety import approval as approval_module

    class _App:
        def __init__(self):
            self.opened = []
            self.closed = 0

        def open_approval(self, record, **_kw):
            self.opened.append(record)

        def close_approval(self, note=""):
            self.closed += 1

        def await_approval(self, timeout=None):
            return "y"

    class _Frame:
        def __init__(self):
            self.app = _App()

    frame = _Frame()
    stood_down = []
    monkeypatch.setattr(repl, "active_frame", lambda: frame)
    monkeypatch.setattr(
        approval_module, "reading_input", lambda: stood_down.append("suspended")
    )
    monkeypatch.setattr(
        control_console, "render_request", lambda *_a, **_k: stood_down.append("panel")
    )

    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)
    decision = repl._ask_approval_somewhere(store, approval_id, _Request(), _Recording())

    assert decision == ALLOW
    assert stood_down == [], "the frame kept the terminal, and drew its own card"
    assert frame.app.opened, "the question went into the pane"
    assert frame.app.closed == 1, "and the keyboard was given back exactly once"


def test_a_framed_approval_still_loses_to_the_phone(tmp_path, monkeypatch, store):
    """Answer-from-anywhere must survive the move into the frame."""
    from shamsu.cli import repl

    approval_id = store.raise_approval(workspace=WS, session_id=SESSION)
    store.resolve_approval(approval_id, DENY, "telegram")

    class _App:
        def __init__(self):
            self.closed = 0

        def open_approval(self, _record, **_kw):
            return None

        def close_approval(self, note=""):
            self.closed += 1

        def await_approval(self, timeout=None):
            # Never answered here. Only the store's watcher can end this.
            import time

            time.sleep(5)
            return ""

    class _Frame:
        app = _App()

    frame = _Frame()
    monkeypatch.setattr(repl, "active_frame", lambda: frame)

    assert repl._ask_approval_somewhere(
        store, approval_id, _Request(), _Recording()
    ) == DENY
    assert frame.app.closed == 1, "the parked reader thread was released"
