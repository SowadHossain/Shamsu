"""Wiring: a request, a session, and a display that watches it.

The interface observes through `RunController.subscribe` and drives nothing.
That direction is deliberate — the runtime owns the loop, and a UI that could
step the agent would be a second place where control flow lives.

Two paths, one code path for the work:

* **TUI** — a full-screen display repainting while the run proceeds.
* **Plain** — one line per event, no escape codes, for pipes and CI.

Both subscribe to the same events. The plain path is not a degraded mode
bolted on; it is the same view model rendered one line at a time.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from shamsu.context.compiler import ContextCompiler
from shamsu.interfaces.cancellation import Cancelled
from shamsu.interfaces.ids import ProjectId
from shamsu.interfaces.models import ModelClient
from shamsu.memory.store import MemoryStore
from shamsu.runtime.controller import RunController
from shamsu.runtime.limits import ExecutionLimits
from shamsu.runtime.session import AgentSession, SessionResult
from shamsu.state.records import ProjectRecord, new_id
from shamsu.state.store import StateStore
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.ui.render import render
from shamsu.ui.terminal import managed_screen, read_key, supports_tui, terminal_size
from shamsu.ui.view import Level, RunView

#: How often the display repaints while waiting. Fast enough that the spinner
#: reads as motion, slow enough to be invisible in a CPU profile.
FRAME_SECONDS = 0.1


@dataclass
class AppOptions:
    """Everything the interface needs that is not the agent itself."""

    workspace: Path
    request: str
    tui: bool = True
    colour: bool = True
    limits: ExecutionLimits | None = None
    database: Path | None = None

    @property
    def state_path(self) -> Path:
        return self.database or (self.workspace / ".shamsu" / "state.db")


async def run_task(
    model: ModelClient,
    options: AppOptions,
    *,
    stream: TextIO | None = None,
) -> SessionResult:
    """Run one task, displaying it as it happens.

    Returns the result rather than exiting, so a caller can decide the exit
    code and a test can assert on the outcome.
    """
    store = StateStore(options.state_path)
    root = str(options.workspace.resolve())

    # A project is identified by its root, not by a fresh id per run. Minting a
    # new one each time violated the UNIQUE constraint on `root` the second
    # time `shamsu` was run in the same directory.
    project = store.project_for_root(root) or store.upsert_project(
        ProjectRecord(
            project_id=ProjectId(new_id()),
            root=root,
            name=options.workspace.resolve().name,
        )
    )

    runs = RunController(store)
    session = AgentSession(
        store=store,
        runs=runs,
        model=model,
        gateway=ToolGateway(authoring_tools(options.workspace)),
        compiler=ContextCompiler(model),
        workspace=options.workspace,
        project_id=project.project_id,
        limits=options.limits,
        memory=MemoryStore(store, project.project_id),
    )

    view = RunView(request=options.request, workspace=str(options.workspace.resolve()))
    unsubscribe = runs.subscribe(view.apply)

    try:
        if options.tui and supports_tui(stream):
            return await _run_with_tui(session, runs, view, options)
        return await _run_plain(session, runs, view, options, stream)
    finally:
        unsubscribe()
        store.close()


async def _run_with_tui(
    session: AgentSession,
    runs: RunController,
    view: RunView,
    options: AppOptions,
) -> SessionResult:
    """Full-screen display, repainting alongside the run."""
    with managed_screen() as screen:
        work = asyncio.ensure_future(session.run(options.request))
        tick = 0

        while not work.done():
            size = terminal_size()
            screen.paint(
                render(
                    view,
                    size.width,
                    size.height,
                    now=_now(),
                    colour=options.colour,
                    tick=tick,
                )
            )
            tick += 1

            key = await asyncio.get_running_loop().run_in_executor(None, read_key, FRAME_SECONDS)
            if key in ("\x03", "q") and view.running:
                # The UI does not stop anything itself: it asks the runtime,
                # which owns cancellation and knows how to stop cleanly.
                for run_id in runs.active():
                    runs.cancel(run_id, "cancelled from the interface")

            await asyncio.sleep(0)

        result = await _settle(work, view)

        # One last frame so the final state is what stays on screen, then hold
        # it until the user acknowledges — the alternate screen is about to be
        # torn down and take the report with it.
        size = terminal_size()
        screen.paint(render(view, size.width, size.height, now=_now(), colour=options.colour))
        await _await_dismissal()

    _print_report(result, options)
    return result


async def _run_plain(
    session: AgentSession,
    runs: RunController,
    view: RunView,
    options: AppOptions,
    stream: TextIO | None,
) -> SessionResult:
    """One line per event. Scriptable, greppable, and CI-safe."""
    import sys

    target = stream or sys.stdout
    printed = 0

    def flush() -> None:
        nonlocal printed
        for item in view.activity[printed:]:
            target.write(f"  {item.label:<10} {item.detail}\n".rstrip() + "\n")
        printed = len(view.activity)
        target.flush()

    unsubscribe = runs.subscribe(lambda event: flush())
    try:
        work = asyncio.ensure_future(session.run(options.request))
        result = await _settle(work, view)
    finally:
        unsubscribe()

    flush()
    _print_report(result, options, stream=target)
    return result


async def _settle(work: asyncio.Future[SessionResult], view: RunView) -> SessionResult:
    """Await the run, turning a cancellation into a result rather than a crash."""
    try:
        return await work
    except Cancelled as exc:
        view.note(Level.STOP, "cancelled", str(exc))
        raise


async def _await_dismissal() -> None:
    """Hold the final frame until a key is pressed.

    Without this the report flashes up and vanishes with the alternate screen.
    Bounded so an unattended run in a terminal still exits on its own.
    """
    for _ in range(int(30 / FRAME_SECONDS)):
        key = await asyncio.get_running_loop().run_in_executor(None, read_key, FRAME_SECONDS)
        if key:
            return


def _print_report(result: SessionResult, options: AppOptions, stream: TextIO | None = None) -> None:
    """The evidence report, on the normal screen where it can be scrolled back."""
    import sys

    target = stream or sys.stdout
    target.write("\n" + result.render() + "\n")
    target.flush()
    del options


def _now() -> datetime:
    return datetime.now(tz=UTC)


def exit_code(result: SessionResult) -> int:
    """0 for a verified completion, 1 otherwise, 130 for cancellation.

    A shell has to be able to tell these apart. "Completed" here means the
    evidence gate opened, not that the agent believes it finished.
    """
    if result.completed:
        return 0
    return 130 if result.cancelled else 1


def describe_result(result: SessionResult) -> Sequence[str]:
    """One-line-per-fact summary, for a caller that wants to print its own."""
    lines = [f"state: {result.final_state.value}", f"reason: {result.stopped_because}"]
    if result.report is not None:
        lines.append(f"changed: {', '.join(result.report.changed_files) or 'nothing'}")
    return lines


__all__ = [
    "FRAME_SECONDS",
    "AppOptions",
    "describe_result",
    "exit_code",
    "run_task",
]
