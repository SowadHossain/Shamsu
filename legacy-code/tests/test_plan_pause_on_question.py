"""Gap J5: a question asked mid-plan was effectively lost.

The pending-question check only ran at the top of the REPL prompt loop, so
`_execute_plan`'s step loop never noticed one. A step that called `ask_user`
was marked "done" anyway, later steps ran on the unanswered assumption, and a
later step could overwrite the question before anyone saw it.

Now: the plan pauses at the asking step, records where to resume, and the
user's answer re-enters execution there.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

from rich.console import Console

import shamsu.cli.repl as repl
from shamsu.session.manager import SessionManager


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


class _Result:
    """Stand-in for AgentLoopResult."""

    def __init__(self, *, awaiting_user: bool = False, changed_files: tuple[str, ...] = ()) -> None:
        self.final = "..."
        self.stopped = awaiting_user
        self.awaiting_user = awaiting_user
        self.changed_files = changed_files


def _run(coro):
    return asyncio.run(coro)


def test_plan_pauses_when_a_step_asks_and_does_not_mark_it_done(tmp_path: Path, monkeypatch):
    logger = SessionManager(tmp_path).create_session("Pause")
    calls: list[str] = []

    async def fake_chat(request, workspace, console, **kwargs):  # noqa: ANN001
        calls.append(request)
        # Step 2 asks the user something.
        return _Result(awaiting_user=len(calls) == 2)

    monkeypatch.setattr(repl, "_run_agent_chat", fake_chat)
    monkeypatch.setattr(repl, "_verify_completed_plan", _noop_verify)

    _run(
        repl._execute_plan(
            "build auth",
            "code_edit",
            "## Steps\n1. a\n2. b\n3. c",
            ["step a", "step b", "step c"],
            tmp_path,
            _console(),
            session_logger=logger,
        )
    )

    # Stopped AT the asking step - step 3 must not have run on a guess.
    assert len(calls) == 2

    pending = logger.get_pending_action()
    assert pending["awaiting"] == "plan_resume"
    assert pending["resume_index"] == 1          # zero-based: step 2
    assert pending["steps"] == ["step a", "step b", "step c"]


def test_paused_plan_resumes_from_the_asking_step(tmp_path: Path, monkeypatch):
    logger = SessionManager(tmp_path).create_session("Resume")
    requests: list[str] = []

    async def fake_chat(request, workspace, console, **kwargs):  # noqa: ANN001
        requests.append(request)
        return _Result()

    monkeypatch.setattr(repl, "_run_agent_chat", fake_chat)
    monkeypatch.setattr(repl, "_verify_completed_plan", _noop_verify)

    paused = {
        "awaiting": "plan_resume",
        "task": "build auth",
        "route": "code_edit",
        "plan_markdown": "## Steps\n1. a\n2. b\n3. c",
        "steps": ["step a", "step b", "step c"],
        "resume_index": 1,
        "changed_files": [],
    }
    _run(repl._resume_paused_plan(paused, "use JWT", tmp_path, _console(), logger))

    # Resumes at step b: the two REMAINING steps run, not all three.
    assert len(requests) == 2
    assert any("use JWT" in request for request in requests)


def test_taking_a_paused_plan_pops_it_once(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Pop")
    logger.set_pending_action({"awaiting": "plan_resume", "steps": ["a"], "resume_index": 0})

    assert repl._take_paused_plan(logger) is not None
    # Popped: a later unrelated answer must not resurrect it.
    assert repl._take_paused_plan(logger) is None


def test_take_paused_plan_ignores_a_plan_awaiting_approval(tmp_path: Path):
    """`/plan` stores awaiting=plan_approval - that is `proceed`'s business,
    not the question-resume path's."""
    logger = SessionManager(tmp_path).create_session("Approval")
    logger.set_pending_action({"awaiting": "plan_approval", "plan_id": "p1"})

    assert repl._take_paused_plan(logger) is None
    assert logger.get_pending_action()["awaiting"] == "plan_approval"


def test_take_paused_plan_survives_no_logger():
    assert repl._take_paused_plan(None) is None


async def _noop_verify(*args, **kwargs):  # noqa: ANN002, ANN003
    return None
