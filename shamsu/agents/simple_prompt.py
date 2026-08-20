"""The system prompt for simple mode.

The words now live in `prompts/simple_system.md`, not in this file. That is
smallcode's arrangement - their skills and knowledge are markdown - and the
reason to copy it is that the prompt is the single most-edited thing in the
agent and the least code-like. A markdown file can be read, diffed and changed
by someone who does not want to open Python, and the sections are addressable
by name rather than by which `+` they were concatenated with.

This module is now the loader and the policy: which sections go out, in what
order, and under what condition.

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

import re
from functools import lru_cache
from pathlib import Path

PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "simple_system.md"

# Sections sent on every turn, in this order. The rest are added by condition.
ALWAYS = ("base", "act")

# Fallbacks, used only if the markdown is missing or unreadable - a packaging
# mistake must not leave the model with no instructions at all. Kept terse on
# purpose: this is a lifeboat, not a second copy to maintain.
_FALLBACK = {
    "base": (
        "You are SHAMSU, a coding assistant working in {workspace}.\n\n"
        "You can read, search and change files in that folder, and run commands "
        "there. Use a tool when you need real information or need to change "
        "something."
    ),
    "act": (
        "Act on what you were asked. If a task has several parts, carry on "
        "through them and say what you did at the end."
    ),
}

_SECTION = re.compile(r"^##\s+([a-z_]+)\s*$", re.MULTILINE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


@lru_cache(maxsize=1)
def _sections() -> dict[str, str]:
    """Parse the markdown into `{section name: text}`.

    Cached: the file does not change inside a run, and this is called once per
    turn. HTML comments are stripped - they are notes for whoever edits the
    file, and sending them would be paying tokens to tell the model why a line
    exists.
    """
    try:
        raw = PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        return dict(_FALLBACK)
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            raw = raw[end + 4 :]
    raw = _COMMENT.sub("", raw)
    found: dict[str, str] = {}
    matches = list(_SECTION.finditer(raw))
    for index, match in enumerate(matches):
        stop = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
        body = raw[match.end() : stop].strip()
        if body:
            found[match.group(1)] = body
    return found or dict(_FALLBACK)


def section(name: str) -> str:
    """One named section of the prompt, or ``""``."""
    return _sections().get(name, _FALLBACK.get(name, ""))


def simple_system_prompt(workspace: Path) -> str:
    """Render the simple-mode system prompt for *workspace*."""
    parts = [section(name) for name in ALWAYS]
    parts.append(section("recall"))
    parts.append(section("big_read"))
    parts.append(section("big_file"))
    if _graph_is_usable(workspace):
        parts.append(section("graph"))
    parts.append(_skill_index(workspace))
    prompt = (chr(10) * 2).join(part for part in parts if part)
    return prompt.replace("{workspace}", Path(workspace).as_posix())


def _skill_index(workspace: Path) -> str:
    """The one-line-per-skill index, when this workspace has any.

    Conditional for the same reason every other capability line is: a skill the
    model cannot see is one it will never call `use_skill` for, and a roster
    advertised into a workspace with no skills is a wasted line every turn.
    """
    try:
        from shamsu.agents.simple_chat import skill_index

        return skill_index(workspace)
    except Exception:  # noqa: BLE001 - never let the prompt fail to render
        return ""


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
