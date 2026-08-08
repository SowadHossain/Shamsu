"""The full-screen interactive session.

The driver: it owns the terminal, the key loop, and the frame. Everything it
*decides* is delegated — `commands.complete` for the dropdown, `handle_command`
for what a command means, `editor.Buffer` for editing rules,
`session_frame.render_session` for what appears. This module holds no editing
rules and no command semantics, which is what stops it becoming v1's
17,411-line `repl.py`.

Two things worth knowing.

**The input line lives below the box.** The terminal's own cursor marks the
position, so no column arithmetic can misplace it and a wide character costs
nothing to handle.

**A run repaints while it runs.** `session.run` is a task raced against the key
reader, so the spinner turns, activity appears as it happens, and Ctrl-C reaches
`RunController.cancel` instead of killing the process.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from shamsu.artifacts.hashing import git_listed_files, is_ignored
from shamsu.interfaces.cancellation import Cancelled
from shamsu.interfaces.models import ModelClient, ModelUnavailable
from shamsu.ui.app import Wiring, build_wiring, exit_code
from shamsu.ui.commands import COMMANDS, common_prefix, complete, file_fragment, match_files
from shamsu.ui.editor import CANCELLED, EOF, Buffer, History
from shamsu.ui.render import SPINNER
from shamsu.ui.repl import recent_runs
from shamsu.ui.session_frame import MAX_SUGGESTIONS, SessionState, render_session
from shamsu.ui.terminal import Key, Screen, managed_screen, read_key, terminal_size
from shamsu.ui.view import Level, RunView

if TYPE_CHECKING:  # imported for types only; `repl` imports this module at runtime
    from shamsu.ui.repl import Outcome, Settings

#: How often the display repaints. Fast enough that the spinner reads as motion.
FRAME_SECONDS = 0.08

#: `_press` returns one of these, or a request to run.
QUIT = "\x00quit"

#: `^X` then a key, using OpenCode's letters so muscle memory transfers.
#: Each maps to a command that already exists, so the leader adds a shortcut
#: and never a second implementation.
_LEADER: dict[str, str] = {
    "n": "/new",
    "l": "/sessions",
    "m": "/model",
    "t": "/theme",
    "s": "/status",
    "h": "/help",
    "?": "/help",
    "q": "/exit",
}


class InteractiveSession:
    """One full-screen session over one workspace."""

    def __init__(
        self,
        settings: Settings,
        build_model: Callable[[Settings], ModelClient],
        *,
        handle_command: Callable[[str, Settings, Sequence[str]], Outcome],
        list_models: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self._settings = settings
        self._build_model = build_model
        self._handle_command = handle_command
        self._list_models = list_models or (lambda host: ())

        self._wiring: Wiring | None = None
        self._history = History()
        self._buffer = Buffer.new()
        self._state = SessionState(
            workspace=str(settings.workspace),
            model=settings.model_name,
            mode=settings.mode,
            recent=recent_runs(settings),
        )
        self._tick = 0
        self._leader = False
        self._palette = False
        self._file_cache: tuple[str, ...] | None = None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def state(self) -> SessionState:
        return self._state

    # -- lifecycle ---------------------------------------------------------

    def _wired(self) -> Wiring:
        """Build the agent on first use, and after anything that invalidates it."""
        if self._wiring is None:
            self._wiring = build_wiring(
                self._build_model(self._settings),
                self._settings.options(""),
            )
        return self._wiring

    def _rewire(self) -> None:
        if self._wiring is not None:
            self._wiring.close()
            self._wiring = None

    async def run(self) -> int:
        """Read, dispatch, repeat, in the alternate screen."""
        self._state.note(Level.NOTE, "ready", "type a request, or / for commands")

        with managed_screen() as screen:
            try:
                while True:
                    self._paint(screen)

                    key = read_key(FRAME_SECONDS)
                    if not key:
                        continue

                    outcome = self._press(key)
                    if outcome == QUIT:
                        return 0
                    if outcome:
                        await self._turn(screen, outcome)
            finally:
                self._rewire()

    # -- input -------------------------------------------------------------

    def _press(self, key: str) -> str:
        """Apply one key. Returns a request to run, `QUIT`, or ""."""
        if self._leader:
            return self._leader_key(key)

        if key == Key.CTRL_X:
            # OpenCode's leader: press, release, then the next key acts. Held
            # as state rather than read inline so the frame can say so.
            self._leader = True
            self._state.status = "^X …"
            return ""

        if key == Key.CTRL_P:
            self._open_palette()
            return ""

        if self._open and self._dropdown(key):
            return ""

        self._buffer, finished = self._buffer.press(key)
        self._sync()

        if finished is None:
            return ""
        if finished == EOF:
            return QUIT
        if finished == CANCELLED:
            return ""

        line = finished.strip()
        if not line:
            self._buffer = Buffer.new(self._history.entries())
            self._sync()
            return ""

        # Recorded *before* the fresh buffer is built: a buffer takes a snapshot
        # of the history, so adding afterwards left the line just submitted
        # unreachable by the up arrow until the turn after next.
        self._history.add(line)
        self._buffer = Buffer.new(self._history.entries())
        self._sync()

        if line.startswith("/"):
            return QUIT if self._dispatch(line) else ""
        return line

    @property
    def _open(self) -> bool:
        """Whether a dropdown is showing anything."""
        return bool(self._state.suggestions or self._state.files)

    def _dropdown(self, key: str) -> bool:
        """Handle a key the open dropdown owns. Returns whether it did.

        The dropdown takes the arrows and Tab only while it is open; history
        and ordinary editing get them back the moment it closes.
        """
        state = self._state
        count = len(state.suggestions) or len(state.files)

        if key == Key.UP:
            state.selected = (state.selected - 1) % count
        elif key == Key.DOWN:
            state.selected = (state.selected + 1) % count
        elif key == Key.TAB:
            self._accept_suggestion()
        elif key == Key.ESCAPE:
            state.suggestions = ()
            state.files = ()
            self._palette = False
        else:
            return False
        return True

    def _accept_suggestion(self) -> None:
        """Fill the input from the dropdown.

        For commands with nothing highlighted this completes to the longest
        shared prefix rather than the first match — `/s` stays `/s` while
        `/sessions` and `/status` both match, instead of silently choosing one.
        A file has no such notion, so the highlighted one is taken.
        """
        state = self._state

        if state.files:
            self._replace_fragment(state.files[state.selected])
            return

        if state.selected:
            text = state.suggestions[state.selected].name
        else:
            text = common_prefix(state.suggestions) or state.suggestions[0].name

        self._palette = False
        self._buffer = Buffer(text=text, cursor=len(text), history=self._buffer.history)
        self._sync()

    def _replace_fragment(self, path: str) -> None:
        """Swap the `@…` under the cursor for a chosen path."""
        found = file_fragment(self._buffer.text, self._buffer.cursor)
        if found is None:  # pragma: no cover - only reachable if state drifted
            return

        start, _ = found
        text = self._buffer.text
        replacement = f"@{path} "
        updated = text[:start] + replacement + text[self._buffer.cursor :]

        self._buffer = Buffer(
            text=updated,
            cursor=start + len(replacement),
            history=self._buffer.history,
        )
        self._sync()

    def _open_palette(self) -> None:
        """Show every command, whatever is in the input.

        A discovery affordance rather than a completion one: it does not filter
        by what has been typed, because the point is to answer "what can this
        do" from a blank prompt.
        """
        self._palette = True
        self._state.suggestions = COMMANDS
        self._state.files = ()
        self._state.selected = 0

    def _sync(self) -> None:
        """Mirror the buffer into the frame and recompute the dropdown."""
        state = self._state
        state.text = self._buffer.text
        state.cursor = self._buffer.cursor

        if self._palette:
            return

        previous = (state.suggestions, state.files)
        state.suggestions = complete(self._buffer.text)
        state.files = self._files_for(self._buffer) if not state.suggestions else ()

        if (state.suggestions, state.files) != previous:
            state.selected = 0

    def _files_for(self, buffer: Buffer) -> tuple[str, ...]:
        """Workspace paths matching the `@…` under the cursor."""
        found = file_fragment(buffer.text, buffer.cursor)
        if found is None:
            return ()
        return match_files(found[1], self._paths())

    def _paths(self) -> tuple[str, ...]:
        """The workspace file list, scanned once per session.

        Cached because `@` recomputes matches on every keystroke, and a repo
        scan per character would be felt. A file created mid-session is missed
        until the next one, which is the right trade for a picker.
        """
        if self._file_cache is None:
            listed = git_listed_files(self._settings.workspace)
            if listed is None:
                listed = [
                    path.relative_to(self._settings.workspace)
                    for path in self._settings.workspace.rglob("*")
                    if path.is_file() and not is_ignored(path.relative_to(self._settings.workspace))
                ]
            self._file_cache = tuple(sorted(path.as_posix() for path in listed))
        return self._file_cache

    # -- leader ------------------------------------------------------------

    def _leader_key(self, key: str) -> str:
        """The key after `^X`. Unrecognised keys cancel rather than act."""
        self._leader = False
        self._state.status = ""

        command = _LEADER.get(key)
        if command is None:
            return ""
        return QUIT if self._dispatch(command) else ""

    def _dispatch(self, line: str) -> bool:
        """Apply one command. Returns whether the session should end."""
        models = self._list_models(self._settings.host) if line.startswith("/model") else ()
        outcome = self._handle_command(line, self._settings, models)

        for text in outcome.lines:
            self._state.note(Level.NOTE, "", text.strip())

        if outcome.settings is not None:
            self._settings = outcome.settings
            self._state.workspace = str(outcome.settings.workspace)
            self._state.model = outcome.settings.model_name
            self._state.mode = outcome.settings.mode
            # The model client and the state database are both bound at
            # construction, so a change to either means a fresh agent.
            self._rewire()

        return outcome.quit

    # -- running -----------------------------------------------------------

    async def _turn(self, screen: Screen, request: str) -> None:
        """Run one request, repainting while it goes."""
        state = self._state
        state.note(Level.NOTE, "you", request)
        state.busy = True
        state.status = "starting"
        state.evidence = 0

        try:
            wiring = self._wired()
        except ModelUnavailable as exc:
            state.busy = False
            state.note(Level.FAIL, "model", str(exc))
            return

        view = RunView(request=request, workspace=state.workspace)
        unsubscribe = wiring.runs.subscribe(view.apply)
        work = asyncio.ensure_future(wiring.session.run(request))
        drained = 0

        try:
            while not work.done():
                drained = self._drain(view, drained)
                state.status = view.phase.value
                state.evidence = len(view.evidence)
                self._paint(screen)

                if read_key(FRAME_SECONDS) == Key.CTRL_C:
                    # The UI never stops anything itself: it asks the runtime,
                    # which owns cancellation and knows how to stop cleanly.
                    for run_id in wiring.runs.active():
                        wiring.runs.cancel(run_id, "cancelled from the interface")
                await asyncio.sleep(0)

            result = await work
        except Cancelled:
            state.note(Level.STOP, "stopped", "cancelled")
            return
        except ModelUnavailable as exc:
            state.note(Level.FAIL, "model", str(exc))
            return
        finally:
            unsubscribe()
            self._drain(view, drained)
            state.busy = False
            state.status = ""

        verified = exit_code(result) == 0
        state.note(
            Level.OK if verified else Level.FAIL,
            "complete" if verified else "blocked",
            result.stopped_because,
        )

    def _drain(self, view: RunView, drained: int) -> int:
        """Move newly-observed activity into the transcript."""
        self._state.transcript.extend(view.activity[drained:])
        return len(view.activity)

    # -- output ------------------------------------------------------------

    def _paint(self, screen: Screen) -> None:
        size = terminal_size()
        self._tick += 1
        self._state.spinner = SPINNER[self._tick % len(SPINNER)]

        lines = render_session(self._state, size.width, size.height, colour=True)
        screen.paint(lines)
        screen.place_cursor(*self._cursor(len(lines)))

    def _cursor(self, drawn: int) -> tuple[int, int]:
        """Where the terminal cursor belongs, one-indexed.

        The input line sits above the dropdown, so its row is counted back from
        the bottom of what was actually drawn rather than from the window size —
        a frame clipped by a short window would otherwise misplace it.
        """
        shown = min(len(self._state.suggestions), MAX_SUGGESTIONS)
        overflow = 1 if len(self._state.suggestions) > MAX_SUGGESTIONS else 0
        row = drawn - shown - overflow
        return row, 4 + self._state.cursor


__all__ = ["FRAME_SECONDS", "QUIT", "InteractiveSession"]
