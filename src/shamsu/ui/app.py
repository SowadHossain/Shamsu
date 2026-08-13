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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from shamsu.agent.mentions import resolve as resolve_mentions
from shamsu.agent.readonly import ReadOnlyAgent
from shamsu.agent.triage import Intent, classify, describe_capabilities, respond
from shamsu.context.compiler import ContextCompiler
from shamsu.interfaces.cancellation import CancellationToken, Cancelled
from shamsu.interfaces.ids import ProjectId
from shamsu.interfaces.models import ModelClient, ModelUnavailable
from shamsu.memory.store import MemoryStore
from shamsu.runtime.controller import RunController
from shamsu.runtime.limits import ExecutionLimits
from shamsu.runtime.session import AgentSession, Approver, SessionResult
from shamsu.state.records import ProjectRecord, new_id
from shamsu.state.store import StateStore
from shamsu.tools import ToolGateway, authoring_tools, read_only_tools
from shamsu.ui.render import render
from shamsu.ui.terminal import managed_screen, read_key, supports_tui, terminal_size
from shamsu.ui.theme import activity_line, fit_encoding, supports_colour, supports_unicode
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

    #: Plan mode. Withholds every mutating tool from the gateway *and* forces
    #: every plan step to `investigate`. Both, because either alone is a
    #: half-measure: tools without step kinds leaves a change step demanding
    #: evidence it has no tool to produce, and step kinds without tools leaves
    #: the capability one prompt away.
    read_only: bool = False

    #: Asked to decide when a step requires approval. `None` stops the run at
    #: `WAIT_APPROVAL` instead, which is what an unattended caller wants.
    approver: Approver | None = None

    @property
    def state_path(self) -> Path:
        return self.database or (self.workspace / ".shamsu" / "state.db")


@dataclass
class Wiring:
    """One assembled agent: the store, the run controller, and the session.

    Separated from `run_task` so an interactive session can build this once and
    run many requests against it. `AgentSession.run` is already safe to call
    repeatedly -- `test_a_session_can_run_a_second_task_without_leaking_state`
    pins that -- so a REPL needs no new runtime machinery, only a way to hold
    the wiring open across turns.
    """

    store: StateStore
    runs: RunController
    session: AgentSession

    def close(self) -> None:
        self.store.close()


def build_wiring(model: ModelClient, options: AppOptions) -> Wiring:
    """Assemble an agent for one workspace."""
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

    # Plan mode withholds the mutating tools rather than forbidding their use.
    # A tool the gateway never registered cannot be called by a model that
    # ignores its instructions, which a prompt-level restriction cannot say.
    tools = (
        read_only_tools(options.workspace)
        if options.read_only
        else authoring_tools(options.workspace)
    )

    session = AgentSession(
        store=store,
        runs=runs,
        model=model,
        gateway=ToolGateway(tools),
        compiler=ContextCompiler(model),
        workspace=options.workspace,
        project_id=project.project_id,
        limits=options.limits,
        memory=MemoryStore(store, project.project_id),
        read_only=options.read_only,
        approver=options.approver,
    )
    return Wiring(store=store, runs=runs, session=session)


async def small_talk(
    model: ModelClient,
    request: str,
    *,
    workspace: Path | None = None,
    limits: ExecutionLimits | None = None,
    cancel: CancellationToken | None = None,
) -> str | None:
    """An answer when `request` is not a task, or None when it is.

    The seam every entry point goes through before starting a run. It lives
    here rather than in each interface because there are three of them —
    one-shot CLI, line REPL, full-screen session — and a triage rule that only
    two of them apply is a rule the third contradicts.

    Returning `None` is the answer for real work: the caller proceeds to
    `run_request` exactly as before, so nothing about the task path changes.

    **Questions are answered, not planned.** Every question used to become a
    task: "what can you do?" went through plan, author, verify and repair
    before dying on *"the failure implicates no editable file"*, because
    attempting a change and failing to prove it was the only thing the runtime
    knew how to do. A question requires no evidence and changes nothing, so it
    gets a read-only investigation and its conclusion.
    """
    # The model decides the change-or-question boundary; the settled cases —
    # empty, small talk, capability questions — never reach it, so a greeting
    # still costs nothing.
    intent = await classify(request, model, cancel=cancel)
    if intent is Intent.TASK:
        return None
    if intent is Intent.EMPTY:
        return ""

    # `@auth.py` is interface syntax. Left in, the agent looks for a file whose
    # name begins with `@` and finds nothing — a live run answered "the
    # repository contains a single file named @OpenBazaar_Marketplace_PRD.docx".
    mentioned = resolve_mentions(request, workspace or Path.cwd())

    if intent is Intent.CAPABILITIES:
        # No model call: the answer is a fact about the tool registry, and a
        # model asked to describe its own tools will confidently name ones it
        # does not have.
        root = workspace or Path.cwd()
        return describe_capabilities([tool.contract for tool in authoring_tools(root)])

    if intent is Intent.QUESTION:
        answer = await _investigate_and_answer(
            model,
            workspace or Path.cwd(),
            mentioned.text,
            limits,
            cancel,
            must_read=mentioned.resolved,
        )
        if mentioned.unresolved:
            missing = ", ".join(mentioned.unresolved)
            answer += f"\n\n[I could not find these in the workspace: {missing}]"
        return answer

    return await respond(model, request, cancel)


async def _investigate_and_answer(
    model: ModelClient,
    workspace: Path,
    question: str,
    limits: ExecutionLimits | None,
    cancel: CancellationToken | None,
    must_read: Sequence[str] = (),
) -> str:
    """Answer a question about the code from a bounded, read-only look at it.

    Read-only by construction: the gateway is built from `read_only_tools`, so
    a question cannot edit anything regardless of what the model decides to do.
    No run, no plan, no evidence gate — a question has nothing to prove, and
    gating it was what turned "what is this project?" into a blocked run.
    """
    gateway = ToolGateway(read_only_tools(workspace))
    agent = ReadOnlyAgent(model, gateway, ContextCompiler(model), limits)

    try:
        # The conclusion *is* the answer here, so it has to be grounded in
        # something. Nothing downstream will catch it otherwise.
        found = await agent.investigate(
            question, cancel=cancel, require_lookup=True, must_read=must_read
        )
    except ModelUnavailable as exc:
        return str(exc)

    if not found.conclusion.strip():
        return (
            f"I could not answer that from the repository ({found.stopped_because}). "
            "Try naming a file or a symbol."
        )

    answer = found.conclusion.strip()

    # A question has no evidence gate — correctly, since it changes nothing —
    # so nothing else will catch an answer the model invented. Saying what was
    # actually inspected is the cheapest honest substitute, and the difference
    # matters: a live run described a Node project with `package.json` and Jest
    # in a directory containing one Python file.
    looked_at = [obs.tool for obs in found.observations if obs.ok]
    if not looked_at:
        return (
            f"{answer}\n\n"
            "[Unverified: I answered without reading anything in this repository, "
            "so treat the above as a guess.]"
        )

    # Named by what was *called*, not by a file list. `project.inspect` and
    # `code.search` take no path, so `files_seen` stays empty even though the
    # agent genuinely looked — and "files read: no files" alongside a real
    # answer reads as a contradiction rather than as provenance.
    used = ", ".join(dict.fromkeys(looked_at))
    if found.files_seen:
        used += f" · read {', '.join(found.files_seen[:6])}"
    return f"{answer}\n\n[Based on: {used}]"


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
    wiring = build_wiring(model, options)
    try:
        return await run_request(wiring, options, stream=stream)
    finally:
        wiring.close()


async def run_request(
    wiring: Wiring,
    options: AppOptions,
    *,
    stream: TextIO | None = None,
) -> SessionResult:
    """Run `options.request` against an already-built agent.

    Does not close the store: the caller owns the wiring's lifetime, which is
    what lets an interactive session keep it across turns.

    `@file` references are resolved first, here rather than inside the runtime:
    the marker is interface syntax, and `runtime/` should never have to know
    which characters a particular front end reserves.
    """
    mentioned = resolve_mentions(options.request, options.workspace)
    if mentioned.text != options.request:
        options = replace(options, request=mentioned.text)

    view = RunView(request=options.request, workspace=str(options.workspace.resolve()))
    unsubscribe = wiring.runs.subscribe(view.apply)

    try:
        if options.tui and supports_tui(stream):
            return await _run_with_tui(wiring.session, wiring.runs, view, options)
        return await _run_plain(wiring.session, wiring.runs, view, options, stream)
    finally:
        unsubscribe()


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
    """One line per event. Scriptable, greppable, and CI-safe.

    Colour is decided per stream rather than per run: `supports_colour` refuses
    to write escapes into anything that is not a terminal, so a piped run still
    produces plain text a grep can read.
    """
    import sys

    target = stream or sys.stdout
    colour = options.colour and supports_colour(target)
    # Decided once per run rather than per line: the stream's encoding does not
    # change mid-run, and asking 200 times would be 200 encode attempts.
    unicode_ok = supports_unicode(target)
    printed = 0

    def flush() -> None:
        nonlocal printed
        for item in view.activity[printed:]:
            target.write(
                activity_line(
                    item.level, item.label, item.detail, colour=colour, unicode=unicode_ok
                ).rstrip()
                + "\n"
            )
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
    target.write(fit_encoding("\n" + result.render() + "\n", target))
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
    "Wiring",
    "build_wiring",
    "describe_result",
    "exit_code",
    "run_request",
    "run_task",
    "small_talk",
]
