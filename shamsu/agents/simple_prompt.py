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

Sections are CONDITIONAL, which is smallcode's shape: they add the BoneScript
paragraph only for backend tasks, and advertise web tools only when browsing
is on. The reason is their issue #58 and it is the important part - *a small
model trusts this prose over the raw `tools` array*. Theirs refused research
tasks with "my tools are for code files only" while the web tools sat right
there in the schema list. So a capability not named here is one the model will
not use; and one named here that does not work is a wasted round. Name them,
but only when they are real.

One instruction here WAS unconditional and cost real work: "Work in small
steps: make one change, check it" presumes the task is a change. Asked to
"review the PRD and plan the next steps" the model wrote five backend files,
ran pip and pytest, hit the 24-round cap at 577s and delivered no plan (live
2026-08-18). It is now conditional, and answering is named as work in its own
right - still a statement of what to do, not a prohibition.
"""
from __future__ import annotations

from pathlib import Path

SIMPLE_SYSTEM_PROMPT = """\
You are SHAMSU, a coding assistant working in {workspace}.

You can read, search and change files in that folder, and run commands there.
Use a tool when you need real information or need to change something. If the
question does not need one, just answer normally.

When someone asks you to review, explain, or plan, the answer IS the work: say
what you found and what you would do next. Change files when you are asked to
change something.

When you are changing code, check it works - run it, run its tests, or run the
build - and work in small steps: make one change, check it, then move on.

You are talking to one person over time. Earlier messages in this conversation
are real: refer back to them, and when they say "continue" or "next", carry on
from what you were doing.\
"""

# Named in prose because a small model reads prose, not the schema list. Each
# is added only when the thing behind it actually works - see above.
RECALL_CAPABILITY = (
    chr(10) * 2
    + "You remember this project: memory_remember keeps a decision or a "
    + "gotcha, memory_load brings back what bears on the job, and "
    + "history_search finds anything said earlier, including turns you can no "
    + "longer see."
)

# A NUMBER, because "too big" is not something a 3B model can act on. This said
# "a file too big to write in one go" and named no limit, so the model decided
# for itself and decided wrong - one reply, whole file, cut off part-way.
#
# The number here is deliberately far stricter than the cap the tool enforces
# (~8,000 characters, about 200 lines). Prose guidance has to be memorable; the
# tool is what has to be exact. Sixty lines of dense code is ~2,500 characters,
# so a model that follows this never reaches the hard refusal at all - the gap
# is belt-and-braces, and it is what smallcode does too.
BIG_FILE_CAPABILITY = (
    chr(10) * 2
    + "Keep every write_file and append_file under 60 lines. For anything "
    + "larger: write_file the first 60 lines, then append_file each following "
    + "section, 60 lines at a time. To change part of an existing file, "
    + "patch_file."
)

GRAPH_CAPABILITY = (
    chr(10) * 2
    + "This workspace is indexed: graph_search finds a symbol without reading "
    + "files, explain_symbol says who calls it."
)


def simple_system_prompt(workspace: Path) -> str:
    """Render the simple-mode system prompt for *workspace*."""
    prompt = SIMPLE_SYSTEM_PROMPT.format(workspace=Path(workspace).as_posix())
    prompt += RECALL_CAPABILITY + BIG_FILE_CAPABILITY
    if _graph_is_usable(workspace):
        prompt += GRAPH_CAPABILITY
    return prompt


def _graph_is_usable(workspace: Path) -> bool:
    """Whether this workspace actually has a code graph worth advertising."""
    try:
        from shamsu.tools.codebase_memory import CodebaseMemoryAdapter

        return bool(CodebaseMemoryAdapter().is_available(Path(workspace)))
    except Exception:  # noqa: BLE001 - never let the prompt fail to render
        return False


BUILD_INSTRUCTION = """\
Read {prd} and build what it describes.

First write down the milestones you will work through, in order. Then implement
them one at a time: write the files for a milestone, check they work, say what
you finished, and stop. I will say "continue" when I want the next one.\
"""


def build_instruction(prd: str) -> str:
    """The one instruction `/build` seeds into an ordinary chat turn."""
    return BUILD_INSTRUCTION.format(prd=prd)
