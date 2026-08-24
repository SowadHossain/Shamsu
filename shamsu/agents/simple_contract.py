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
import re
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
    "claims_runtime_behaviour",
    "load_phase_contract",
    "phase_contracts",
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
    #: Where this came from, when it was not hand-written - `PLAN.md / Phase 2`.
    source: str = ""
    #: `phase-02`, when this is one phase of a plan. Also its archive filename,
    #: so finishing phase 2 and starting phase 3 no longer overwrites phase 2 -
    #: five contracts were created in the demo-3 session and four are gone.
    slug: str = ""
    #: A write cannot pass ANY assertion here.
    #:
    #: Derived from a plan, an assertion is a phase of a build: "Implement
    #: requestAnimationFrame game loop with delta time tracking" means the loop
    #: RUNS, not that the words are in the file. The general rule
    #: (`claims_runtime_behaviour`) reads the sentence and would let every one
    #: of those through, because a plan's items are written as work rather than
    #: as claims - measured on the real PLAN.md, 0 of 8 phase headings tripped
    #: it. So the provenance decides instead of the wording.
    requires_run: bool = False

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

    @property
    def skipped(self) -> list[Assertion]:
        """Resolved by declaring them out of scope, and checked by nobody.

        Skip exists because a guard with no exit is a deadlock, and that is
        still true. What was not true is that skipping is quiet. Live
        2026-08-24, `demo-3/asteroid`: `contract_assert_pass` was refused for
        want of an observation, and the model's very next call was
        `contract_assert_skip` on the same assertion - then on five more, six of
        ten inside three minutes, ending the turn with "Contract Complete". Read
        a10's skip REASON: "npm install completed successfully - evidenced by
        existence of package-lock.json". That is a pass justification posted
        through the skip door, because skip asks only for a non-empty string -
        the exact test this module already established is worth nothing.

        So skip still resolves. It just no longer does it silently.
        """
        return [item for item in self.assertions if item.state == SKIPPED]

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
        if self.done and (self.unproven or self.skipped):
            # "Resolved" was doing two jobs and only admitting to one. A
            # contract with six skips and four writes rendered as "Every
            # assertion is resolved", and the model read that line and wrote
            # "Contract Complete" - demo-3/asteroid, 2026-08-24.
            if self.unproven:
                names = ", ".join(item.id for item in self.unproven)
                lines.append(
                    f"{len(self.unproven)} of them ({names}) are backed only by the "
                    "file being written - nothing has been run that would show they "
                    "WORK."
                )
            if self.skipped:
                names = ", ".join(item.id for item in self.skipped)
                lines.append(
                    f"{len(self.skipped)} of them ({names}) you skipped, so nobody "
                    "checked them at all."
                )
            lines.append(
                "Every assertion is resolved, but that is not the same as checked. "
                "Run something that exercises the ones above before reporting the "
                "task finished - and if any of them genuinely cannot be checked "
                "here, tell the user which, in the reply, rather than reporting "
                "success."
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
            "source": self.source,
            "slug": self.slug,
            "requires_run": self.requires_run,
            "assertions": [item.to_dict() for item in self.assertions],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Contract:
        return cls(
            title=str(raw.get("title") or "Untitled"),
            brief=str(raw.get("brief") or ""),
            created=float(raw.get("created") or time.time()),
            source=str(raw.get("source") or ""),
            slug=str(raw.get("slug") or ""),
            requires_run=bool(raw.get("requires_run")),
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


def _phase_dir(workspace: Path) -> Path:
    return Path(workspace) / ".shamsu" / "contracts"


def save_contract(workspace: Path, contract: Contract) -> None:
    payload = json.dumps(contract.to_dict(), indent=2, ensure_ascii=True)
    target = _path(workspace)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
    except OSError:
        pass  # a contract that cannot be saved must not end the turn
    if not contract.slug:
        return
    # A phase also keeps its own file. `contract.json` is the ACTIVE one and is
    # overwritten by design - which is how the demo-3 session created five
    # contracts and left one on disk, with no record of what the other four had
    # asserted or abandoned. Moving to phase 3 must not erase phase 2.
    try:
        archive = _phase_dir(workspace)
        archive.mkdir(parents=True, exist_ok=True)
        (archive / f"{contract.slug}.json").write_text(payload, encoding="utf-8")
    except OSError:
        pass


def load_phase_contract(workspace: Path, slug: str) -> Contract | None:
    """One phase's contract, finished or not."""
    if not slug:
        return None
    try:
        raw = json.loads((_phase_dir(workspace) / f"{slug}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not raw.get("assertions"):
        return None
    return Contract.from_dict(raw)


def phase_contracts(workspace: Path) -> dict[str, Contract]:
    """Every phase contract this workspace has, keyed by slug."""
    found: dict[str, Contract] = {}
    try:
        paths = sorted(_phase_dir(workspace).glob("*.json"))
    except OSError:
        return found
    for path in paths:
        contract = load_phase_contract(workspace, path.stem)
        if contract is not None:
            found[path.stem] = contract
    return found


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
#
# Live 2026-08-24, `F:\Work\shamsu test - 24aug\demo-3\asteroid`: replaying all
# 15 assistant replies through this list, the guard fired on ONE. The other 14
# claimed success in phrasings none of these cover - "All bugs fixed!",
# "Contract Complete", "Phase 2 Complete", "Bug fixed!", "The game is now
# running", "I've fixed the rendering issue" - across a session that ended with
# a game whose `initGame()` had never once been called. A miss costing "one
# turn" was the wrong estimate: it cost sixteen.
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

#: The same claim, made in the shapes a chatty model actually reaches for. Each
#: of these was written by a real run before it was written here; see the note
#: above `_DONE_PHRASES`. Kept as patterns rather than substrings because the
#: count varies ("All 4 bugs fixed") and so does the subject ("the game is now
#: running", "the server is running").
_DONE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\b(?:all |the )?(?:\d+ )?bugs? (?:are |is |have been |has been )?fixed\b",
        r"\bi(?:'ve| have) fixed\b",
        r"\bi(?:'ve| have) (?:made|applied) [^.!?]{0,40}\bfixes\b",
        r"\b(?:all )?(?:the )?fixes (?:are )?applied\b",
        r"\bcontract (?:is )?complete\b",
        r"\bphase \S+ (?:is )?complete\b",
        (
            r"\b(?:server|app|game|site|page|build|it) (?:is|are) (?:now )?"
            r"(?:running|up|live|working|visible)\b"
        ),
        r"\bis now (?:running|working|live|visible|rendering)\b",
        r"\bnow (?:renders|works|running) (?:correctly|properly)\b",
        r"\bproblem (?:is )?(?:solved|fixed)\b",
        r"\bissue (?:is )?(?:resolved|fixed)\b",
    )
)

#: One sentence, or the tail that never got its full stop. Markdown headings
#: have no terminal punctuation, so a heading runs on into the prose beneath it
#: - which is fine here: the only question being asked of a chunk is whether it
#: is a question.
_SENTENCE = re.compile(r"[^.!?]*[.!?]|[^.!?]+")


def _claims_completion(chunk: str) -> bool:
    return any(phrase in chunk for phrase in _DONE_PHRASES) or any(
        pattern.search(chunk) for pattern in _DONE_PATTERNS
    )


def looks_like_a_done_claim(text: str) -> bool:
    """Does this reply claim the work is over?

    A question is never a done claim, however it is phrased - "shall I mark this
    complete?" is the model asking, and answering it with a guard would be
    talking past the user.

    That exemption used to be `body.endswith("?")`, applied to the WHOLE reply,
    and a 2,000-character message is not one question. Live 2026-08-24 a reply
    headed "Phase 2 Complete - Development Server Running!" ended with a menu of
    what to do next - "**D)** Something else?" - and that single trailing `?`
    disarmed the guard for the entire message. The exemption now covers the
    sentence that IS the question and nothing else.
    """
    body = " ".join((text or "").split()).lower()
    if not body:
        return False
    return any(
        _claims_completion(sentence)
        for sentence in (match.group().strip() for match in _SENTENCE.finditer(body))
        if sentence and not sentence.endswith("?")
    )


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
        #
        # And skipping was the same hole one door along. `unproven` only ever
        # looked at PASSED, so six assertions skipped in three minutes were
        # invisible here; skip all of them and this returned "" outright. Live
        # 2026-08-24 that is exactly the route the model took, the moment
        # `contract_assert_pass` refused it for want of an observation.
        if not contract.unproven and not contract.skipped:
            return ""
        parts = ["You said the task is finished, but resolved is not checked."]
        if contract.unproven:
            listed = chr(10).join(
                f"  {item.id}  {item.text}" for item in contract.unproven
            )
            parts.append(
                f"{len(contract.unproven)} assertion(s) are marked passed on the "
                "strength of the file being written, and nothing has been run that "
                "would show they work:" + chr(10) * 2 + listed
            )
        if contract.skipped:
            listed = chr(10).join(
                f"  {item.id}  {item.text}"
                + (f"{chr(10)}        you said: {item.evidence[:160]}" if item.evidence else "")
                for item in contract.skipped
            )
            parts.append(
                f"{len(contract.skipped)} assertion(s) you skipped, so nobody checked "
                "them at all:" + chr(10) * 2 + listed + chr(10) * 2
                + "Skipping is for something genuinely out of scope, not for something "
                "you could not check. If a skip reason reads like evidence, it wanted "
                "to be a pass and belongs behind a real observation."
            )
        if contract.unproven and contract.skipped:
            opening = (
                "Writing the code is not evidence that the code runs, and skipping "
                "it is not evidence of anything."
            )
        elif contract.unproven:
            opening = "Writing the code is not evidence that the code runs."
        else:
            opening = "Skipping something is not evidence about it."
        parts.append(
            opening + " Run something that exercises these - run_tests, or "
            "run_command with whatever checks this project - and record what it "
            "printed. If they genuinely cannot be checked here, say which ones, "
            "plainly, to the user instead of reporting success."
        )
        return (chr(10) * 2).join(parts)
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


#: Assertions about what the CODE DOES WHEN IT RUNS, as opposed to what the
#: code says. `unproven` and `done_guard` already complain about a pass backed
#: only by a write - but only at the end, only if the model claims done, and
#: `render()` still listed it as PASS the whole way there.
#:
#: Live 2026-08-24, `demo-3/asteroid`, the contract on disk when the session
#: ended:
#:
#:   a03  "game renders without setElement error on page load"   state: passed
#:        verified_by: write
#:        observation: "wrote src/main.js (not run)"
#:        evidence:    "Console shows: 'Page loaded, starting game...',
#:                      '=== INITIALIZING GAME ===', '/ Scene initialized'..."
#:
#: The evidence quotes browser console output that was never produced, and the
#: field beside it says `(not run)`. An assertion about what a BROWSER does was
#: discharged by writing a file. a01 is worse: it cites `canvas.appendChild(
#: renderer.domElement)` as proof the renderer is attached correctly, and that
#: line IS the bug.
#: Measured on a 30-assertion corpus (15 runtime, 15 static): 15/15 caught, 0
#: false positives. The two things that cost the most iterations:
#:
#: * The leading lookbehinds must use `\s`, not a literal space. Under `(?x)`
#:   the space is stripped out of the pattern, so `(?<!\ba )` silently became
#:   `(?<!\ba)` and matched nothing - which is how "a **run** script" and
#:   "a **render** function" were being called runtime claims.
#: * Bare `run` and `load` are nouns at least as often as verbs in this domain
#:   ("a run script", "a loading spinner"), so they only count with something
#:   attached: `is running`, `runs at`, `the page loads`.
#:
#: A false positive here refuses an assertion a write could honestly have
#: backed, so the bar is a subject DOING something, not a word appearing.
_RUNTIME_CLAIM = re.compile(
    r"(?ix)"
    r"(?<!\ba\s)(?<!\ban\s)(?<!\bthe\s)"
    r"\b(?:"
    r"renders?|rendered|rendering"
    r"|displays?|displayed|draws?|drawn|paints?|painted"
    r"|appears?|appeared|(?:is|are|becomes?)\s+visible"
    r"|spawns?|spawned|moves?|moved|collides?|collided|fires?|fired"
    r"|animates?|animated|updates?|updated|restarts?|restarted"
    r"|responds?|responded|serves?|served|submits?|submitted"
    r"|returns?\s+\d+|exits?\s+\d+"
    r"|(?:is|are|still)\s+(?:running|up|live|serving)"
    r"|runs?(?:\s+(?:at|without|correctly|successfully|clean|fine))?"
    r"|(?:page|app|site|module|script|game|it)\s+loads?"
    r"|loads?\s+(?:without|correctly|successfully)"
    r"|on\s+page\s+load|in\s+the\s+browser|console\s+(?:shows?|logs?)"
    # `without ReferenceError` - a named exception type is the most specific
    # runtime claim there is, and `\berror\b` does not match inside one.
    r"|(?:without|no)\s+(?:an?\s+)?\w*(?:error|exception|crash)s?"
    r"|works?(?:\s+(?:correctly|as|when|after))?|working\s+(?:correctly|as)"
    r"|behaves?|user\s+can|clicking|pressing|typing"
    r")\b"
)


def claims_runtime_behaviour(text: str) -> bool:
    """Does this assertion describe what happens when the code RUNS?

    Then a file write cannot discharge it, however sincere the paragraph. The
    distinction the contract exists to draw is between "the file now says the
    collision detection is there" and "the collision detection works", and this
    is the half that says which one an assertion is asking about.
    """
    return bool(_RUNTIME_CLAIM.search(text or ""))
