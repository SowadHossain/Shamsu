"""Typed records for project memory (plan §13.1, layer 2).

Three kinds of knowledge, kept apart because they age differently.

**Facts** are small checkable claims — "tests run with `pytest -q`", "this
project uses SQLAlchemy 2.0". They depend on files, so they can go stale, and
they carry the hash of what they were learned from.

**Decisions** are ADRs. They are narrative, they supersede rather than update,
and *a file changing does not invalidate them*: a decision that was made stays
made, even when the code it produced has since been rewritten. Conflating the
two would either invalidate history or leave stale facts standing.

**Memories** are lessons from failures, keyed by error signature so a
recurrence is recognisable across tasks.

Confidence is on facts and memories, never on decisions. A decision is not more
or less true; it is accepted, superseded, or rejected.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from shamsu.interfaces.enums import DecisionStatus, FactKind, FactOrigin, MemoryKind
from shamsu.interfaces.ids import (
    DecisionId,
    FactId,
    MemoryId,
    ProjectId,
    TaskId,
    ToolEventId,
)
from shamsu.state.records import utcnow

#: Starting confidence by origin. An observed fact traces to a tool execution
#: the runtime watched; an asserted one is a model's claim. They must not enter
#: the store at the same weight, and the model does not get to choose.
BASE_CONFIDENCE: dict[FactOrigin, float] = {
    FactOrigin.USER: 1.0,
    FactOrigin.OBSERVED: 0.8,
    FactOrigin.DERIVED: 0.6,
    FactOrigin.ASSERTED: 0.3,
}

#: Confidence at or above this is safe to state plainly in a frame. Below it, a
#: fact is either labelled or withheld -- see `MemoryStore.recall`.
TRUSTED = 0.5


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ProjectFact(_Record):
    """One checkable claim about the project.

    `evidence_paths` and `evidence_hash` are what make staleness computable.
    A fact learned by reading `pyproject.toml` records that path and the hash
    of its content at the time; when the file changes, the fact is marked
    unverified rather than deleted. Deleting would lose a claim that is
    probably still true; leaving it verified would state it as current when
    nothing has rechecked it.
    """

    fact_id: FactId
    project_id: ProjectId
    kind: FactKind
    subject: str = Field(min_length=1, description="What the fact is about.")
    statement: str = Field(min_length=1)

    origin: FactOrigin
    source_event_id: ToolEventId | None = Field(
        default=None,
        description="The tool execution behind an OBSERVED fact. None for the other origins.",
    )

    confidence: float = Field(ge=0.0, le=1.0)
    confirmations: int = Field(default=0, ge=0)
    contradictions: int = Field(default=0, ge=0)

    evidence_paths: tuple[str, ...] = ()
    evidence_hash: str = Field(
        default="", description="Combined hash of `evidence_paths` when last verified."
    )
    verified: bool = Field(
        default=True,
        description="False once the evidence changed. The fact is kept, but labelled.",
    )
    superseded_by: FactId | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def trusted(self) -> bool:
        """Whether this can be stated plainly rather than hedged."""
        return self.verified and self.confidence >= TRUSTED

    def render(self) -> str:
        """One line, labelled when it is not current.

        Invariant 4: stale context may reach the model only with an explicit
        label. An unverified fact that reads identically to a verified one is
        the whole stale-context failure mode in a single string.
        """
        line = f"{self.subject}: {self.statement}"
        if not self.verified:
            return f"{line} [UNVERIFIED — the files this was learned from have changed]"
        if self.confidence < TRUSTED:
            return f"{line} [low confidence — {self.origin.value}, unconfirmed]"
        return line


class ArchitectureDecision(_Record):
    """An ADR (plan §15.13).

    Superseding rather than editing is the whole point: "what did we decide,
    and what did we decide before that?" is the question an ADR exists to
    answer, and an edited decision cannot answer it.
    """

    decision_id: DecisionId
    project_id: ProjectId
    title: str = Field(min_length=1)
    context: str = ""
    decision: str = Field(min_length=1)
    alternatives: tuple[str, ...] = ()
    consequences: tuple[str, ...] = ()
    status: DecisionStatus = DecisionStatus.ACCEPTED
    related_paths: tuple[str, ...] = ()
    related_tasks: tuple[TaskId, ...] = ()
    supersedes: DecisionId | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def render(self) -> str:
        lines = [f"{self.title} [{self.status.value}]", f"  Decision: {self.decision}"]
        if self.consequences:
            lines.append("  Consequences: " + "; ".join(self.consequences))
        return "\n".join(lines)


class MemoryRecord(_Record):
    """A lesson, keyed by the error signature it was learned from.

    `signature` is the same stable hash `verification.digest` computes, so a
    failure recurring in a *different task* is still recognisable. That is the
    one thing a per-task failure table cannot do, and the reason this is not
    just a view over `failures`.
    """

    memory_id: MemoryId
    project_id: ProjectId
    task_id: TaskId | None = None
    kind: MemoryKind = MemoryKind.FAILURE_LESSON

    signature: str = ""
    statement: str = Field(min_length=1, description="What went wrong.")
    resolution: str = Field(default="", description="What actually fixed it, if anything did.")

    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    occurrences: int = Field(default=1, ge=1)
    related_paths: tuple[str, ...] = ()

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def render(self) -> str:
        line = f"Seen before ({self.occurrences}x): {self.statement}"
        if self.resolution:
            return f"{line}\n  What worked: {self.resolution}"
        return f"{line}\n  No fix was found last time."


__all__ = [
    "BASE_CONFIDENCE",
    "TRUSTED",
    "ArchitectureDecision",
    "MemoryRecord",
    "ProjectFact",
]
