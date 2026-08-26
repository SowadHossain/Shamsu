"""Small SHAMSU harness: TUI shell plus the simple chat loop."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import InMemoryHistory
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from shamsu import __version__
from shamsu.agents.simple_chat import SimpleChatLoop, build_simple_tools
from shamsu.agents.simple_feedback import FeedbackQueue
from shamsu.cli.arguments import parse_args
from shamsu.cli.commands import CommandContext, completion_words, dispatch
from shamsu.cli.live_console import TurnTelemetry, route_input
from shamsu.cli.prompt_label import SURFACE_CLI, session_prompt_label
from shamsu.cli.turn_render import CliTurnRenderer
from shamsu.llm.ollama_client import default_ollama_client
from shamsu.runtime.models import initialize_model_tier, model_for_role
from shamsu.runtime.run_control import active_run_ids, cancel_run
from shamsu.runtime.settings import verbosity as saved_verbosity
from shamsu.runtime.timeouts import TimeoutConfig
from shamsu.runtime.turn_stream import TurnStream
from shamsu.safety.approval import ask_approval
from shamsu.session.manager import SessionLogger, SessionManager
from shamsu.tools.workspace import WorkspaceTool


class SmallSlashCompleter(Completer):
    """Slash and @file completions for the reduced TUI harness.

    The slash half is generated from the command registry rather than a second
    hand-maintained list. The REPL kept those two apart and they drifted: the
    approval prompt advertised a key its own menu read as Deny.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def get_completions(self, document: Any, complete_event: Any):
        text = document.text_before_cursor
        token = text.rsplit(maxsplit=1)[-1] if text.strip() else text
        if token.startswith("@"):
            prefix = token[1:]
            with contextlib.suppress(Exception):
                for path in WorkspaceTool(self.workspace).find_files(prefix, limit=20):
                    rel = path.relative_to(self.workspace).as_posix()
                    yield Completion("@" + rel, start_position=-len(token))
            return
        if not text.startswith("/"):
            return
        lowered = text.lower()
        for command in completion_words():
            if command.startswith(lowered):
                yield Completion(command, start_position=-len(text))


#: The terminal app this process is running, if any.
#:
#: Remote surfaces need to know whether a person is sitting at this terminal:
#: a Telegram turn mirrors itself into the local console, and it must not paint
#: over a framed TUI that owns the screen. The REPL exposed this as
#: `active_live_console()` / `active_frame()`; when the REPL was deleted, the
#: Telegram bridge kept importing those names and its ImportError was swallowed
#: by a bare `except`, so remote turns silently stopped mirroring at all. One
#: accessor, set by the app that owns the terminal, is the whole mechanism.
_ACTIVE_APP: "SmallHarnessApp | None" = None


def active_app() -> "SmallHarnessApp | None":
    return _ACTIVE_APP


def active_frame() -> Any:
    """The framed TUI if one is running and owns the screen, else None."""
    app = _ACTIVE_APP
    frame = getattr(app, "frame", None) if app is not None else None
    return frame if frame is not None and frame.running else None


def active_telemetry() -> Any:
    """The live turn telemetry for this terminal, if there is one."""
    app = _ACTIVE_APP
    return getattr(app, "telemetry", None) if app is not None else None


class SmallHarnessApp:
    """Owns the terminal session and runs natural prompts through SimpleChatLoop."""

    def __init__(self, workspace: Path, console: Console) -> None:
        self.workspace = workspace
        self.console = console
        self.manager = SessionManager(workspace)
        self.session_logger: SessionLogger = self.manager.continue_or_create(
            "SHAMSU TUI",
            max_age_seconds=1800,
            max_messages=80,
        )
        self.feedback = FeedbackQueue()
        self.telemetry = TurnTelemetry()
        self.telemetry.feedback_depth = lambda: len(self.feedback)
        self.frame: Any = None
        self._console_state: tuple[Any, int] | None = None
        self._running = True

    def run(self) -> None:
        global _ACTIVE_APP
        initialize_model_tier(self.workspace)
        self._banner()
        _ACTIVE_APP = self
        self.start_frame()
        history = InMemoryHistory()
        prompt = PromptSession(
            history=history,
            completer=SmallSlashCompleter(self.workspace),
            complete_while_typing=True,
        )
        try:
            while self._running:
                try:
                    user_input = self._read_line(prompt)
                except EOFError:
                    self.console.print("Goodbye.")
                    break
                if not self._handle_command(user_input):
                    self._run_turn(user_input)
        finally:
            self.stop_frame()
            _ACTIVE_APP = None

    def _read_line(self, prompt: PromptSession) -> str:
        if self.frame is not None and self.frame.running:
            return self.frame.read_line()
        return prompt.prompt(self._prompt_label())

    def _banner(self) -> None:
        self.console.print(
            Panel(
                f"SHAMSU v{__version__}\n"
                f"Workspace: {self.workspace}\n"
                f"Model: {model_for_role('qa')}\n\n"
                "Type a prompt, /help, /tui, or /exit.",
                title="Small Harness",
                border_style="cyan",
            )
        )

    def _prompt_label(self) -> str:
        return session_prompt_label(self.session_logger, SURFACE_CLI)

    def command_context(self) -> CommandContext:
        return CommandContext(workspace=self.workspace, console=self.console, app=self)

    def request_exit(self) -> None:
        """Asked for by `/exit`. The loop owns the flag; the command does not."""
        self._running = False

    def _handle_command(self, user_input: str) -> bool:
        text = (user_input or "").strip()
        if not text:
            return True
        # Bare `exit`/`quit` without the slash stayed in muscle memory from the
        # REPL, and a harness that answers them with a model turn is annoying.
        if text.lower() in {"exit", "quit"}:
            self.request_exit()
            self.console.print("Goodbye.")
            return True
        return dispatch(text, self.command_context())

    def start_frame(self) -> bool:
        if self.frame is not None and self.frame.running:
            return True
        try:
            from shamsu.cli.tui import FrameHost, PaneWriter, TuiApp
        except Exception as exc:  # noqa: BLE001
            self.console.print(f"[yellow]TUI unavailable: {exc}[/yellow]")
            return False
        frame: Any = None

        def submit(text: str) -> None:
            if frame is not None:
                frame.submit(text)

        app = TuiApp(
            telemetry=self.telemetry,
            on_submit=submit,
            completer=SmallSlashCompleter(self.workspace),
            prompt_label=self._prompt_label,
            on_interrupt=self._interrupt,
            on_exit=self.stop_frame,
            workspace=self.workspace,
        )
        frame = FrameHost(app)
        frame.on_route = self._route_midturn
        if not frame.start():
            self.console.print("[yellow]The framed TUI could not start.[/yellow]")
            return False
        self.frame = frame
        self._console_state = (self.console.file, self.console.width)
        self.console.file = PaneWriter(app.pane, app.invalidate)
        self.console.width = app.output_width()
        app.echo("SHAMSU small harness TUI. /help for commands, Ctrl+D to close.")
        return True

    def stop_frame(self) -> None:
        frame, self.frame = self.frame, None
        if self._console_state is not None:
            self.console.file, self.console.width = self._console_state
            self._console_state = None
        if frame is not None:
            with contextlib.suppress(Exception):
                frame.stop()

    def _route_midturn(self, text: str) -> None:
        """Something typed while a turn is still running.

        Plain text becomes feedback the loop picks up at its next tool
        boundary. A slash command runs now only if it is read-only - see
        `Command.midturn` for why writing config mid-turn is refused.
        """
        route, payload = route_input(text, midturn=True)
        if route == "feedback":
            self.feedback.push(payload)
            return
        if route == "command":
            dispatch(payload, self.command_context(), midturn=True)
            return
        if self.frame is not None:
            self.frame.app.echo(f"queued for after this turn: {payload}")

    def _interrupt(self) -> None:
        for run_id in active_run_ids():
            with contextlib.suppress(Exception):
                cancel_run(run_id)
        if self.frame is not None:
            self.frame.app.echo("Stopping the current turn...")

    def _ask_approval(self, request: Any) -> bool:
        if self.frame is None:
            return ask_approval(request, self.console)
        app = self.frame.app
        app.open_approval(request)
        try:
            answer = app.await_approval(timeout=900)
            return answer.lower() in {"y", "a"}
        finally:
            app.close_approval()

    def _run_turn(self, user_input: str) -> None:
        clean = (user_input or "").strip()
        if not clean:
            return
        self.session_logger.append_message("user", clean, source=SURFACE_CLI)
        self.manager.maybe_auto_title(self.session_logger, clean)
        try:
            asyncio.run(self._run_simple_chat(clean))
        except KeyboardInterrupt:
            self.console.print("[yellow]Turn interrupted.[/yellow]")
        except Exception as exc:  # noqa: BLE001
            self.console.print(f"[red]Turn failed: {exc}[/red]")

    async def _run_simple_chat(self, user_input: str) -> None:
        stream = TurnStream(self.workspace, self.session_logger.session_id, persist=True)
        stream.add_renderer(
            CliTurnRenderer(
                self.console,
                status_updater=self.telemetry.set_status,
                verbosity=saved_verbosity(),
            )
        )
        stream.add_renderer(self.telemetry.absorb)
        tools = build_simple_tools(
            self.workspace,
            main_loop=asyncio.get_running_loop(),
            console_approval=self._ask_approval,
            session_logger=self.session_logger,
        )
        loop = SimpleChatLoop(
            self.workspace,
            client=default_ollama_client(timeout_config=TimeoutConfig.from_env()),
            feedback=self.feedback,
            tools=tools,
            session_logger=self.session_logger,
            emit=stream.publish,
            source=SURFACE_CLI,
        )
        result = await loop.run(user_input)
        body = result.final.strip() or "No response returned."
        if self.frame is not None:
            with self.frame.app.answering():
                self.console.print(Markdown(body))
        else:
            self.console.print(Markdown(body))
        self.session_logger.append_message("assistant", body, source=SURFACE_CLI)


def resolve_workspace(workspace_arg: str | None) -> Path:
    workspace = Path(workspace_arg).expanduser() if workspace_arg else Path.cwd()
    resolved = workspace.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"Workspace does not exist or is not a directory: {resolved}")
    return resolved


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure: Callable[..., Any] | None = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> None:
    _force_utf8_stdio()
    args = parse_args(argv)
    console = Console()
    try:
        workspace = resolve_workspace(args.workspace)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(2) from exc
    if args.command == "run":
        from shamsu.cli.noninteractive import run_cli

        raise SystemExit(run_cli(args))
    if args.command == "web":
        from shamsu.webui.cli import DEFAULT_PORT, serve

        raise SystemExit(
            serve(
                workspace,
                port=args.port if args.port is not None else DEFAULT_PORT,
                console=console,
                scan=args.scan,
            )
        )
    SmallHarnessApp(workspace, console).run()


if __name__ == "__main__":
    main()
