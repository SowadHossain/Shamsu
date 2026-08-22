"""A Definition of Done the model writes down and then has to satisfy.

The failure this exists for is the one every measurement of this harness keeps
finding: the model stops before the work is finished and says something that
reads like success. Live 2026-08-20 it wrote 39 lines of a 1,500-line file and
asked "what would you like to do next?"; on 2026-08-19 it reported a no-op write
as done on a file that would not parse. The prompt has said "do not claim
complete" - four separate times, in the legacy path - and saying it again is
measurably not the fix.

smallcode's answer is a contract (`src/session/contract_*.js`) and it is a good
one, because it moves the claim from PROSE to STATE. The model writes down what
"done" means as a list of checkable assertions, and then a claim of completion
is not a sentence to be believed but a set of assertions that are either
resolved or not.

Two things make this different from the contract machinery SHAMSU already has.
`verify/contract.py` DERIVES a contract from the user's prompt - which paths
were named, which symbols must exist - and `verify/dod.py` runs checks a
registry declared per category. Both are inferred, and both belong to the
legacy pipeline. This one is AUTHORED, by the model, for this task, and its
assertions are sentences rather than check ids. The three do not overlap.

**On disk, not in memory.** A `SimpleChatLoop` is rebuilt for every user
message, so anything kept on the object resets whenever the user types - which
is exactly how `MAX_UNPRODUCTIVE_EDITS` failed to fire for months. A contract
that survives one turn is not a contract.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "Assertion",
    "Contract",
    "contract_disabled",
    "done_guard",
    "new_contract",
    "load_contract",
    "save_contract",
    "clear_contract",
    "looks_like_a_done_claim",
]

PENDING = "pending"
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

# Resolved means "the model looked and said what it found", not "it worked".
# A failed assertion is resolved: the model ran the check, the check said no,
# and that is a finished piece of work even though the answer is bad news.
# Blocking on it would leave the model unable to report a real failure.
RESOLVED = {PASSED, FAILED, SKIPPED}


#: How a PASSED assertion is backed. The distinction the contract existed to
#: make and never did: `evidence` is a paragraph the MODEL wrote, and a model
#: describing its own code is not a check.
#:
#: Live 2026-08-22, `F:\Work\demo2\test`: seven assertions marked passed,
#: every one with a confident paragraph, and the game drew neither the ship nor
#: a single asteroid. a02's evidence read "Positioned at bottom of screen using
#: camera position calculations" - an accurate description of the line that put
#: the ship outside the camera frustum.
BY_RUN = "run"        # a command or test the harness watched exit 0
BY_WRITE = "write"    # a file the harness itself wrote
BY_NOTHING = ""       # nothing - no longer accepted


@dataclass
class Assertion:
    """One checkable promise."""

    id: str
    text: str
    state: str = PENDING
    evidence: str = ""
    #: What the HARNESS saw, as opposed to what the model said it saw. Set by
    #: the loop, never by the model.
    verified_by: str = BY_NOTHING
    observation: str = ""

    @property
    def is_verified(self) -> bool:
        """Passed AND backed by something that ran. A write is not a check: it
        proves the text reached the disk, not that the text is right."""
        return self.state == PASSED and self.verified_by == BY_RUN

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "state": self.state,
                "evidence": self.evidence, "verified_by": self.verified_by,
                "observation": self.observation}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Assertion:
        return cls(
            id=str(raw.get("id") or ""),
            text=str(raw.get("text") or ""),
            state=str(raw.get("state") or PENDING),
            evidence=str(raw.get("evidence") or ""),
            verified_by=str(raw.get("verified_by") or BY_NOTHING),
            observation=str(raw.get("observation") or ""),
        )


@dataclass
class Contract:
    """What this task means by "done"."""

    title: str
    brief: str = ""
    assertions: list[Assertion] = field(default_factory=list)
    created: float = field(default_factory=time.time)

    @property
    def blockers(self) -> list[Assertion]:
        return [item for item in self.assertions if item.state not in RESOLVED]

    @property
    def done(self) -> bool:
        return bool(self.assertions) and not self.blockers

    @property
    def unproven(self) -> list[Assertion]:
        """Passed on the strength of a file write and nothing else.

        Not failures - the text really did reach the disk. But "the file now
        says the collision detection is there" and "the collision detection
        works" are different claims, and only one of them was checked."""
        return [
            item
            for item in self.assertions
            if item.state == PASSED and item.verified_by != BY_RUN
        ]

    def find(self, assertion_id: str) -> Assertion | None:
        wanted = (assertion_id or "").strip().lower()
        if not wanted:
            return None
        for item in self.assertions:
            if item.id.lower() == wanted:
                return item
        # `a1` for `a01`, and a bare number - a small model will not reliably
        # reproduce a zero-padded id, and refusing it teaches nothing.
        digits = wanted.lstrip("a").lstrip("0") or "0"
        for item in self.assertions:
            if item.id.lstrip("a").lstrip("0") == digits:
                return item
        return None

    def render(self) -> str:
        """The contract as the model should read it."""
        lines = [f"Contract: {self.title}"]
        if self.brief:
            lines.append(self.brief)
        lines.append("")
        marks = {PASSED: "PASS", FAILED: "FAIL", SKIPPED: "SKIP", PENDING: "....."}
        for item in self.assertions:
            mark = marks.get(item.state, item.state)
            lines.append(f"  {item.id}  [{mark}]  {item.text}")
            if item.state == PASSED:
                lines.append(
                    f"          backed by: {item.observation or 'nothing that ran'}"
                )
            if item.evidence:
                lines.append(f"          you said: {item.evidence[:200]}")
        lines.append("")
        if self.done and self.unproven:
            names = ", ".join(item.id for item in self.unproven)
            lines.append(
                f"Every assertion is resolved, but {len(self.unproven)} of them "
                f"({names}) are backed only by the file being written - nothing has "
                "been run that would show they WORK. Run something that exercises "
                "them before reporting the task finished."
            )
        elif self.done:
            lines.append("Every assertion is resolved. You can report the task finished.")
        else:
            names = ", ".join(item.id for item in self.blockers)
            lines.append(
                f"{len(self.blockers)} still unresolved ({names}). Check each one and "
                "record what you found with contract_assert_pass, contract_assert_fail "
                "or contract_assert_skip."
            )
        return chr(10).join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "brief": self.brief,
            "created": self.created,
            "assertions": [item.to_dict() for item in self.assertions],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Contract:
        return cls(
            title=str(raw.get("title") or "Untitled"),
            brief=str(raw.get("brief") or ""),
            created=float(raw.get("created") or time.time()),
            assertions=[Assertion.from_dict(item) for item in raw.get("assertions") or []],
        )


def new_contract(title: str, brief: str, assertions: list[str]) -> Contract:
    """Build a contract, numbering the assertions."""
    items = [
        Assertion(id=f"a{index:02d}", text=" ".join(str(text).split()))
        for index, text in enumerate(assertions, start=1)
        if str(text).strip()
    ]
    return Contract(title=" ".join((title or "Untitled").split()), brief=brief.strip(),
                    assertions=items)


def _path(workspace: Path) -> Path:
    return Path(workspace) / ".shamsu" / "contract.json"


def load_contract(workspace: Path) -> Contract | None:
    try:
        raw = json.loads(_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("assertions"):
        return None
    return Contract.from_dict(raw)


def save_contract(workspace: Path, contract: Contract) -> None:
    target = _path(workspace)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(contract.to_dict(), indent=2, ensure_ascii=True), encoding="utf-8"
        )
    except OSError:
        pass  # a contract that cannot be saved must not end the turn


def clear_contract(workspace: Path) -> None:
    try:
        _path(workspace).unlink()
    except OSError:
        pass


def contract_disabled() -> bool:
    """`SHAMSU_CONTRACT=0` turns the whole feature off, guard included."""
    return os.environ.get("SHAMSU_CONTRACT", "").strip().lower() in {"0", "false", "no", "off"}


# Phrases that mean the model thinks it has finished. Conservative on purpose:
# the guard costs a round every time it fires, so a false positive is expensive
# and a miss only means the contract goes unchecked for one turn.
_DONE_PHRASES = (
    "all done",
    "task is complete",
    "task is now complete",
    "task complete",
    "is now complete",
    "have completed",
    "successfully implemented",
    "successfully completed",
    "everything is working",
    "everything works",
    "all set",
    "ready to use",
    "ready to ship",
    "implementation is complete",
    "the fix is complete",
    "finished the task",
    "i have finished",
    "it is finished",
)


def looks_like_a_done_claim(text: str) -> bool:
    """Does this reply claim the work is over?

    A question is never a done claim, however it is phrased - "shall I mark this
    complete?" is the model asking, and answering it with a guard would be
    talking past the user.
    """
    body = " ".join((text or "").split()).lower()
    if not body or body.endswith("?"):
        return False
    return any(phrase in body for phrase in _DONE_PHRASES)


def done_guard(contract: Contract | None, text: str) -> str:
    """The correction to inject, or ``""``.

    Not a block and not a rewrite: the model's sentence stands, and it is handed
    the list of things it has not checked. smallcode's shape, and the important
    property is that it names the exact next calls rather than repeating the
    prohibition - a standing "do not claim complete" has been in this project's
    prompts four times over and never worked.
    """
    if contract is None or contract_disabled():
        return ""
    if not looks_like_a_done_claim(text):
        return ""
    if contract.done:
        # Resolved is not the same as checked. Every assertion being marked
        # passed used to end this function, so a contract signed off entirely
        # on file writes waved through "All requirements have been successfully
        # implemented" - live 2026-08-22, on a game that drew neither the ship
        # nor a single asteroid.
        if not contract.unproven:
            return ""
        listed = chr(10).join(
            f"  {item.id}  {item.text}" for item in contract.unproven
        )
        return (
            f"You said the task is finished. {len(contract.unproven)} assertion(s) "
            "are marked passed on the strength of the file being written, and "
            "nothing has been run that would show they work:" + chr(10) * 2
            + listed + chr(10) * 2
            + "Writing the code is not evidence that the code runs. Run something "
            "that exercises them - run_tests, or run_command with whatever checks "
            "this project - and record what it printed. If it genuinely cannot be "
            "run here, say so plainly to the user instead of reporting success."
        )
    listed = chr(10).join(
        f"  {item.id}  {item.text}" for item in contract.blockers
    )
    return (
        f"You said the task is finished, but the contract {contract.title!r} has "
        f"{len(contract.blockers)} assertion(s) nobody has checked:" + chr(10) * 2
        + listed + chr(10) * 2
        + "Check each one now and record what you found: contract_assert_pass with "
        "the evidence, contract_assert_fail if it really is broken, or "
        "contract_assert_skip with a reason if it is out of scope. If something "
        "stops you checking, say which assertion and what is blocking it."
    )
