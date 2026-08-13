"""The interactive session.

`shamsu` with no request opens one: type a request to run it, or a `/` command
to change how the next one runs.

**This module is built around the lesson in `ui/__init__.py`.** v1's REPL was
17,411 lines with display, input, session management, and agent control in one
object, and nothing in it could be tested without driving a terminal. So the
split here is deliberate and load-bearing:

* `Settings` is data.
* `handle_command` is a **pure function** — `(command, settings, models) ->
  Outcome`. It performs no I/O, opens no session, and prints nothing. Every
  slash command is testable by calling it.
* `Repl` is the only part that blocks on a terminal, and it takes its input
  callable and output stream as arguments, so a test drives it with a list.

The rule that keeps it that way: a command may *describe* a change and return
new settings, but it may not perform one.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TextIO

from shamsu.interfaces.cancellation import Cancelled
from shamsu.interfaces.models import ModelClient, ModelUnavailable
from shamsu.runtime.limits import ExecutionLimits
from shamsu.runtime.session import Approver
from shamsu.ui.app import (
    AppOptions,
    Wiring,
    build_wiring,
    describe_result,
    exit_code,
    run_request,
    small_talk,
)
from shamsu.ui.approval import ConsoleApprover
from shamsu.ui.commands import allowed_values, complete, help_lines, lookup
from shamsu.ui.editor import History, prompt
from shamsu.ui.terminal import supports_tui
from shamsu.ui.theme import AMBER, BOLD, GREEN, GREY, RED, VIOLET, paint, supports_colour

PROMPT = "> "


@dataclass(frozen=True)
class Settings:
    """Everything a turn needs that the user can change between turns."""

    model_name: str
    host: str
    workspace: Path
    context_tokens: int | None = None
    tui: bool = False
    colour: bool = True
    limits: ExecutionLimits | None = None
    database: Path | None = None

    #: "build" or "plan". Plan is OpenCode's read-only agent, and it maps onto
    #: something this runtime already enforces rather than merely asks for: a
    #: step declared `investigate` has every mutating tool stripped from it.
    mode: str = "build"

    #: Whether tool output is shown in full or as the summary line.
    details: bool = False

    @property
    def read_only(self) -> bool:
        """Whether the next turn withholds every mutating tool."""
        return self.mode == "plan"

    def options(self, request: str, *, approver: Approver | None = None) -> AppOptions:
        """The settings, as the options one turn runs under.

        `read_only` is threaded here rather than read from `mode` further down,
        because this is the one place a `Settings` becomes an `AppOptions` —
        and `mode` used to stop at this boundary. `/mode plan` set the field,
        `_status` printed it, the session frame coloured it, and nothing else
        ever looked: the runtime built an authoring gateway either way, so plan
        mode was a label on a build agent.
        """
        return AppOptions(
            workspace=self.workspace,
            request=request,
            tui=self.tui,
            colour=self.colour,
            limits=self.limits,
            database=self.database,
            read_only=self.read_only,
            approver=approver,
        )


@dataclass(frozen=True)
class Outcome:
    """What a command wants done. Nothing here has happened yet."""

    lines: tuple[str, ...] = ()
    settings: Settings | None = None
    quit: bool = False

    @property
    def rewire(self) -> bool:
        """Whether the agent has to be rebuilt before the next turn."""
        return self.settings is not None


def handle_command(line: str, settings: Settings, models: Sequence[str] = ()) -> Outcome:
    """Interpret one `/` command. Pure: decides, never acts.

    `models` is passed in rather than fetched so this stays testable without a
    server, and so a slow or absent Ollama cannot wedge the prompt.
    """
    parts = line.strip().split()
    typed, args = parts[0].lower(), parts[1:]

    # Resolved through the registry so aliases work everywhere and a command
    # cannot be dispatchable while being absent from /help and the dropdown.
    command = lookup(typed)
    if command is None:
        suggestion = complete(typed)
        hint = f" — did you mean {suggestion[0].name}?" if len(suggestion) == 1 else " — try /help"
        return Outcome(lines=(f"  unknown command {typed}{hint}",))

    if command.name == "/exit":
        return Outcome(quit=True)

    if command.name == "/help":
        return Outcome(lines=help_lines())

    if command.name == "/status":
        return Outcome(lines=_status(settings))

    if command.name == "/new":
        return Outcome(lines=("  new task — the workspace and model are unchanged",))

    if command.name == "/mode":
        return _set_mode(args, settings)

    if command.name == "/details":
        return _toggle_details(settings)

    if command.name == "/theme":
        return _set_theme(args, settings)

    if command.name == "/sessions":
        return Outcome(lines=_sessions(settings))

    if command.name == "/tools":
        return Outcome(lines=tool_lines(settings))

    if command.name == "/model":
        return _set_model(args, settings, models)

    if command.name == "/workspace":
        return _set_workspace(args, settings)

    if command.name == "/context":
        return _set_context(args, settings)

    return Outcome(lines=(f"  {command.name} is registered but not wired up",))


def _status(settings: Settings) -> tuple[str, ...]:
    window = settings.context_tokens
    database = settings.database or (settings.workspace / ".shamsu" / "state.db")
    return (
        f"  mode       {settings.mode}",
        f"  model      {settings.model_name}",
        f"  host       {settings.host}",
        f"  workspace  {settings.workspace}",
        f"  context    {window if window is not None else 'probed from the server'}",
        f"  database   {database}",
        f"  details    {'full tool output' if settings.details else 'summary lines'}",
    )


def _set_mode(args: Sequence[str], settings: Settings) -> Outcome:
    """Switch between the editing agent and the read-only one.

    Not a prompt instruction: `plan` means every step is declared `investigate`,
    and the runtime strips every mutating tool from those. The model is not
    asked to refrain from editing; it is not given anything that can edit.
    """
    if not args:
        return Outcome(lines=(f"  mode {settings.mode}",))

    # Checked against the registry's own list, so what the dropdown offers and
    # what the handler accepts cannot drift apart.
    allowed = allowed_values("/mode")
    wanted = args[0].lower()
    if wanted not in allowed:
        return Outcome(lines=(f"  unknown mode {wanted!r} — {' or '.join(allowed)}",))
    if wanted == settings.mode:
        return Outcome(lines=(f"  already in {wanted}",))

    lines = (
        (
            "  mode -> plan",
            "  the gateway is rebuilt without file.patch, test.run, check.run or git.checkpoint",
            "  every plan step is materialised as investigate",
        )
        if wanted == "plan"
        else (
            "  mode -> build",
            "  edits allowed; completion still requires verified evidence",
        )
    )
    return Outcome(lines=lines, settings=replace(settings, mode=wanted))


def _toggle_details(settings: Settings) -> Outcome:
    wanted = not settings.details
    shown = "full tool output" if wanted else "summary lines"
    return Outcome(lines=(f"  details -> {shown}",), settings=replace(settings, details=wanted))


def _set_theme(args: Sequence[str], settings: Settings) -> Outcome:
    if not args:
        return Outcome(lines=(f"  colour {'on' if settings.colour else 'off'}",))

    allowed = allowed_values("/theme")
    wanted = args[0].lower()
    if wanted not in allowed:
        return Outcome(lines=(f"  unknown theme {wanted!r} — {' or '.join(allowed)}",))
    return Outcome(
        lines=(f"  colour -> {wanted}",),
        settings=replace(settings, colour=wanted == "on"),
    )


def recent_runs(settings: Settings, limit: int = 10) -> tuple[tuple[str, str, str], ...]:
    """Past runs in this workspace as `(when, status, request)`, newest first.

    Read straight from the state database rather than from anything the session
    remembers: runs from previous invocations are the ones worth listing, and
    they were already recorded. Best-effort — a missing or unreadable database
    is an empty list, never an error at a prompt.
    """
    database = settings.database or (settings.workspace / ".shamsu" / "state.db")
    if not database.exists():
        return ()

    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT r.started_at, r.status, t.request "
            "FROM runs r JOIN tasks t ON t.task_id = r.task_id "
            "ORDER BY r.started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    except sqlite3.DatabaseError:  # pragma: no cover - corrupt or foreign schema
        return ()
    finally:
        connection.close()

    return tuple(
        (str(started_at)[11:16] or "--:--", str(status), str(request))
        for started_at, status, request in rows
    )


def tool_lines(settings: Settings) -> tuple[str, ...]:
    """What the agent can run, and the arguments each tool takes.

    Rendered from `Tool.input_schema()`, so it is the same shape the gateway
    validates against and cannot describe an argument that does not exist.

    Worth having for its own sake — "what can this thing actually do" is the
    first question anyone asks — and worth noting that the model is currently
    shown *less* than this: `ContextCompiler._render_tools` emits only name and
    purpose, which is why it guesses argument names and gets refused.
    """
    from shamsu.tools import authoring_tools

    lines: list[str] = []
    for tool in sorted(authoring_tools(settings.workspace), key=lambda t: t.name):
        schema = tool.input_schema()
        required = set(schema.get("required", ()))
        arguments = [
            f"{name}*" if name in required else name for name in (schema.get("properties") or {})
        ]
        signature = ", ".join(arguments) or "no arguments"
        lines.append(f"  {tool.name:<16} {signature}")
        lines.append(f"  {'':<16} {_fit(tool.contract.purpose, 58)}")

    lines.append("  * = required")
    return tuple(lines)


def _sessions(settings: Settings) -> tuple[str, ...]:
    """`/sessions`, rendered from `recent_runs`."""
    runs = recent_runs(settings)
    if not runs:
        return ("  no runs yet in this workspace",)
    return tuple(f"  {when}  {status:<10}  {_fit(request, 46)}" for when, status, request in runs)


def _fit(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _set_model(args: Sequence[str], settings: Settings, models: Sequence[str]) -> Outcome:
    if not args:
        lines = [f"  model     {settings.model_name}"]
        lines.append(
            "  available " + ", ".join(models)
            if models
            else "  available (could not reach the server; type a name anyway)"
        )
        return Outcome(lines=tuple(lines))

    name = args[0]
    lines = [f"  model -> {name}"]
    if models and name not in models:
        # A warning, not a refusal: the list is what is *pulled*, and a name
        # absent from it may still be valid on another host.
        lines.append(f"  note: {name!r} is not in this server's list; pull it if a run fails")
    return Outcome(lines=tuple(lines), settings=replace(settings, model_name=name))


def _set_workspace(args: Sequence[str], settings: Settings) -> Outcome:
    if not args:
        return Outcome(lines=(f"  workspace {settings.workspace}",))

    target = Path(" ".join(args)).expanduser()
    if not target.is_dir():
        return Outcome(lines=(f"  not a directory: {target}",))

    resolved = target.resolve()
    return Outcome(
        lines=(f"  workspace -> {resolved}",),
        settings=replace(settings, workspace=resolved),
    )


def _set_context(args: Sequence[str], settings: Settings) -> Outcome:
    if not args:
        current = settings.context_tokens
        return Outcome(
            lines=(f"  context {current if current is not None else 'probed from the server'}",)
        )

    try:
        window = int(args[0])
    except ValueError:
        return Outcome(lines=(f"  not a number: {args[0]}",))
    if window <= 0:
        return Outcome(lines=("  context must be positive",))

    return Outcome(
        lines=(f"  context -> {window} (sent as num_ctx; larger windows cost VRAM)",),
        settings=replace(settings, context_tokens=window),
    )


class Repl:
    """The loop. The only part of this module that blocks on a terminal."""

    def __init__(
        self,
        settings: Settings,
        build_model: Callable[[Settings], ModelClient],
        *,
        read_line: Callable[[str], str] | None = None,
        stream: TextIO | None = None,
        list_models: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._settings = settings
        self._build_model = build_model
        self._stream = stream
        self._list_models = list_models or (lambda host: ())
        self._wiring: Wiring | None = None
        self._history = History()

        # A real editor when there is a terminal to edit in, and `input()` when
        # there is not -- a piped session still has to work, and driving the key
        # reader against a pipe would block forever waiting for a keystroke.
        self._read_line = read_line or self._default_reader()

    def _default_reader(self) -> Callable[[str], str]:
        import sys

        if supports_tui(self._stream or sys.stdout):
            return lambda text: prompt(text, self._history, stream=self._stream)
        return input

    @property
    def settings(self) -> Settings:
        return self._settings

    def _write(self, text: str = "") -> None:
        import sys

        target = self._stream or sys.stdout
        target.write(text + "\n")
        target.flush()

    @property
    def _colour(self) -> bool:
        import sys

        return self._settings.colour and supports_colour(self._stream or sys.stdout)

    def _paint(self, text: str, colour: str) -> str:
        return paint(text, colour, self._colour)

    def _banner(self) -> None:
        """The idle screen. Says what is loaded and what the keys are."""
        self._write()
        self._write(f"  {self._paint('☀ SHAMSU', AMBER + BOLD)}  {self._paint('v2', GREY)}")
        self._write(
            f"  {self._paint('local-first · evidence-gated · nothing completes unproved', GREY)}"
        )
        self._write()
        self._write(
            f"  {self._paint('model', GREY)}      {self._paint(self._settings.model_name, VIOLET)}"
        )
        workspace = self._paint(str(self._settings.workspace), GREY)
        self._write(f"  {self._paint('workspace', GREY)}  {workspace}")
        self._write()
        self._write(f"  {self._paint('type a request, or /help', GREY)}")
        self._write()

    def _wired(self) -> Wiring:
        """Build the agent on first use, and after anything that invalidates it.

        Deferred so that opening a session never depends on the model server
        being up -- an unreachable Ollama should be an error on the first
        *request*, which can say so, not on the first prompt.
        """
        if self._wiring is None:
            # `read_only` and `approver` are bound into the session here, not
            # per turn, which is why `/mode` must rewire -- and it does, because
            # every command returning new settings invalidates the agent.
            self._wiring = build_wiring(
                self._build_model(self._settings),
                self._settings.options("", approver=self._approver()),
            )
        return self._wiring

    def _approver(self) -> Approver:
        """Ask at this session's prompt, on this session's stream."""
        return ConsoleApprover(read_line=self._read_line, stream=self._stream)

    def _rewire(self) -> None:
        if self._wiring is not None:
            self._wiring.close()
            self._wiring = None

    async def run(self) -> int:
        """Read, dispatch, repeat. Returns a process exit code."""
        self._banner()

        try:
            while True:
                try:
                    line = self._read_line(self._paint(PROMPT, AMBER))
                except (EOFError, KeyboardInterrupt):
                    self._write()
                    return 0

                line = line.strip()
                if not line:
                    continue

                if line.startswith("/"):
                    if self._dispatch(line):
                        return 0
                    continue

                await self._turn(line)
        finally:
            self._rewire()

    def _dispatch(self, line: str) -> bool:
        """Apply one command. Returns whether the session should end."""
        models = self._list_models(self._settings.host) if line.startswith("/model") else ()
        outcome = handle_command(line, self._settings, models)

        for text in outcome.lines:
            self._write(text)

        if outcome.settings is not None:
            self._settings = outcome.settings
            # The model client and the state database are both bound at
            # construction, so a change to either means a fresh agent.
            self._rewire()

        return outcome.quit

    async def _turn(self, request: str) -> None:
        """Run one request, keeping the session alive whatever it does."""
        options = self._settings.options(request)

        try:
            wiring = self._wired()
        except ModelUnavailable as exc:
            self._write(f"  {exc}")
            return

        # Small talk gets an answer, not a run. Checked before the session is
        # started so no task, plan, or run row is created for a greeting.
        try:
            reply = await small_talk(
                wiring.session.model,
                request,
                workspace=self._settings.workspace,
                limits=self._settings.limits,
            )
        except Cancelled:
            self._write("  stopped.")
            return
        if reply is not None:
            if reply:
                self._write(f"  {reply}")
            self._write()
            return

        try:
            result = await run_request(wiring, options, stream=self._stream)
        except Cancelled:
            self._write("  stopped.")
            return
        except ModelUnavailable as exc:
            # Expected and recoverable: the user can /model to something else.
            self._write(f"  {exc}")
            return
        except KeyboardInterrupt:
            for run_id in self._wired().runs.active():
                self._wired().runs.cancel(run_id, "interrupted")
            self._write("  stopped.")
            return

        # The report above carried the detail; this is the at-a-glance verdict.
        # Green comes from `exit_code`, which already owns what "verified" means
        # -- a second definition here would be one that could drift from it.
        tint = GREEN if exit_code(result) == 0 else RED
        for fact in describe_result(result):
            key, separator, value = fact.partition(": ")
            label = self._paint(key + separator, GREY)
            self._write(f"  {label} {self._paint(value, tint if key == 'state' else GREY)}")
        self._write()


def run_repl(
    settings: Settings,
    build_model: Callable[[Settings], ModelClient],
    *,
    list_models: Callable[[str], Sequence[str]] | None = None,
) -> int:
    """Entry point: open an interactive session and return an exit code.

    Full-screen when there is a terminal to draw in, line-oriented when there
    is not. The fallback is not a lesser mode kept around for politeness: a
    piped or redirected session has to work, and a full-screen app in a log
    file is unreadable.
    """
    import sys

    if supports_tui(sys.stdout):
        from shamsu.ui.interactive import InteractiveSession

        session = InteractiveSession(
            settings,
            build_model,
            handle_command=handle_command,
            list_models=list_models,
        )
        try:
            return asyncio.run(session.run())
        except KeyboardInterrupt:
            return 130

    repl = Repl(settings, build_model, list_models=list_models)
    try:
        return asyncio.run(repl.run())
    except KeyboardInterrupt:
        return 130


__all__ = [
    "PROMPT",
    "Outcome",
    "Repl",
    "Settings",
    "handle_command",
    "run_repl",
]
