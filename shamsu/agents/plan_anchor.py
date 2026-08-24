"""Does this request need a plan, and what should the model be shown of it?

Adapted from smallcode `src/session/plan_tracker.js` - the *heuristic* and the
*re-injection*, not the machinery. Theirs carries a parallel plan object with
its own step cursor, parser and store. SHAMSU already has all of that under
another name: `agents/simple_contract.py` holds an ordered list of checkable
items, each `pending|passed|failed|skipped`, persisted per workspace, with a
done-guard that will not let the model claim finished while one is unresolved.

So the gap was never the data structure. It was two things:

1. **Nothing ever asked for a plan.** `contract_create` is offered and a model
   that does not think to call it never does.
2. **Nothing ever showed the plan again.** The contract sat on disk and reached
   the model only if it called `contract_status` - so the thing meant to keep a
   multi-step task on the rails was invisible unless the model remembered to
   ask, which is exactly what a model losing the thread stops doing. smallcode
   re-injects `ACTIVE PLAN (step 3 of 5)` on every single turn, and that is the
   half worth copying.

On smallcode's step cursor: their own code says *"Don't auto-advance - let the
model explicitly mark completion. Auto-advance leads to drift in long traces."*
Same conclusion here, reached the same way - which is why this leans on
`contract_assert_pass` rather than inventing a cursor to guess with.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Below this, a request is one thing. smallcode uses 300 for the "definitely
# multi-step" signal and a second, weaker test at 150 with three sentences.
LONG_REQUEST_CHARS = 300
SEVERAL_SENTENCES_CHARS = 150
SEVERAL_SENTENCES = 3

# The re-injected block is a tax on every turn of a long task, so it is capped.
# Enough for about a dozen steps; past that the model is being handed a document
# rather than an anchor.
MAX_ANCHOR_CHARS = 900

# Phrases that mean "this has parts". Kept to things that state SEQUENCE or
# MULTIPLICITY, not merely difficulty: "fix the hard bug" is one step.
_PLAN_HINTS = (
    re.compile(r"\b(then|after that|afterwards|finally|lastly|next,)\b", re.IGNORECASE),
    re.compile(r"\b(and then|followed by|once (that|it)'?s? done)\b", re.IGNORECASE),
    re.compile(r"\b(step by step|one at a time|in order|each of)\b", re.IGNORECASE),
    # No bare `port`: it matched "remember: the port is 8080" and asked a
    # twenty-six character note to write itself a plan. In this domain the noun
    # is everywhere and the verb is rare, and `migrate`/`rewrite` already carry
    # the intent. A false positive here costs a round AND anchors the model to
    # a plan it had no reason to write.
    re.compile(r"\b(refactor|migrate|rewrite|redesign|restructure)\b", re.IGNORECASE),
    re.compile(r"\b(implement|build|create) (a|an|the) \w+ (feature|system|module|api|page)\b", re.IGNORECASE),
    re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE),  # the user numbered it themselves
)

# A request that is explicitly asking for words, not work. Planning THIS would
# be planning to plan.
_ASKS_FOR_WORDS = re.compile(
    r"^\s*(what|why|how does|how do|explain|describe|tell me|is |are |does |can you tell)",
    re.IGNORECASE,
)


def plan_disabled() -> bool:
    """``SHAMSU_PLAN=0`` turns the whole anchor off, smallcode's switch."""
    return os.environ.get("SHAMSU_PLAN", "").strip().lower() in {"0", "false", "no", "off"}


def should_plan(request: str) -> bool:
    """Is this a job with parts, worth writing down before starting?

    Deliberately conservative. A false positive costs a round and anchors the
    model to a plan it wrote badly; a false negative costs nothing, because the
    model can still call `contract_create` itself. So the bar is evidence of
    SEQUENCE, not evidence of difficulty.
    """
    if plan_disabled():
        return False
    text = (request or "").strip()
    if not text or _ASKS_FOR_WORDS.match(text):
        return False
    if len(text) > LONG_REQUEST_CHARS:
        return True
    if any(hint.search(text) for hint in _PLAN_HINTS):
        return True
    if len(text) > SEVERAL_SENTENCES_CHARS:
        sentences = [s.strip() for s in re.split(r"[.!?]+", text) if len(s.strip()) > 10]
        return len(sentences) >= SEVERAL_SENTENCES
    return False


def ask_for_a_plan(request: str, workspace=None) -> str:
    """The one-shot instruction, or ``""``.

    Phrased as the call to make rather than as a rule to remember. "Plan before
    you start" has been in this project's prompts before and did not survive
    contact with a 3B; naming the tool and its argument does.
    """
    if not should_plan(request):
        return ""
    if workspace is not None and plan_phases(workspace):
        # There is already a plan with phases in it. Asking for a second list
        # beside it is how the phase numbers came apart in the first place -
        # the model wrote PLAN.md, then wrote a contract that matched nothing
        # in it, and "phase 2" then meant whichever the summary said last.
        return (
            "There is already a plan document with phases in this workspace. Do not "
            "write a second list beside it: call contract_from_plan with the phase "
            "number you are starting, so the contract IS that phase and its numbers "
            "keep meaning what the plan says." + chr(10) * 2
            + "Do that first, in this turn, then begin."
        )
    return (
        "This has several parts, so write them down before starting: call "
        "contract_create with a short title and one assertion per part, in the "
        "order you will do them. Then work through them, recording each with "
        "contract_assert_pass and the evidence that shows it.\n\n"
        "Do that first, in this turn, then begin."
    )


def anchor(rendered_contract: str) -> str:
    """The contract, re-shown as the turn's standing plan.

    smallcode's `ACTIVE PLAN (step 3 of 5)`. Without this the contract exists
    and is invisible: it reaches the model only when it calls
    `contract_status`, and a model that has lost the thread is exactly the one
    that stops asking.
    """
    body = (rendered_contract or "").strip()
    if not body:
        return ""
    if len(body) > MAX_ANCHOR_CHARS:
        body = body[:MAX_ANCHOR_CHARS].rstrip() + "\n  ... (call contract_status for the rest)"
    return "ACTIVE PLAN - you wrote this. Keep working through it:\n" + body


# -- the plan the model wrote as a FILE --------------------------------------
#
# `anchor` above re-shows the contract, and a contract is what SHAMSU means by
# a plan. It is not always what the USER means. Asked to "outline your approach
# in a PLAN.md file and wait for my approval", the model writes a document -
# and that document then reaches no prompt ever again.
#
# Live 2026-08-24, `demo-3/asteroid`. PLAN.md was written in turn 1 at 02:45 and
# never read back. Its real headings - `### Phase 3: Player Ship Module
# (player.js)` - appear in ZERO of the 24 surviving prompts. What the model saw
# instead, every turn, was the rolling conversation summary, which had invented
# a different decomposition and stamped it finished:
#
#     - Phase 1 complete: index.html, package.json, vite.config.js created and validated.
#     - Phase 2 complete: src/main.js, player.js, ... scaffolded.
#
# PLAN.md says Phase 1 is "Project Setup & Scaffolding" and Phase 2 is "Core
# Game Loop & Scene Setup (main.js)". Neither line matches it, and "validated"
# never happened - three commands succeeded all session. So when the user typed
# "lets proceed with phase 2", the model read "Phase 2 complete" as established
# fact and improvised. The plan was on disk the whole time.

#: Where a plan document lives, in the order worth trying. Root only: a plan is
#: something the user asked for by name, not something to go hunting for.
PLAN_FILENAMES = ("PLAN.md", "plan.md", "PLAN.txt", "plan.txt")

#: A heading that names a step. Sequence words only - `## Overview` is context,
#: `### Phase 3: Player Ship Module` is the thing the model must not re-invent.
#: The keyword must be followed by SPACE and then something - `Phase 1`,
#: `Step 2`, `Milestone A`. Without that, `## Step-by-Step Approach` came back
#: as a step, which is a section about the steps and not one of them.
_STEP_HEADING = re.compile(
    r"^\s{0,3}#{1,4}\s*((?:phase|step|milestone|stage|part|task)\s+\w.*)$",
    re.IGNORECASE | re.MULTILINE,
)

#: Smaller than the contract's budget. This is a reminder of what the steps are
#: CALLED, not the plan's contents - the model can read the file for those.
MAX_DOCUMENT_ANCHOR_CHARS = 600


#: One numbered or bulleted item under a phase heading. These are what the
#: phase's contract is made of.
_PHASE_STEP = re.compile(r"^\s{0,3}(?:\d+[.)]|[-*+])\s+(\S.*)$")

#: Any markdown heading - where a phase's body stops.
_ANY_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")


@dataclass(frozen=True)
class PlanPhase:
    """One phase of a plan document, and the items listed under it."""

    index: int
    #: The heading as written - `Phase 2: Core Game Loop & Scene Setup (main.js)`.
    heading: str
    #: Its numbered or bulleted items, verbatim.
    steps: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return f"phase-{self.index:02d}"

    def matches(self, wanted: str) -> bool:
        """Does *wanted* name this phase? `2`, `phase 2`, or part of the title."""
        probe = " ".join(str(wanted or "").split()).strip().lower()
        if not probe:
            return False
        bare = probe.removeprefix("phase").removeprefix("step").strip(" :.#")
        if bare.isdigit() and int(bare) == self.index:
            return True
        return probe in self.heading.lower()


def _plan_text(workspace) -> tuple[str, str]:
    """The plan document's text and filename, or ``("", "")``."""
    from pathlib import Path

    root = Path(workspace)
    for name in PLAN_FILENAMES:
        candidate = root / name
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace"), name
        except OSError:
            continue
    return "", ""


def plan_phases(workspace) -> list[PlanPhase]:
    """The plan's phases, each with the items written under it.

    Numbered from 1 in document order rather than by whatever the heading says,
    so a plan that skips or repeats a number still addresses cleanly. `matches`
    accepts the heading's own wording either way.
    """
    text, _ = _plan_text(workspace)
    if not text:
        return []
    phases: list[PlanPhase] = []
    current: str | None = None
    steps: list[str] = []

    def close() -> None:
        if current is not None:
            phases.append(
                PlanPhase(len(phases) + 1, current, tuple(steps))
            )

    for line in text.splitlines():
        heading = _STEP_HEADING.match(line)
        if heading:
            close()
            current = " ".join(heading.group(1).split()).rstrip("#").strip()
            steps = []
            continue
        if current is None:
            continue
        if _ANY_HEADING.match(line):
            # A non-phase heading ends the phase body - `## Exact File
            # Structure` is not part of Phase 8.
            close()
            current = None
            steps = []
            continue
        item = _PHASE_STEP.match(line)
        if item:
            # Verbatim, minus markdown emphasis. Rewording these into claim
            # form was considered and rejected: the model re-describing its own
            # plan is the drift this whole mechanism exists to remove, and the
            # evidence gate does not care how the sentence reads.
            steps.append(" ".join(item.group(1).replace("**", "").split()))
    close()
    return phases


def plan_document_steps(workspace) -> list[str]:
    """The step headings from a plan document in *workspace*, if there is one."""
    return [phase.heading for phase in plan_phases(workspace)]


def document_anchor(steps: list[str], filename: str = "PLAN.md") -> str:
    """The plan document's steps, re-shown by their real names.

    Deliberately just the names. The failure was never that the model could not
    read PLAN.md - it was that nothing reminded it the file existed or what its
    steps were called, so "phase 2" got resolved against a summary instead.
    """
    if not steps:
        return ""
    # No numbering of our own: the steps carry their own, and a second set
    # beside it is one more thing for "phase 2" to resolve against wrongly.
    body = "\n".join(f"  - {step}" for step in steps)
    if len(body) > MAX_DOCUMENT_ANCHOR_CHARS:
        body = body[:MAX_DOCUMENT_ANCHOR_CHARS].rstrip() + f"\n  ... (read {filename} for the rest)"
    return (
        f"THE PLAN IN {filename} - you wrote it, and these are its steps by their "
        "real names:\n" + body + "\n"
        f"When the user names a step, it means the one with that name HERE. If you "
        f"are about to work on one, read {filename} first for what it actually "
        "says - do not work from your memory of it."
    )


# -- one contract per phase --------------------------------------------------
#
# The user's shape, and a better one than a single contract with one assertion
# per phase. Measured on the real PLAN.md, that version produced 8 assertions of
# which 0 tripped the runtime gate - "Phase 3: Player Ship Module (player.js)"
# is a unit of WORK, not a checkable claim, so every one of them would have
# passed on a file write. Which is the failure this whole mechanism exists to
# stop, rebuilt out of the fix for it.
#
# A phase's own items are far closer to claims, and there are 3-5 of them rather
# than 38, so each phase's contract renders in ~500 characters - comfortably
# inside the anchor budget, unlike the 2,019 of the everything-at-once version.


def contract_from_phase(phase: PlanPhase, filename: str = "PLAN.md"):
    """Turn one phase of the plan into that phase's Definition of Done.

    Items go in verbatim. Rewording them into claim form was considered and
    rejected: a model re-describing its own plan is exactly the drift being
    removed here, and `requires_run` means the gate does not care how the
    sentence reads.
    """
    from shamsu.agents.simple_contract import new_contract

    contract = new_contract(
        phase.heading,
        f"Phase {phase.index} of {filename}, verbatim.",
        list(phase.steps) or [phase.heading],
    )
    contract.source = f"{filename} / {phase.heading}"
    contract.slug = phase.slug
    # A phase of a build plan is a unit of working software. Nothing here passes
    # on a file write.
    contract.requires_run = True
    return contract


def phase_progress(workspace, filename: str = "PLAN.md") -> str:
    """Where the work is, across every phase - not just the open one.

    The anchor shows the ACTIVE phase's contract in full. This is the line above
    it that says which phase that is and what happened to the others, so
    "proceed with phase 2" has somewhere true to resolve against.
    """
    from shamsu.agents.simple_contract import phase_contracts

    phases = plan_phases(workspace)
    if not phases:
        return ""
    done = phase_contracts(workspace)
    rows = []
    for phase in phases:
        contract = done.get(phase.slug)
        if contract is None:
            mark = "not started"
        elif contract.done and not contract.unproven and not contract.skipped:
            mark = "done, checked"
        elif contract.done:
            mark = "resolved, but not all of it was checked"
        else:
            mark = f"in progress - {len(contract.blockers)} of {len(contract.assertions)} left"
        rows.append(f"  {phase.slug}  [{mark}]  {phase.heading}")
    return (
        f"PHASES IN {filename} - this is what a phase number means here:\n"
        + "\n".join(rows)
        + f"\nTo start one, call contract_from_plan with its number. Read {filename} "
        "for what the phase actually says - do not work from memory of it."
    )
