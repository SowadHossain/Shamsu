"""The skill for this request, chosen by the harness rather than by the model.

`use_skill` has been in the tool roster since 2026-08-20 and its index has
shipped in every system prompt since. Across every session logged to
2026-08-28 - two workspaces, eleven turns, a 9B and a 7B - it was called **zero
times**. The capability was real, wired, documented and advertised, and it only
ever existed if the model thought to reach for it.

That is the same shape as four other findings in this harness, and memory
already settled it: `run()` writes an evidence note at the end of every turn
because `memory_remember` "exists ONLY when the model volunteers a tool call -
and it does not". `render_memory` then puts what it noted back into the window
without anybody asking. This is that, pointed at skills.

**One skill, never a shelf of them.** A small model given two documents about
how to work has been given a choice it did not ask for and cannot make well.
The best match goes in; the rest stay behind `use_skill`, which keeps working
for the model that does ask.

**Specific beats general.** Every skill worth having declares triggers, and the
generic ones - `developer` answers to "build", "create", "fix" - would win every
coding request by sheer breadth and crowd out the one that actually knows
something. So a trigger scores by how much it says: a two-word phrase that
matched is worth more than a verb that did.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shamsu.context.budget import count_tokens

__all__ = [
    "SKILL_BUDGET_RATIO",
    "Situation",
    "best_skill",
    "render_skill",
    "situation_skill_name",
    "skill_for_turn",
]

#: Share of the window one injected skill may take. Still below
#: `summary_budget`'s sixteenth: a skill is guidance about the job and must never
#: outweigh the conversation about the job. Empty for a workspace with no
#: matching skill, which is most turns.
#:
#: 0.04 and not more, which is 327 tokens of an 8k window - see
#: `SMALL_MODEL_BUDGET` in the tests. A skill longer than that arrives cut on the
#: machines these skills exist to help, and the tail that gets cut is where the
#: "do not do X" rules live. Every bundled skill is written to fit it; a skill
#: that will not fit wants to be shorter, not the budget bigger.
SKILL_BUDGET_RATIO = 0.04

#: A single word that matched is weak evidence - "test" appears in half of all
#: requests. A phrase is strong evidence. Scored by the number of words in the
#: trigger so this stays a property of the skill's own metadata rather than a
#: hand-maintained list of which triggers are too generic.
_WEIGHT_PER_TRIGGER_WORD = 2.0
_WEIGHT_NAME = 5.0
_WEIGHT_TAG = 1.0

#: Below this nothing is injected. One bare verb ("fix") scores 2.0 and must not
#: be enough; a two-word trigger, or a verb plus a tag, is.
MIN_SCORE = 3.0


def _mentions(text: str, phrase: str) -> bool:
    """Does *text* contain *phrase* as whole words?

    Word boundaries, not `in`. A plain substring test made `ui` match the middle
    of "b**ui**ld" - measured against the live 2026-08-31 asteroid session, where
    "Let's build an asteroid game with multiple levels and sound effects" scored
    `ui-designer` at 3.0 and would have injected a page-layout skill into a game
    build. `test` matches "la**test**" the same way, and `part` matches
    "a**part**".

    `re.escape` because a trigger is author-supplied text, not a pattern, and
    one containing `c++` or `.net` must match itself rather than raise.
    """
    if not phrase:
        return False
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def _score(skill: Any, request: str) -> float:
    """How much this skill has to say about *request*."""
    text = (request or "").lower()
    if not text.strip():
        return 0.0
    score = 0.0
    name = str(getattr(skill, "name", "") or "").lower()
    if _mentions(text, name):
        score += _WEIGHT_NAME
    for trigger in getattr(skill, "triggers", ()) or ():
        phrase = str(trigger or "").strip().lower()
        if _mentions(text, phrase):
            score += _WEIGHT_PER_TRIGGER_WORD * len(phrase.split())
    for tag in getattr(skill, "tags", ()) or ():
        word = str(tag or "").strip().lower()
        if _mentions(text, word):
            score += _WEIGHT_TAG
    return score


def best_skill(skills: list[Any], request: str) -> Any | None:
    """The one skill worth putting in the window, or ``None``.

    Ties break on the LONGER instructions, which is a proxy for the more
    specific skill: `react-vite` and `ui-designer` both answer to a dashboard
    request, and the one with more to say about it is the better answer.
    """
    ranked = [(_score(skill, request), skill) for skill in skills]
    ranked = [(score, skill) for score, skill in ranked if score >= MIN_SCORE]
    if not ranked:
        return None
    return max(
        ranked,
        key=lambda pair: (pair[0], len(str(getattr(pair[1], "instructions", "") or ""))),
    )[1]


#: Writes to ONE path before the file counts as being built in pieces. Three,
#: because two is an edit and a fix; the asteroid run made EIGHT consecutive
#: `append_file` calls against `asteroid/game.js`, taking it from 76 lines to
#: 581 and spending most of a 24-step budget doing it.
WRITES_BEFORE_SURGERY = 3

#: Identical failures before the model is told how to satisfy the thing it
#: keeps failing. Two, because the first is a mistake and the second is a
#: pattern. The same run called `contract_assert_pass` three times and got the
#: same "needs evidence: what did you run, and what did it say?" every time.
FAILURES_BEFORE_HELP = 2

#: Kept: the old name for the same threshold.
FAILURES_BEFORE_TESTING = FAILURES_BEFORE_HELP

#: A fact the harness ALREADY KNOWS about this turn, and the skill that speaks
#: to it. This is the whole idea: the request said "build an asteroid game
#: with multiple levels and sound effects", and no matcher on earth reads
#: `large-file-surgery` out of that sentence - but eight appends to one file
#: says it plainly. What the work is only becomes clear once it is under way.
SITUATION_SKILLS: tuple[tuple[str, str], ...] = (
    ("one file written over and over", "large-file-surgery"),
    ("an assertion refused for want of evidence", "qa-tester"),
    ("the same tool failing the same way", "debugger"),
)

#: A failing assertion is not a bug to diagnose - it is a claim the model has
#: not backed up, and `qa-tester` is the skill that says what backing it up
#: means. Anything else failing the same way twice is a bug, and `debugger`
#: is the skill for finding out why rather than trying again harder.
_EVIDENCE_TOOLS = ("contract_assert", "contract_status")


@dataclass(frozen=True)
class Situation:
    """What this turn has DONE so far, as the loop already records it.

    Both fields are kept by `SimpleChatLoop` for other reasons - the evidence
    note and the trust tracker - so reading them here costs nothing and adds
    no new bookkeeping to the round loop.
    """

    #: Every path written this turn, in order, repeats included.
    writes: tuple[str, ...] = field(default=())
    #: `(tool, first line of the message)` for every failure this turn.
    failures: tuple[tuple[str, str], ...] = field(default=())

    def busiest_file(self) -> int:
        """How many times the most-written path was written."""
        if not self.writes:
            return 0
        return Counter(path.strip().lower() for path in self.writes if path.strip()).most_common(
            1
        )[0][1]

    def worst_repeat(self) -> tuple[str, int]:
        """`(tool, count)` for the failure this turn keeps repeating."""
        if not self.failures:
            return ("", 0)
        counted = Counter(
            (str(tool).strip().lower(), str(message).strip().lower()[:200])
            for tool, message in self.failures
        )
        (tool, _message), count = counted.most_common(1)[0]
        return (tool, count)


def situation_skill_name(situation: Situation | None) -> str:
    """The skill this SITUATION asks for, or ``""``.

    Ordered by how much the evidence says. A file being built in pieces is a
    statement about the whole rest of the turn; a repeated failure is about
    the one thing that keeps failing.
    """
    if situation is None:
        return ""
    if situation.busiest_file() >= WRITES_BEFORE_SURGERY:
        return "large-file-surgery"
    tool, count = situation.worst_repeat()
    if count >= FAILURES_BEFORE_HELP:
        if any(tool.startswith(prefix) for prefix in _EVIDENCE_TOOLS):
            return "qa-tester"
        return "debugger"
    return ""


def skill_for_turn(
    workspace: Path,
    request: str,
    budget_tokens: int,
    *,
    situation: Situation | None = None,
    already_used: tuple[str, ...] = (),
) -> tuple[str, str]:
    """`(name, text)` for the skill worth putting in the window right now.

    The situation is asked FIRST and the request second. A request is what
    someone thought the job was before starting it; the situation is what the
    job turned out to be, and when they disagree the second one is right.

    `already_used` is what this turn has injected before, so a skill arrives
    once and does not re-enter the window every round for the rest of the turn.
    """
    if budget_tokens <= 0:
        return ("", "")
    offerable = _offerable(workspace)
    if not offerable:
        return ("", "")
    by_name = {str(getattr(skill, "name", "")): skill for skill in offerable}

    wanted = situation_skill_name(situation)
    skill = by_name.get(wanted) if wanted else None
    if skill is None:
        skill = best_skill(offerable, request)
    if skill is None:
        return ("", "")
    name = str(getattr(skill, "name", ""))
    if name in already_used:
        return ("", "")
    return (name, _render(skill, budget_tokens))


def _offerable(workspace: Path) -> list[Any]:
    """The skills this workspace could plausibly use, read fresh.

    Fresh, deliberately: the stack filter is keyed on the file extensions
    PRESENT, and the asteroid workspace was empty when the turn began - so
    `ui-designer` was filtered out at the moment of choosing and became
    applicable four tool calls later, when `index.html` existed.
    """
    try:
        from shamsu.agents.simple_chat import _skill_catalog, _skills_worth_offering

        catalog = _skill_catalog(workspace)
        return _skills_worth_offering(list(catalog.sorted_skills()), workspace)
    except Exception:  # noqa: BLE001 - a skill must never end a turn
        return []


def render_skill(workspace: Path, request: str, budget_tokens: int) -> str:
    """The matched skill's instructions for this turn, or ``""``.

    Budgeted twice: by the skill's own `context_budget_tokens`, which is the
    author saying how much of it is worth reading, and by *budget_tokens*, which
    is this window saying how much it can afford. The smaller wins.
    """
    if budget_tokens <= 0 or not (request or "").strip():
        return ""
    # The same stack filter the index uses: a `react-vite` skill in a Python
    # project is not a match however well its triggers read.
    skill = best_skill(_offerable(workspace), request)
    if skill is None:
        return ""
    return _render(skill, budget_tokens)


def _render(skill: Any, budget_tokens: int) -> str:
    """One skill, cut to fit. The only place a skill becomes prompt text."""
    body = str(getattr(skill, "instructions", "") or "").strip()
    if not body:
        return ""
    cap = min(int(budget_tokens), int(getattr(skill, "context_budget_tokens", 0) or budget_tokens))
    if count_tokens(body) > cap:
        # Cut on a line, not mid-sentence: a truncated instruction that reads
        # like a whole one is worse than a short one that admits it stopped.
        kept: list[str] = []
        spent = 0
        for line in body.splitlines():
            cost = count_tokens(line) + 1
            if spent + cost > cap:
                break
            kept.append(line)
            spent += cost
        body = "\n".join(kept).rstrip()
        if not body:
            return ""
        body += f"\n... (call use_skill with '{getattr(skill, 'name', '')}' for the rest)"
    return (
        f"How this project does {getattr(skill, 'name', 'this')} "
        "(from a skill, because your request matched it):\n" + body
    )
