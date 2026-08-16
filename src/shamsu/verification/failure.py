"""Failure classification and the failure capsule.

Plan §27's recovery flow starts with two steps that v1 never really had:
classify the failure, then generate a capsule. v1 fed raw test output back to
the model and hoped, which meant every repair attempt started by re-reading
four thousand lines to rediscover what had already been established.

A capsule is the opposite: a small, structured account of *one* failure —
what was expected, what happened, where, which files are implicated, what has
already been tried, and what to probe next. It is what enters the repair
frame in place of the output.

Classification is done by patterns over real tool output, never by asking a
model. Invariant 8: structural facts come from parsers. A misclassified failure
sends repair down the wrong path, and a model that guesses "dependency
conflict" because the word `import` appeared is worse than a classifier whose
failure mode is a documented fall-through to `TEST_FAILURE`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from shamsu.interfaces.enums import EvidenceKind, FailureKind
from shamsu.verification.digest import TestDigest, error_signature

#: Ordered patterns, most specific first. The first match wins. Order matters
#: for the same reason it does in the evidence vocabulary: "ModuleNotFoundError"
#: contains "Error", and a generic pattern placed early swallows every specific
#: one after it.
_CLASSIFIERS: tuple[tuple[re.Pattern[str], FailureKind], ...] = (
    (re.compile(r"\b(SyntaxError|IndentationError|TabError)\b"), FailureKind.SYNTAX_ERROR),
    (
        re.compile(r"\b(ModuleNotFoundError|ImportError)\b|\bno matching distribution\b"),
        FailureKind.DEPENDENCY_CONFLICT,
    ),
    (
        re.compile(r"\b(PermissionError|EACCES|EPERM)\b|\bpermission denied\b"),
        FailureKind.PERMISSION_FAILURE,
    ),
    (
        re.compile(
            r"\b(ConnectionError|ConnectionRefusedError|socket\.timeout)\b"
            r"|\bconnection refused\b|\bname or service not known\b"
        ),
        FailureKind.NETWORK_FAILURE,
    ),
    (
        re.compile(
            r"\b(OperationalError|IntegrityError|ProgrammingError)\b|\bdatabase is locked\b"
        ),
        FailureKind.DATABASE_FAILURE,
    ),
    (
        re.compile(r"\b(MemoryError|RecursionError)\b|\bno space left on device\b"),
        FailureKind.RESOURCE_LIMIT,
    ),
    (
        re.compile(
            r"\b(TypeError|AttributeError)\b|\berror: .*\[(arg-type|assignment|attr-defined)"
        ),
        FailureKind.TYPE_ERROR,
    ),
    (
        re.compile(r"\bhealth ?check\b|\bservice unhealthy\b|\bcontainer .*unhealthy\b"),
        FailureKind.SERVICE_HEALTH_FAILURE,
    ),
    (
        re.compile(r"\bbuild failed\b|\bcompilation (error|failed)\b|\blinker\b"),
        FailureKind.BUILD_FAILURE,
    ),
    (re.compile(r"^E\s+assert\b|\bAssertionError\b", re.M), FailureKind.TEST_FAILURE),
)

#: What a capsule suggests looking at next, by failure kind. Probes, not fixes:
#: the runtime does not know the answer, and pretending to would be worse than
#: naming where the answer is likely to be.
_PROBES: dict[FailureKind, tuple[str, ...]] = {
    FailureKind.SYNTAX_ERROR: (
        "Read the file at the reported line; the real error is often one line above it.",
    ),
    FailureKind.TYPE_ERROR: (
        "Read the definition of the symbol named in the error.",
        "Check the call site's argument types against that definition.",
    ),
    FailureKind.TEST_FAILURE: (
        "Read the failing test to see what it expects.",
        "Read the function under test; compare its behaviour to that expectation.",
    ),
    FailureKind.DEPENDENCY_CONFLICT: (
        "Check whether the module is declared in the project manifest.",
        "Check whether the import path matches the package layout.",
    ),
    FailureKind.BUILD_FAILURE: ("Read the first error in the build log, not the last.",),
    FailureKind.PERMISSION_FAILURE: (
        "Check the path is inside the workspace; the sandbox refuses anything else.",
    ),
    FailureKind.MISSING_CONTEXT: ("Search for the symbol before assuming where it lives.",),
    FailureKind.INCOMPLETE_EVIDENCE: (
        "The work may already be done; what is missing is the proof.",
        "Call the tool that produces the missing evidence before concluding again.",
    ),
}

#: The same failure when *nothing was written*, which is a different situation
#: and needs the opposite advice.
#:
#: The §31.1 suite measured what the wrong advice costs. Four of five failures
#: ended after exactly two tool calls, every call successful, no edit ever
#: attempted: the agent read `README.md`, concluded, was told "the work may
#: already be done; what is missing is the proof" — and so read `README.md` a
#: second time, looking for the proof of work it had never performed. Told that
#: repeating a failure means it should investigate first, it investigated more.
#:
#: Both messages are true of a step that edited something and forgot to verify.
#: Both are false, and actively misleading, for one that never edited at all.
_NOTHING_WRITTEN_PROBES: tuple[str, ...] = (
    "No file has been changed yet — this is not a missing-proof problem.",
    "Make the edit now with file.patch. Reading the file again cannot satisfy this.",
)

_DEFAULT_PROBES: tuple[str, ...] = (
    "Re-read the failing output; state what was expected before changing anything.",
)


def classify_failure(text: str, *, tool: str = "") -> FailureKind:
    """Name the kind of failure in `text`.

    The fall-through is deliberate and documented: unmatched output from
    `test.run` is a test failure, and unmatched output from anything else is a
    tool failure. Guessing more specifically from less evidence is how a repair
    ends up fixing the wrong thing.
    """
    for pattern, kind in _CLASSIFIERS:
        if pattern.search(text):
            return kind
    return FailureKind.TEST_FAILURE if tool == "test.run" else FailureKind.TOOL_FAILURE


@dataclass(frozen=True)
class RepairAttempt:
    """One previous try at this failure."""

    attempt: int
    signature: str
    summary: str


@dataclass(frozen=True)
class FailureCapsule:
    """A compact, structured account of one failure (plan §15.12).

    Every field is derived from observed output. Nothing here is a model's
    description of what it thinks went wrong, because a description of a
    failure is exactly the thing a confused model gets confidently wrong.
    """

    kind: FailureKind
    signature: str
    expected: str = ""
    actual: str = ""
    frames: tuple[str, ...] = ()
    implicated_files: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    related_files: tuple[str, ...] = ()
    previous_attempts: tuple[RepairAttempt, ...] = ()
    #: What worked the last time this signature appeared, from project memory.
    #: Advisory: it describes a *different task's* fix, so it is offered as a
    #: starting point and never as an instruction.
    prior_lesson: str = ""
    probes: tuple[str, ...] = field(default_factory=lambda: _DEFAULT_PROBES)

    @property
    def attempt(self) -> int:
        """Which attempt this capsule describes, counting from 1."""
        return len(self.previous_attempts) + 1

    @property
    def repeating(self) -> bool:
        """Whether the last attempt produced this same failure.

        The signal that repair is grinding rather than progressing. Reported on
        the capsule so the model sees it too — being told "you have already
        tried this and it failed identically" is more useful than silently
        stopping.
        """
        return (
            bool(self.previous_attempts) and self.previous_attempts[-1].signature == self.signature
        )

    def render(self, max_frames: int = 5) -> str:
        """The capsule as it enters a repair frame."""
        lines = [f"Failure: {self.kind.value} (attempt {self.attempt})"]

        if self.expected:
            lines.append(f"Expected: {self.expected}")
        if self.actual:
            lines.append(f"Actual: {self.actual}")

        if self.frames:
            lines.append("Where:")
            lines.extend(f"  {frame}" for frame in self.frames[:max_frames])

        if self.changed_files:
            lines.append("Changed in this step: " + ", ".join(self.changed_files))

        if self.previous_attempts:
            lines.append("Already tried:")
            lines.extend(
                f"  attempt {item.attempt}: {item.summary}" for item in self.previous_attempts
            )
        if self.prior_lesson:
            lines.append(f"From a previous task: {self.prior_lesson}")

        if self.repeating:
            # "Investigate before editing" is the right lesson for a change
            # that did not work, and the wrong one for a step that never made a
            # change — it sent an agent that had only read files off to read
            # more. What repeated there is the *inaction*, so say that instead.
            lines.append(
                "This is the SAME failure as the last attempt. You did not edit "
                "anything last time either — investigating again will repeat it. "
                "Call file.patch."
                if not self.changed_files
                else "This is the SAME failure as the last attempt. Repeating that "
                "change will not work; investigate before editing."
            )

        lines.append("You may only edit: " + (", ".join(self.editable()) or "(nothing)"))
        lines.append("Next probes:")
        lines.extend(f"  {probe}" for probe in self.probes)

        return "\n".join(lines)

    def editable(self) -> tuple[str, ...]:
        """Files this failure justifies changing, in first-seen order.

        Three sources, and the third is not optional. Traceback frames are not
        enough on their own: when a test fails on an assertion, the buggy
        function *returned normally*, so no frame names it — the only file in
        the traceback is the test. A scope built from frames alone would
        therefore permit editing the test and nothing else, which is precisely
        backwards.

        So the union is: what the traceback implicates, what the step already
        changed, and what the plan step declared it was about. All three are
        recorded before the failure, none is invented afterwards, and anything
        outside them is the "broad repository rewrite" plan §20.5 blocks.
        """
        ordered: list[str] = []
        for path in (*self.implicated_files, *self.changed_files, *self.related_files):
            if path not in ordered:
                ordered.append(path)
        return tuple(ordered)


def build_capsule(
    digest: TestDigest,
    *,
    tool: str = "test.run",
    changed_files: Sequence[str] = (),
    related_files: Sequence[str] = (),
    previous_attempts: Sequence[RepairAttempt] = (),
    prior_lesson: str = "",
    raw: str = "",
) -> FailureCapsule:
    """Turn a digested failure into a capsule.

    `digest` already carries the expensive parts — the signature, the frames,
    the assertions. This adds classification, the files the failure implicates,
    and the history, which is what makes the second attempt cheaper than the
    first rather than identical to it.
    """
    text = raw or "\n".join([digest.summary, *digest.assertions, *digest.frames])
    kind = classify_failure(text, tool=tool)

    return FailureCapsule(
        kind=kind,
        signature=digest.signature or error_signature(text),
        expected=_expected(digest),
        actual=digest.assertions[0] if digest.assertions else digest.summary,
        frames=digest.frames,
        implicated_files=_files_from_frames(digest.frames),
        changed_files=tuple(dict.fromkeys(changed_files)),
        related_files=tuple(dict.fromkeys(related_files)),
        previous_attempts=tuple(previous_attempts),
        prior_lesson=prior_lesson,
        probes=_PROBES.get(kind, _DEFAULT_PROBES),
    )


def evidence_capsule(
    missing: Sequence[EvidenceKind],
    *,
    producers: Mapping[EvidenceKind, Sequence[str]] | None = None,
    changed_files: Sequence[str] = (),
    related_files: Sequence[str] = (),
    previous_attempts: Sequence[RepairAttempt] = (),
    prior_lesson: str = "",
) -> FailureCapsule:
    """A capsule for a step that ended without the evidence its gate requires.

    Distinct from `build_capsule` because there is no failing command to digest.
    Every tool call may have succeeded; what is missing is proof, and often only
    one call's worth of it.

    That case is common and was previously unrecoverable. A live §31.1 run fixed
    `add()` correctly, produced `file_changed`, and stopped one `git.inspect`
    short of `git_diff_reviewed` — then blocked, because repair was reachable
    only from a *test* failure. The capsule says which evidence is outstanding
    and which tool produces it, so the next attempt is a single call rather than
    a re-derivation.

    The signature is the sorted missing kinds, so two identical refusals look
    identical to `RepairTracker` and the run stops rather than looping.
    """
    names = sorted(kind.value for kind in missing)
    tools = producers or {}

    wanted: list[str] = []
    for kind in sorted(missing, key=lambda item: item.value):
        available = sorted(set(tools.get(kind, ())))
        wanted.append(f"{kind.value} (call {' or '.join(available)})" if available else kind.value)

    changed = tuple(dict.fromkeys(changed_files))
    wrote_nothing = not changed and EvidenceKind.FILE_CHANGED in missing

    return FailureCapsule(
        kind=FailureKind.INCOMPLETE_EVIDENCE,
        signature=f"unmet-evidence:{'+'.join(names)}",
        expected="verified evidence: " + ", ".join(wanted),
        actual=(
            "no file was changed and the step concluded anyway"
            if wrote_nothing
            else "the step concluded without producing it"
        ),
        changed_files=changed,
        related_files=tuple(dict.fromkeys(related_files)),
        previous_attempts=tuple(previous_attempts),
        prior_lesson=prior_lesson,
        probes=_NOTHING_WRITTEN_PROBES
        if wrote_nothing
        else _PROBES[FailureKind.INCOMPLETE_EVIDENCE],
    )


def _expected(digest: TestDigest) -> str:
    """What the run was supposed to produce, stated plainly."""
    if digest.failed_tests:
        names = ", ".join(digest.failed_tests[:3])
        extra = "" if len(digest.failed_tests) <= 3 else f" (+{len(digest.failed_tests) - 3} more)"
        return f"{names}{extra} to pass"
    return "the command to succeed"


def _files_from_frames(frames: Sequence[str]) -> tuple[str, ...]:
    """Source paths named by traceback frames, in order, deduplicated.

    Separators are normalised to `/`. These are not for display: they are
    compared against `changed_files` and turned into the `WriteScope` a repair
    is confined to, and every other path in the system is POSIX-shaped. On
    Windows a traceback says `tests\\test_calc.py`, which matched nothing, so a
    repair scope silently excluded the very file the failure implicated.
    """
    paths: list[str] = []
    for frame in frames:
        path = frame.rsplit(":", 1)[0] if ":" in frame else frame
        normalised = path.replace("\\", "/")
        if normalised and normalised not in paths:
            paths.append(normalised)
    return tuple(paths)


__all__ = [
    "FailureCapsule",
    "RepairAttempt",
    "build_capsule",
    "classify_failure",
    "evidence_capsule",
]
