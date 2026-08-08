"""Colour, in one place.

Two renderers draw the same run -- the full-screen one in `render` and the
line-oriented one the interactive session uses -- and they must not develop
separate colour vocabularies. Everything either of them paints comes from here.

**Colour carries meaning, and never carries it alone.** Green means verified
evidence and nothing else; red means blocked or failed; blue names a tool,
because tools are the only things that can produce evidence. Every line that is
coloured also has a glyph saying the same thing, so the display survives a pipe,
a mono terminal, `--no-colour`, and a reader who cannot separate red from green.

The palette is deliberately small and deliberately not the terminal's defaults:
these are the 256-colour codes closest to the design, which render consistently
across Windows Terminal, iTerm and a bare xterm without assuming truecolour.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

from shamsu.interfaces.enums import Phase
from shamsu.ui.view import Level

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"

#: The six semantic roles. Everything drawn picks one of these; there is no
#: seventh colour for "looks nice here".
AMBER = "\x1b[38;5;179m"  # SHAMSU itself: the prompt, the spinner, the brand
GREEN = "\x1b[38;5;108m"  # verified evidence, passed steps, completion
RED = "\x1b[38;5;167m"  # blocked, failed, missing evidence
BLUE = "\x1b[38;5;110m"  # tool names
VIOLET = "\x1b[38;5;140m"  # the model: its name, its decisions, its reasoning
GREY = "\x1b[38;5;245m"  # chrome, timings, paths -- everything not the point

#: Activity level -> colour. `Level.STEP` is a phase boundary, which is
#: structure rather than outcome, so it takes the brand colour rather than
#: borrowing green from evidence.
LEVEL_COLOUR: dict[str, str] = {
    Level.STEP: AMBER,
    Level.OK: GREEN,
    Level.FAIL: RED,
    Level.NOTE: GREY,
    Level.STOP: AMBER,
}

LEVEL_GLYPH: dict[str, str] = {
    Level.STEP: "▸",
    Level.OK: "●",
    Level.FAIL: "✗",
    Level.NOTE: "·",
    Level.STOP: "■",
}

PHASE_COLOUR: dict[Phase, str] = {
    Phase.INSPECT: BLUE,
    Phase.PLAN: VIOLET,
    Phase.AUTHOR: AMBER,
    Phase.VERIFY: GREEN,
    Phase.REPAIR: RED,
    Phase.COMPLETE: GREEN,
}


def paint(text: str, colour: str, enabled: bool = True) -> str:
    """Wrap `text` in `colour`, or return it untouched."""
    if not enabled or not colour or not text:
        return text
    return f"{colour}{text}{RESET}"


def supports_colour(stream: TextIO | None = None) -> bool:
    """Whether to emit escapes at all.

    Honours the `NO_COLOR` convention and `TERM=dumb`, and refuses to colour
    anything that is not a terminal -- piping a run into a file or a grep should
    produce text, not escape sequences. Windows Terminal and modern conhost
    handle SGR natively, so there is no platform gate beyond being a tty.
    """
    target = stream if stream is not None else sys.stdout

    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM", "").lower() == "dumb":
        return False

    try:
        return bool(target.isatty())
    except (AttributeError, ValueError):
        return False


def activity_line(level: str, label: str, detail: str, *, colour: bool = True) -> str:
    """One activity line, coloured, for the line-oriented renderer.

    The glyph and the label take the level's colour; the detail is split so the
    tool name reads as a tool and its subject reads as a path. The trailing
    timing is dimmed because it is context, not content.
    """
    glyph = paint(LEVEL_GLYPH.get(level, "·"), LEVEL_COLOUR.get(level, ""), colour)
    painted_label = paint(f"{label:<9}", LEVEL_COLOUR.get(level, GREY), colour)
    return f"  {glyph} {painted_label} {_detail(detail, colour)}".rstrip()


def _detail(detail: str, colour: bool) -> str:
    """Colour the parts of a detail line that mean different things.

    Tool details arrive as `tool.name  subject  extras` (see
    `runtime.session._describe_call`). Anything that is not in that shape is
    left alone rather than guessed at.
    """
    if not colour or not detail:
        return detail

    head, separator, tail = detail.partition("  ")
    if not separator or "." not in head or " " in head:
        return detail

    parts = [paint(head, BLUE, colour)]
    for chunk in tail.split("  "):
        if not chunk:
            continue
        if chunk.startswith("✓"):
            parts.append(paint(chunk, GREEN, colour))
        elif chunk.startswith("—"):
            parts.append(paint(chunk, RED, colour))
        elif chunk.endswith("s") and chunk[:-1].replace(".", "", 1).isdigit():
            parts.append(paint(chunk, GREY, colour))
        else:
            parts.append(paint(chunk, GREY, colour))
    return "  ".join(parts)


__all__ = [
    "AMBER",
    "BLUE",
    "BOLD",
    "DIM",
    "GREEN",
    "GREY",
    "LEVEL_COLOUR",
    "LEVEL_GLYPH",
    "PHASE_COLOUR",
    "RED",
    "RESET",
    "VIOLET",
    "activity_line",
    "paint",
    "supports_colour",
]
