"""The system prompt for simple mode.

Deliberately one short, POSITIVE block. The legacy path sends 49 bullet rules
across the system prompt and the state frame, and measurement on 2026-08-17
found the same four instructions repeated 3-4 times each - "do not claim
complete" appears four separate times. The loudest repeated signal a small model
receives there is *don't overstep*, and the behaviour matched: it inspected,
read, re-read, and never wrote.

Corrections do not belong here. The tool layer already returns a specific error
at the moment a call goes wrong, which is where a small model can actually act
on it; repeating those errors as standing prohibitions only dilutes the
instruction that says what to do.
"""
from __future__ import annotations

from pathlib import Path

SIMPLE_SYSTEM_PROMPT = """\
You are SHAMSU, a coding assistant working in {workspace}.

You can read, search and change files in that folder, and run commands there.
Use a tool when you need real information or need to change something. If the
question does not need one, just answer normally.

When you change code, check it works - run it, run its tests, or run the build.
Work in small steps: make one change, check it, then move to the next.

You are talking to one person over time. Earlier messages in this conversation
are real: refer back to them, and when they say "continue" or "next", carry on
from what you were doing.\
"""


def simple_system_prompt(workspace: Path) -> str:
    """Render the simple-mode system prompt for *workspace*."""
    return SIMPLE_SYSTEM_PROMPT.format(workspace=Path(workspace).as_posix())


BUILD_INSTRUCTION = """\
Read {prd} and build what it describes.

First write down the milestones you will work through, in order. Then implement
them one at a time: write the files for a milestone, check they work, say what
you finished, and stop. I will say "continue" when I want the next one.\
"""


def build_instruction(prd: str) -> str:
    """The one instruction `/build` seeds into an ordinary chat turn."""
    return BUILD_INSTRUCTION.format(prd=prd)
