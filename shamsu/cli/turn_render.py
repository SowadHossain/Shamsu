"""The CLI's renderer over the turn stream.

The CLI is the reference surface - what it prints is the definition of
"everything that happened" - so this file must not decide anything the other
renderers do not also see. `self.lines` is that contract: one entry per body
line, in order, compared against the Telegram card and `log-summary.md` by
`tests/test_turn_stream_parity.py`. Everything below changes how a line is
PAINTED, never which lines exist.

What it used to be was one call:

    self.console.print(f"[dim]{event.text}[/dim]")

Every action in the same grey. A successful read and a failed `run_tests`
looked identical, and a live 3B run that called `contract_status` eight times
in a row was eight identical lines you had to count to notice. Three things
follow from that, and they are the whole of this module:

**A row is printed when the tool FINISHES, not when it starts.** Duration is
the most useful thing on the line and it does not exist yet at call time. The
call is remembered, the result completes it. A call with no result - a crash, a
cancel - is flushed unfinished rather than lost.

**Repeats collapse.** A run of identical calls prints one row, `x8`, when the
run ends. It is deliberately NOT an in-place rewrite of a printed line: a
`console.status` spinner is a live region pinned below the cursor, so the last
line on screen is the spinner and not the row - cursor-relative editing would
corrupt it. The live count goes on the spinner instead, which is already a
live element and already safe. You watch the number climb there and get one
clean line in the scrollback.

**Nothing owns the frame.** No `rich.Live` layout, no pinned input. A raw
`msvcrt` keystroke thread reads stdin during a turn so the user can steer
mid-run (`shamsu/agents/simple_feedback.py`); a display that repaints the whole
screen from another thread would fight it, and on Windows that is the fight you
lose. `console.status` coexists because it is transient and unpinned.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from rich.markup import escape
from rich.syntax import Syntax

from shamsu.cli.prompt_label import prompt_label
from shamsu.runtime.turn_stream import TurnEvent, body_kinds

#: Row icons, matching `log-summary.md` so one turn reads the same in the
#: terminal and in the file afterwards. Geometric rather than emoji: emoji
#: width is unreliable in a Windows console.
ICON_MODEL = "◆"
ICON_TOOL = "▤"
ICON_APPROVAL = "⚑"
ICON_PATCH = "✎"
ICON_COMMAND = "▶"
ICON_PASS = "✓"
ICON_FAIL = "✗"
ICON_NOTE = "·"

#: Tools whose name reads better as a verb. Same table as the log's.
_TOOL_VERBS = {
    "read_file": "Reading",
    "read_symbol": "Reading symbol",
    "outline": "Outlining",
    "write_file": "Writing",
    "append_file": "Appending to",
    "patch_file": "Editing",
    "replace_symbol": "Replacing symbol in",
    "search_files": "Searching",
    "find_file": "Finding",
    "run_command": "Running",
    "run_tests": "Testing",
    "delete_file": "Deleting",
}

_WRITERS = frozenset(
    {"write_file", "patch_file", "append_file", "replace_symbol", "delete_file"}
)

#: Diff lines shown inline under a write. The full diff is in
#: `log-detailed.md`; this is the glance that tells you it did the right thing.
MAX_DIFF_LINES = 8

#: Reasoning shown inline. A trace runs to thousands of characters and the
#: terminal is not where you read it - `log-detailed.md` keeps all of it.
MAX_REASONING_CHARS = 400


class CliTurnRenderer:
    """Paint a turn on a rich console.

    `echo_surface`, when set, prints a `shamsu (<title>) <surface>> <prompt>`
    line at `turn.start`, built by the shared `prompt_label`. The local REPL
    leaves it unset - it already shows the prompt the user typed - while a
    remote turn sets it, so a turn started elsewhere reads on the desktop like
    any other terminal turn instead of arriving as a coloured panel.
    """

    def __init__(
        self,
        console: Any,
        *,
        status_updater: Callable[[str], None] | None = None,
        verbosity: str = "normal",
        echo_surface: str = "",
        echo_title: str = "",
    ) -> None:
        self.console = console
        self.status_updater = status_updater
        self.verbosity = verbosity
        self.echo_surface = echo_surface
        self.echo_title = echo_title
        self._body = body_kinds(verbosity)
        #: Every body line this renderer printed, in order. The parity test
        #: compares this against the Telegram card's list and the session log's.
        #: Presentation below may enrich a row; it may never add or drop one.
        self.lines: list[str] = []
        #: The call waiting for its result, so the row can carry a duration.
        self._pending: dict[str, Any] | None = None
        #: The last row PAINTED, and how many times its tool has run since.
        #: Kept across intervening model lines - see `_settle`.
        self._last_tool = ""
        self._last_row = ""
        self._repeats = 1
        #: The last status text, so the spinner can keep saying it while the
        #: repeat counter changes underneath.
        self._status_text = ""
        self._meter = ""

    # -- the bus -----------------------------------------------------------

    def __call__(self, event: TurnEvent) -> None:
        handler = getattr(self, f"_on_{event.kind.replace('.', '_')}", None)
        if handler is not None:
            handler(event)
            return
        self._on_body(event)

    def _on_status(self, event: TurnEvent) -> None:
        # The spinner owns the status; it REPLACES rather than appends, and
        # printing it as well would fill the scrollback with ticks.
        self._status_text = event.text
        self._meter = _meter_of(event.data)
        self._paint_status()

    def _on_turn_start(self, event: TurnEvent) -> None:
        self._reset()
        if self.echo_surface and event.text:
            label = prompt_label(self.echo_title, self.echo_surface, trailing_space=False)
            self.console.print(f"[bold]{escape(label)}[/bold] {escape(event.text)}")

    def _on_tool_call(self, event: TurnEvent) -> None:
        """Open a call. It is painted when its result lands, not before.

        Still recorded in `self.lines` here, on this event, so the parity
        contract is unaffected by the fact that painting is deferred."""
        if event.kind in self._body and event.text:
            self.lines.append(event.text)
        self._settle()
        self._pending = {
            "name": str(event.data.get("tool") or ""),
            "text": event.text,
            "started": time.monotonic(),
        }

    def _on_tool_result(self, event: TurnEvent) -> None:
        """Close the open call and settle it immediately.

        Immediately, rather than waiting for the next row: the next row is
        usually the model's reply to this result, which on a local 7B is forty
        seconds away. Holding the row that long means running a tool and
        watching nothing happen.
        """
        if event.kind in self._body and event.text:
            self.lines.append(event.text)
        if self._pending is None:
            self._pending = {
                "name": str(event.data.get("tool") or ""),
                "text": event.text,
            }
        self._pending.update(
            {"ok": bool(event.data.get("ok", True)), "data": dict(event.data)}
        )
        self._settle()

    def _on_reasoning(self, event: TurnEvent) -> None:
        """A trace, dimmed and quoted, so it never competes with the actions."""
        if event.kind in self._body and event.text:
            self.lines.append(event.text)
        text = str(event.data.get("text_full") or event.text or "").strip()
        if not text:
            return
        self._flush()
        for line in _clip(text, MAX_REASONING_CHARS).splitlines():
            self.console.print(f"    [dim italic]{escape(line)}[/dim italic]")

    def _on_approval(self, event: TurnEvent) -> None:
        """Loud, and never dim. This is the one row that wants an answer."""
        if event.kind in self._body and event.text:
            self.lines.append(event.text)
        # Deliberately does NOT flush. The approval fires from INSIDE the tool
        # that is still running - `patch_file` asks before it writes - so
        # closing the pending row here printed it once unfinished at 0ms and
        # again when the result arrived. The approval row names its own action
        # and its own file, so it reads perfectly well ahead of the row it
        # gated.
        data = event.data
        action = escape(str(data.get("action_type") or "action"))
        target = str(data.get("target") or "")
        on = f" on [bold]{escape(target)}[/bold]" if target else ""
        if str(data.get("phase") or "") == "requested":
            self.console.print(
                f"  [bold yellow]{ICON_APPROVAL} approval needed[/bold yellow] "
                f"— [bold]{action}[/bold]{on}"
            )
            return
        approved = bool(data.get("approved"))
        verdict = (
            "[bold green]approved[/bold green]"
            if approved
            else "[bold red]DENIED[/bold red]"
        )
        self.console.print(
            f"  [yellow]{ICON_APPROVAL}[/yellow] {action}{on} — {verdict}"
        )

    def _on_turn_end(self, event: TurnEvent) -> None:
        """The verdict badge, and whatever was still open."""
        self._flush()
        data = event.data
        # Absent status is not failure. The loop always sends one; a caller
        # that does not is not making a claim, and painting a red badge on
        # silence would be inventing a verdict.
        ok = str(data.get("status") or "done") == "done" and not data.get("error")
        badge = (
            "[bold black on green] ✓ SUCCESS [/]"
            if ok
            else "[bold white on red] ✗ FAILED [/]"
        )
        self.console.print(f"\n{badge} [dim]{escape(event.text)}[/dim]\n")

    def _on_error(self, event: TurnEvent) -> None:
        self._flush()
        if event.text:
            self.console.print(f"  [bold red]{ICON_FAIL} {escape(event.text)}[/bold red]")

    def _on_body(self, event: TurnEvent) -> None:
        """Anything with no richer treatment: an activity line, as before."""
        if event.kind not in self._body or not event.text:
            return
        # `_settle`, not `_flush`: an activity line is the model talking BETWEEN
        # two calls to the same tool, and closing the streak here is exactly
        # what stopped the contract loop from ever collapsing.
        self._settle()
        self.lines.append(event.text)
        if self._repeats > 1:
            # Inside a run of identical calls. These are the model's replies to
            # a tool that keeps returning the same thing, and printing them is
            # how eight suppressed rows still cost eight lines. Recorded above
            # for parity; not painted.
            return
        self.console.print(f"  [dim]{escape(event.text)}[/dim]")

    # -- rows --------------------------------------------------------------

    def _settle(self) -> None:
        """Paint the open call - or, if it repeats the last one, swallow it.

        A repeat is the same tool as the last row PAINTED, not the last event
        seen. That distinction is the whole feature: live 2026-08-21 a 3B ran
        `contract_status` eight times and every one of them had a "model
        responded" line between it and the next, so a rule that only collapsed
        back-to-back events would never once have fired on the run it was
        written for.

        The first call prints in full and immediately. Repeats print nothing
        and climb a counter on the spinner. The group is closed by
        `_close_streak` when a DIFFERENT tool runs, or when the turn ends.
        """
        pending, self._pending = self._pending, None
        if pending is None:
            return
        name = str(pending.get("name") or "")
        if name and name == self._last_tool:
            self._repeats += 1
            self._paint_status()
            return
        self._close_streak()
        data = pending.get("data") or {}
        row = _row_of(data, pending.get("text") or "", name=name)
        elapsed = data.get("duration_ms")
        if elapsed is None and pending.get("started") and data:
            elapsed = (time.monotonic() - pending["started"]) * 1000
        self._print_row(row, data, elapsed=elapsed)
        self._last_tool = name
        self._last_row = row
        self._repeats = 1

    def _close_streak(self) -> None:
        """Say how many times the last row actually ran, if more than once."""
        repeats, self._repeats = self._repeats, 1
        if repeats > 1 and self._last_row:
            self.console.print(
                f"  {self._last_row} [bold yellow]x{repeats}[/bold yellow]"
            )
        self._last_tool = ""
        self._last_row = ""

    def _flush(self) -> None:
        """Settle whatever is open and close any run of repeats."""
        self._settle()
        self._close_streak()

    def _print_row(
        self, row: str, data: dict[str, Any], elapsed: float | None = None
    ) -> None:
        if elapsed is None:
            elapsed = data.get("duration_ms")
        suffix = ""
        if data.get("ok") is False:
            # Before the duration, not after it. How long a failure took is the
            # least interesting thing about it.
            suffix += " [bold red]FAILED[/bold red]"
            note = str(data.get("message") or "")
            if note:
                # Short enough to sit on the row beside the tool name at a
                # normal width. The whole message is in `log-detailed.md`.
                suffix += f" [red]{escape(_clip(note, 70, one_line=True))}[/red]"
        if elapsed is not None:
            suffix += f" [dim]· {_duration(elapsed)}[/dim]"
        self.console.print(f"  {row}{suffix}")
        self._print_diff(str(data.get("diff") or ""))

    def _print_diff(self, diff: str) -> None:
        """A glance at what changed. The whole diff is in `log-detailed.md`."""
        if not diff.strip():
            return
        # `---`/`+++`/`@@` are unified-diff furniture: they name the file you
        # are already looking at and cost three of the eight lines on offer.
        shown = [
            line
            for line in diff.splitlines()
            if line[:1] in {"+", "-"}
            and not line.startswith(("---", "+++"))
        ][:MAX_DIFF_LINES]
        if not shown:
            return
        self.console.print(
            Syntax(
                "\n".join(shown),
                "diff",
                theme="ansi_dark",
                background_color="default",
                word_wrap=True,
                padding=(0, 0, 0, 4),
            )
        )

    # -- the spinner -------------------------------------------------------

    def _paint_status(self) -> None:
        if self.status_updater is None:
            return
        parts = [self._status_text or "working"]
        if self._repeats > 1 and self._last_tool:
            parts[0] = f"{self._last_tool} x{self._repeats}"
        if self._meter:
            parts.append(self._meter)
        self.status_updater(" | ".join(part for part in parts if part))

    def _reset(self) -> None:
        self._pending = None
        self._last_tool = ""
        self._last_row = ""
        self._repeats = 1
        self._status_text = ""
        self._meter = ""


# -- helpers ---------------------------------------------------------------


def _row_of(data: dict[str, Any], text: str, name: str = "") -> str:
    """One action, as a coloured title. Mirrors `log-summary.md`'s wording."""
    tool = str(data.get("tool") or name or "")
    if not tool:
        return f"[dim]{escape(text)}[/dim]"
    # `target` is what the emitter said this call was pointed at. When it did
    # not say - an older emitter, a salvaged call, a test - recover it from the
    # line's own text rather than printing a bare tool name: dropping "a.py"
    # from "read_file a.py" would make the richer row carry LESS than the dim
    # one it replaced.
    target = str(data.get("target") or "") or _subject_of(text, tool)
    icon, colour = _style_of(tool, bool(data.get("ok", True)))
    verb = _TOOL_VERBS.get(tool)
    if verb and target:
        return f"[{colour}]{icon} {verb}[/{colour}] [bold]{escape(target)}[/bold]"
    if target:
        return f"[{colour}]{icon} {escape(tool)}[/{colour}] [bold]{escape(target)}[/bold]"
    return f"[{colour}]{icon} {escape(tool)}[/{colour}]"


def _subject_of(text: str, tool: str) -> str:
    """Whatever a body line said after the tool's own name."""
    rest = (text or "").strip()
    if rest.startswith(tool):
        rest = rest[len(tool):]
    return rest.strip(" :-")


def _style_of(tool: str, ok: bool) -> tuple[str, str]:
    if not ok:
        return (ICON_FAIL, "red")
    if tool in _WRITERS:
        return (ICON_PATCH, "green")
    if tool in {"run_command", "run_tests"}:
        return (ICON_COMMAND, "magenta")
    return (ICON_TOOL, "cyan")


def _meter_of(data: dict[str, Any]) -> str:
    """`Ctx 68% (22.3k/32.8k) | Rnd 4/25`, from whatever the tick carried."""
    parts: list[str] = []
    ctx = str(data.get("ctx_text") or "")
    if ctx:
        pct = data.get("ctx_pct")
        colour = "red" if isinstance(pct, int) and pct >= 80 else (
            "yellow" if isinstance(pct, int) and pct >= 60 else "dim"
        )
        parts.append(f"[{colour}]{escape(ctx)}[/{colour}]")
    current, total = data.get("round"), data.get("max_rounds")
    if current and total:
        parts.append(f"[dim]rnd {current}/{total}[/dim]")
    return " | ".join(parts)


def _duration(ms: Any) -> str:
    if not isinstance(ms, (int, float)):
        return ""
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        seconds = ms / 1000
        return f"{seconds:.0f}s" if seconds >= 10 else f"{seconds:.1f}s"
    minutes, seconds = divmod(ms / 1000, 60)
    return f"{minutes:.0f}m {seconds:.0f}s"


def _clip(text: str, limit: int, *, one_line: bool = False) -> str:
    """Bound a payload for the terminal. `one_line` folds newlines away first.

    Explicit rather than inferred from the limit: a tool's error message must
    become one line because it sits ON a row, and a reasoning trace must keep
    its newlines because it is printed as a block. Deciding that from the size
    of the limit worked and read like a riddle.
    """
    text = " ".join((text or "").split()) if one_line else (text or "")
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."
