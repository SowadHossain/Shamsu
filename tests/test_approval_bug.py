"""The approval prompt, and four ways it went wrong at once.

From the session the user pasted on 2026-08-22. One `run_command` approval
produced:

    ╭─ Approval Required ─╮   (the shared "answer from anywhere" prompt)
    ╭─ Approval Required ─╮   (the local fallback, because the shared one threw)
    Do you want to proceed?
      [y] Allow once
      [n] Deny
    approval.py: RuntimeWarning: coroutine 'Application.run_async' was never awaited
    Press y to allow once, a to always allow when offered, or n to deny.
    ╭─ Approval Required ─╮   (the watcher, announcing our own question back)
    /approve approval-5b3… or /deny approval-5b3…
    repl.py: RuntimeWarning: coroutine 'ask_here_or_anywhere' was never awaited

Three panels, two leaked coroutines, a menu offering two keys under a hint
offering three - and `a`, the key the hint told you to press, meant Deny.
"""
from __future__ import annotations

import asyncio
import gc
import warnings

import pytest
from rich.console import Console

from shamsu.control.console import (
    ApprovalWatcher,
    forget_raised_here,
    mark_raised_here,
    raised_here,
    run_coroutine_blocking,
)
from shamsu.safety.approval import ask_approval_menu
from shamsu.types import ApprovalRequest


def _request(action_type: str = "run_command") -> ApprovalRequest:
    return ApprovalRequest(
        action_type=action_type,
        description="node -c js/main.js",
        risk_level="medium",
    )


# -- `a` must never mean deny ---------------------------------------------


def test_pressing_a_allows_even_when_remember_was_not_offered(monkeypatch):
    """The reported bug, and the worst of the four. The single-key reader
    prints "Press y to allow once, a to always allow when offered, or n to
    deny" and accepts `a` - unconditionally, both of them. The menu then only
    looked at `a` when `offer_remember` was True and fell through to the
    catch-all otherwise, so the key the user was TOLD to press silently refused
    the action. 20 of 22 `node --check` calls denied in one live session, every
    one of them deliberately allowed by the person sitting there."""
    import shamsu.safety.approval as approval

    monkeypatch.setattr(approval, "_read_approval_answer", lambda *a, **k: "a")

    approved, scope = ask_approval_menu(
        _request(), offer_remember=False, console=Console(file=open("nul", "w"))
    )

    assert approved is True
    assert scope == "none", "nothing was offered, so nothing may be remembered"


def test_pressing_a_when_remember_is_offered_remembers(monkeypatch):
    import shamsu.safety.approval as approval

    monkeypatch.setattr(approval, "_read_approval_answer", lambda *a, **k: "a")

    approved, scope = ask_approval_menu(
        _request("file_write"), offer_remember=True, console=Console(file=open("nul", "w"))
    )

    assert (approved, scope) == (True, "workspace")


def test_n_and_nonsense_still_deny(monkeypatch):
    """The safe default has to survive the fix."""
    import shamsu.safety.approval as approval

    for answer in ("n", "no", "q", ""):
        monkeypatch.setattr(approval, "_read_approval_answer", lambda *a, **k: answer)
        approved, _ = ask_approval_menu(
            _request(), console=Console(file=open("nul", "w"))
        )
        assert approved is False, answer


def test_the_key_hint_only_names_keys_that_are_on_offer(monkeypatch):
    """The menu printed two options and the hint underneath printed three."""
    import shamsu.safety.approval as approval

    console = Console(record=True, width=100, force_terminal=True)
    monkeypatch.setattr(approval.sys, "platform", "win32")
    monkeypatch.setattr(approval, "msvcrt", None, raising=False)

    # Only the hint line matters here, so drive it directly.
    printed: list[str] = []
    monkeypatch.setattr(
        approval, "_read_approval_answer", lambda c, **k: printed.append(k) or "y"
    )
    ask_approval_menu(_request(), offer_remember=False, console=console)

    assert printed == [{"offer_remember": False}], "the reader was not told what is on offer"


# -- the leaked coroutines ------------------------------------------------


def test_running_a_coroutine_from_a_thread_a_loop_owns_does_not_leak():
    """`asyncio.run(coro())` builds the coroutine as an ARGUMENT and only then
    discovers a loop is running here, so it raises without ever awaiting what
    it just made. That is both RuntimeWarnings in the reported session."""

    async def work() -> str:
        return "answered"

    async def main() -> str:
        # Exactly the shape in the REPL: sync code, on a thread that a loop
        # already owns, needing an async answer.
        return run_coroutine_blocking(work)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert asyncio.run(main()) == "answered"
        gc.collect()

    leaked = [w for w in caught if "never awaited" in str(w.message)]
    assert not leaked, [str(w.message) for w in leaked]


def test_it_still_works_where_no_loop_is_running():
    async def work() -> str:
        return "answered"

    assert run_coroutine_blocking(work) == "answered"


def test_the_error_from_inside_reaches_the_caller():
    """The caller catches exceptions to fall back to the local prompt. It can
    only do that if the exception actually arrives."""

    async def work() -> str:
        raise ValueError("no store")

    async def main():
        return run_coroutine_blocking(work)

    with pytest.raises(ValueError, match="no store"):
        asyncio.run(main())


# -- one question, one panel ----------------------------------------------


def test_the_fallback_does_not_draw_the_question_a_second_time():
    console = Console(record=True, width=100)
    import shamsu.safety.approval as approval

    original = approval._read_approval_answer
    approval._read_approval_answer = lambda *a, **k: "n"
    try:
        ask_approval_menu(_request(), console=console, render=False)
    finally:
        approval._read_approval_answer = original

    printed = console.export_text()
    assert "Approval Required" not in printed
    assert "Do you want to proceed?" in printed, "it must still ASK"


def test_the_watcher_does_not_announce_this_processs_own_question():
    """The watcher exists to surface questions from OTHER processes. It swept
    the store and found ours, so the same `run_command` was drawn again under a
    `/approve <id>` the user did not need - beside the prompt already waiting
    for their keypress."""

    class _Record:
        approval_id = "approval-abc"
        description = "node -c js/main.js"
        risk_level = "medium"
        preview = ""
        workspace = "asteroid-shamsu"
        decided_by = ""

    class _Store:
        def pending_approvals(self):
            return [_Record()]

        def approval(self, _id):
            return _Record()

    console = Console(record=True, width=100)
    watcher = ApprovalWatcher(_Store(), console)

    mark_raised_here("approval-abc")
    try:
        watcher._sweep()
        assert console.export_text().strip() == ""
        assert raised_here("approval-abc")
    finally:
        forget_raised_here("approval-abc")

    # ...and a question from somewhere else is still announced.
    watcher._sweep()
    printed = console.export_text()
    assert "Approval Required" in printed
    assert "/approve approval-abc" in printed


def test_a_resolution_with_no_surface_still_reads_as_a_sentence():
    """`Approval resolved on .` - the record had no `decided_by`."""

    class _Record:
        approval_id = "approval-abc"
        description = "x"
        risk_level = ""
        preview = ""
        workspace = "w"
        decided_by = ""

    class _Store:
        def __init__(self):
            self.live = [_Record()]

        def pending_approvals(self):
            return list(self.live)

        def approval(self, _id):
            return _Record()

    console = Console(record=True, width=100)
    store = _Store()
    watcher = ApprovalWatcher(store, console)
    watcher._sweep()
    store.live = []
    watcher._sweep()

    printed = console.export_text()
    assert "Approval resolved on ." not in printed
    assert "another surface" in printed
