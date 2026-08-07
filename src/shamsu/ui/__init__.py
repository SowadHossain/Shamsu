"""The terminal interface.

Three modules, split so that two of them are pure:

* `view` — what is known about a run, updated by folding `RunEvent`s. No I/O.
* `render` — `(view, width, height) -> lines`. A pure function of its input,
  including the clock, so a frame can be asserted on without a terminal.
* `terminal` — the only module that touches a file descriptor.

v1's CLI was 18,729 lines, 17,411 of them in `repl.py`, with display, input,
session management, and agent control in one object. Nothing in it could be
tested without driving a terminal, so in practice none of it was. This split is
the whole lesson.

The interface observes the runtime through `RunController.subscribe` and never
the other way round. Nothing in `runtime/` knows a UI exists.
"""

from shamsu.ui.render import render
from shamsu.ui.view import Activity, Level, RunView

__all__ = ["Activity", "Level", "RunView", "render"]
