"""Bounded repair: fix the failure, or stop — never wander.

Plan §20.5 allows repair to read the failure capsule, read affected files,
modify *failure-related* files, and run targeted verification. It blocks
unrelated architecture changes, broad repository rewrites, and new feature
work. This module makes the middle one enforceable rather than aspirational.

Two controls do the work.

**A write scope derived from the failure.** The files a repair may edit are the
ones the traceback implicates plus the ones the step already changed. The scope
goes to the gateway, so the restriction is enforced on the same path as the
phase allowlist — a repair controller that merely *intended* to stay in scope
would be one refactor away from not.

**Test files are protected by default.** A repair that edits the failing test
is indistinguishable from a repair that deletes the evidence, and it is the
single most attractive wrong move available to a model that wants the run over.
Editing them requires `allow_test_edits=True`, which is the caller's decision —
never the model's, and never a default.

Stopping is the other half. Two identical error signatures mean the attempts
are not making progress, and continuing spends the remaining budget to arrive
at the same place. `RepairTracker` already computes that; this module acts on
it and records why.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from shamsu.interfaces.enums import FailureKind, StepOutcome
from shamsu.interfaces.ids import FailureId, StepId, TaskId
from shamsu.memory.store import MemoryStore
from shamsu.runtime.limits import DEFAULT_LIMITS, ExecutionLimits
from shamsu.state.records import FailureRecord, new_id
from shamsu.state.store import StateStore
from shamsu.verification.digest import RepairTracker, TestDigest
from shamsu.verification.failure import FailureCapsule, RepairAttempt, build_capsule

#: Path shapes treated as tests. A heuristic, and named as one: it covers the
#: conventions of the ecosystems v2 targets first and will need extending. The
#: failure mode is the safe direction — an unrecognised test file is merely
#: editable, not silently exempt from verification.
_TEST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"[^/]+_test\.py$"),
    re.compile(r"\.(test|spec)\.[jt]sx?$"),
    re.compile(r"(^|/)conftest\.py$"),
)


def looks_like_a_test(path: str) -> bool:
    """Whether a path is a test file by convention."""
    normalised = path.replace("\\", "/")
    return any(pattern.search(normalised) for pattern in _TEST_PATTERNS)


@dataclass(frozen=True)
class RepairScope:
    """The files a repair is permitted to modify.

    Satisfies `shamsu.interfaces.tools.WriteScope`, so it is handed to the
    gateway rather than consulted by the caller.
    """

    allowed: frozenset[str]
    protected: frozenset[str] = frozenset()

    def permits(self, path: str) -> bool:
        normalised = path.replace("\\", "/").lstrip("./")
        return normalised in self.allowed

    def describe(self) -> str:
        if not self.allowed:
            return "This repair may not modify any file; investigate and report instead."
        editable = ", ".join(sorted(self.allowed))
        line = f"A repair may only edit files related to the failure: {editable}."
        if self.protected:
            line += (
                f" Test files are protected and cannot be edited during repair: "
                f"{', '.join(sorted(self.protected))}."
            )
        return line

    @classmethod
    def for_capsule(cls, capsule: FailureCapsule, *, allow_test_edits: bool = False) -> RepairScope:
        """Derive the scope from what the failure actually implicates.

        Grounded rather than declared: the paths come from traceback frames and
        from the step's own recorded edits, so the scope is as narrow as the
        evidence and no narrower.
        """
        candidates = [path.replace("\\", "/").lstrip("./") for path in capsule.editable()]

        if allow_test_edits:
            return cls(allowed=frozenset(candidates))

        allowed = {path for path in candidates if not looks_like_a_test(path)}
        protected = {path for path in candidates if looks_like_a_test(path)}
        return cls(allowed=frozenset(allowed), protected=frozenset(protected))


@dataclass(frozen=True)
class RepairDecision:
    """Whether to attempt another repair, and the capsule to attempt it with."""

    proceed: bool
    reason: str
    capsule: FailureCapsule | None = None
    scope: RepairScope | None = None
    outcome: StepOutcome | None = None

    @property
    def kind(self) -> FailureKind | None:
        return self.capsule.kind if self.capsule else None


@dataclass
class RepairController:
    """Decides whether a failed step gets another attempt.

    A bounded controller, like every other agent component here: it answers one
    question per call and returns. It never loops, and it never repairs
    anything itself — it says whether repair is warranted, within what scope,
    and records the failure either way.
    """

    store: StateStore
    task_id: TaskId
    limits: ExecutionLimits = DEFAULT_LIMITS
    allow_test_edits: bool = False

    #: Optional project memory. When present, a failure whose signature has
    #: been seen in an *earlier task* arrives with what fixed it last time, and
    #: this failure is recorded so the next task inherits the same benefit.
    #: Optional because repair must work on a project with no history at all.
    memory: MemoryStore | None = None

    #: How many identical signatures in a row mean "not making progress".
    stuck_threshold: int = 2

    def consider(
        self,
        digest: TestDigest,
        *,
        step_id: StepId | None = None,
        tool: str = "test.run",
        changed_files: Sequence[str] = (),
        related_files: Sequence[str] = (),
        raw: str = "",
    ) -> RepairDecision:
        """Record a failure and decide what happens next.

        `related_files` is normally the failing step's declared `inputs`. It
        matters more than it looks: an assertion failure's traceback names the
        test, not the function that returned the wrong value, so without it a
        repair of a wrong return value has nothing in scope but the test.

        The failure is persisted *before* the decision, so a run that stops here
        still leaves a complete account of why. v1 recorded outcomes and lost
        the reasons.
        """
        history = self._history(step_id)
        capsule = build_capsule(
            digest,
            tool=tool,
            changed_files=changed_files,
            related_files=related_files,
            previous_attempts=history,
            prior_lesson=self._prior_lesson(digest.signature),
            raw=raw,
        )

        self.store.record_failure(
            FailureRecord(
                failure_id=FailureId(new_id()),
                task_id=self.task_id,
                step_id=step_id,
                kind=capsule.kind,
                signature=capsule.signature,
                expected=capsule.expected,
                actual=capsule.actual,
                detail=capsule.render(),
                attempt=capsule.attempt,
            )
        )
        if self.memory is not None and capsule.signature:
            # Recorded on every attempt, not only on success: "this failure has
            # happened five times and nothing fixed it" is worth knowing, and a
            # memory that only remembers wins cannot say it.
            self.memory.remember_failure(
                capsule.signature,
                f"{capsule.kind.value}: {capsule.actual or capsule.expected}"[:400],
                task_id=self.task_id,
                related_paths=capsule.editable(),
            )

        scope = RepairScope.for_capsule(capsule, allow_test_edits=self.allow_test_edits)

        # Rebuilt from persisted history on every call rather than accumulated
        # in the controller. A resumed run gets a fresh controller, and an
        # in-memory counter would hand a stuck step its whole budget again.
        tracker = RepairTracker(threshold=self.stuck_threshold)
        for previous in history:
            tracker.record(previous.signature)
        tracker.record(capsule.signature)

        if tracker.is_stuck():
            return RepairDecision(
                proceed=False,
                reason=(
                    f"the same failure ({capsule.signature}) occurred "
                    f"{self.stuck_threshold} time(s) in a row; "
                    "repeating the attempt will not change it"
                ),
                capsule=capsule,
                scope=scope,
                outcome=StepOutcome.BLOCKED,
            )

        attempts_used = len(history)
        if attempts_used >= self.limits.repair_attempts_per_step:
            return RepairDecision(
                proceed=False,
                reason=(
                    f"repair budget spent ({self.limits.repair_attempts_per_step} "
                    "attempts per step)"
                ),
                capsule=capsule,
                scope=scope,
                outcome=StepOutcome.BLOCKED,
            )

        if not scope.allowed:
            # Nothing the failure implicates may be edited. Reporting that is
            # honest; widening the scope to have something to do is not.
            return RepairDecision(
                proceed=False,
                reason=(
                    "the failure implicates no editable file"
                    + (
                        f"; only protected test files ({', '.join(sorted(scope.protected))}) "
                        "are involved"
                        if scope.protected
                        else ""
                    )
                ),
                capsule=capsule,
                scope=scope,
                outcome=StepOutcome.BLOCKED,
            )

        return RepairDecision(
            proceed=True,
            reason=f"attempt {capsule.attempt} of {self.limits.repair_attempts_per_step + 1}",
            capsule=capsule,
            scope=scope,
            outcome=StepOutcome.REPAIRABLE,
        )

    def _prior_lesson(self, signature: str) -> str:
        """What fixed this signature in an earlier task, if anything did.

        Only a *resolution* crosses over. "This failed before" without a fix
        is noise in a repair frame -- the capsule already says the failure is
        happening now.
        """
        if self.memory is None or not signature:
            return ""
        lesson = self.memory.lesson(signature)
        if lesson is None or not lesson.resolution:
            return ""
        return f"{lesson.resolution} (worked after {lesson.occurrences} occurrence(s))"

    def _history(self, step_id: StepId | None) -> tuple[RepairAttempt, ...]:
        """Previous failures for this step, oldest first."""
        return tuple(
            RepairAttempt(
                attempt=record.attempt,
                signature=record.signature,
                summary=f"{record.kind.value}: {record.actual or record.expected}"[:200],
            )
            for record in self.store.failures_for(self.task_id, step_id=step_id)
        )


__all__ = [
    "RepairController",
    "RepairDecision",
    "RepairScope",
    "looks_like_a_test",
]
