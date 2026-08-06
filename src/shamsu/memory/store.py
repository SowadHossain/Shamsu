"""Persisting and recalling project memory.

Milestone 9's exit condition is the design brief, and its second half is the
hard part: *memory improves task success **without increasing stale-context
errors***. Adding recall is easy. Adding recall that never asserts something
that stopped being true is the work.

Four mechanisms carry it.

**Confidence is derived, not declared.** It starts from how the fact was
learned (`BASE_CONFIDENCE`) and moves only on evidence: confirmation raises it,
contradiction lowers it. A model cannot hand in a fact rated 0.95.

**Contradiction is recorded, not resolved by overwriting.** When a new
observation disagrees with a stored fact, the stored one loses confidence and
the new statement replaces it *only* if the new origin is at least as strong.
An asserted claim cannot overwrite an observed one.

**Staleness is computed from content hashes.** A fact records the paths it was
learned from. When those files change, the fact is marked unverified — kept,
because it is probably still true, but never again stated as current.

**Recall is budgeted and labelled.** `recall` returns the highest-confidence
facts that fit, and anything unverified or low-confidence carries a label into
the frame. Invariant 4 is not advisory.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from shamsu.artifacts.hashing import hash_text
from shamsu.interfaces.enums import DecisionStatus, FactKind, FactOrigin, MemoryKind
from shamsu.interfaces.ids import (
    DecisionId,
    FactId,
    MemoryId,
    ProjectId,
    TaskId,
    ToolEventId,
)
from shamsu.memory.records import (
    BASE_CONFIDENCE,
    TRUSTED,
    ArchitectureDecision,
    MemoryRecord,
    ProjectFact,
)
from shamsu.state.records import new_id, utcnow
from shamsu.state.store import StateStore

#: How much one confirmation or contradiction moves confidence. Small on
#: purpose: a fact should need several independent confirmations to become
#: authoritative, and one contradiction should not erase a well-established
#: one — but it should be enough to drop a shaky fact below `TRUSTED`.
CONFIRM_STEP = 0.1
CONTRADICT_STEP = 0.25

#: Origin precedence. A weaker origin may confirm a fact but may not restate
#: it: a model asserting something must not be able to overwrite what a tool
#: observed, however confidently it phrases it.
_PRECEDENCE: dict[FactOrigin, int] = {
    FactOrigin.ASSERTED: 0,
    FactOrigin.DERIVED: 1,
    FactOrigin.OBSERVED: 2,
    FactOrigin.USER: 3,
}


class MemoryStore:
    """Project facts, architecture decisions, and failure lessons."""

    def __init__(self, store: StateStore, project_id: ProjectId) -> None:
        self._store = store
        self._project_id = project_id

    # -- facts -------------------------------------------------------------

    def learn(
        self,
        kind: FactKind,
        subject: str,
        statement: str,
        *,
        origin: FactOrigin,
        source_event_id: ToolEventId | None = None,
        evidence_paths: Sequence[str] = (),
        evidence_hash: str = "",
    ) -> ProjectFact:
        """Record a fact, or reconcile it with what is already known.

        Three outcomes, and which one happens is decided here rather than by
        the caller:

        * **New** — stored at the confidence its origin earns.
        * **Agrees** — confirmed, confidence rises, evidence refreshed.
        * **Disagrees** — a contradiction. Confidence falls. The statement is
          replaced only when the new origin is at least as strong as the
          stored one, so an assertion cannot overwrite an observation.

        Raises:
            ValueError: an OBSERVED fact without a source event. Observation
                means "a tool did this and the runtime watched"; without the
                event id that is just an assertion wearing a better label.
        """
        if origin is FactOrigin.OBSERVED and not source_event_id:
            raise ValueError(
                "an observed fact needs the tool event that produced it; "
                "use FactOrigin.ASSERTED for a claim with nothing behind it"
            )

        existing = self.fact(kind, subject)
        now = utcnow()

        if existing is None:
            fact = ProjectFact(
                fact_id=FactId(new_id()),
                project_id=self._project_id,
                kind=kind,
                subject=subject,
                statement=statement,
                origin=origin,
                source_event_id=source_event_id,
                confidence=BASE_CONFIDENCE[origin],
                evidence_paths=tuple(evidence_paths),
                evidence_hash=evidence_hash,
                created_at=now,
                updated_at=now,
            )
            self._insert_fact(fact)
            return fact

        agrees = _same_claim(existing.statement, statement)
        outranks = _PRECEDENCE[origin] >= _PRECEDENCE[existing.origin]

        updates: dict[str, Any] = {"updated_at": now, "verified": True}
        if agrees:
            updates["confirmations"] = existing.confirmations + 1
            updates["confidence"] = min(1.0, existing.confidence + CONFIRM_STEP)
        else:
            updates["contradictions"] = existing.contradictions + 1
            updates["confidence"] = max(0.0, existing.confidence - CONTRADICT_STEP)
            if outranks:
                updates["statement"] = statement
                updates["origin"] = origin
                updates["source_event_id"] = source_event_id
                # A restated fact starts over: the confirmations belonged to
                # the claim that was just replaced.
                updates["confirmations"] = 0
                updates["confidence"] = BASE_CONFIDENCE[origin]

        if evidence_paths:
            updates["evidence_paths"] = tuple(evidence_paths)
            updates["evidence_hash"] = evidence_hash

        updated = existing.model_copy(update=updates)
        self._update_fact(updated)
        return updated

    def fact(self, kind: FactKind, subject: str) -> ProjectFact | None:
        with self._store.reading() as connection:
            row = connection.execute(
                "SELECT * FROM project_facts WHERE project_id=? AND kind=? AND subject=?",
                (self._project_id, kind.value, subject),
            ).fetchone()
        return None if row is None else _fact_from_row(row)

    def facts(
        self, *, kind: FactKind | None = None, verified_only: bool = False
    ) -> Sequence[ProjectFact]:
        """Stored facts, most confident first."""
        query = "SELECT * FROM project_facts WHERE project_id=?"
        params: tuple[Any, ...] = (self._project_id,)
        if kind is not None:
            query += " AND kind=?"
            params += (kind.value,)
        if verified_only:
            query += " AND verified=1"

        with self._store.reading() as connection:
            rows = connection.execute(
                query + " ORDER BY confidence DESC, subject", params
            ).fetchall()
        return [_fact_from_row(row) for row in rows]

    def revalidate(self, current_hashes: dict[str, str]) -> Sequence[FactId]:
        """Mark facts whose evidence changed as unverified.

        The context-invalidation half of Milestone 9. Content hashes, not
        mtimes — the same reason the artifact registry uses them: a file
        restored from a backup has a new mtime and identical content, and
        invalidating on that is how a memory layer trains its users to ignore
        it.

        Returns the ids that changed state, so a caller can report what went
        stale rather than discovering it later.
        """
        invalidated: list[FactId] = []

        for fact in self.facts():
            if not fact.evidence_paths or not fact.verified:
                continue
            if combined_hash(fact.evidence_paths, current_hashes) == fact.evidence_hash:
                continue
            self._update_fact(fact.model_copy(update={"verified": False, "updated_at": utcnow()}))
            invalidated.append(fact.fact_id)

        return tuple(invalidated)

    def recall(
        self,
        *,
        limit: int = 8,
        include_unverified: bool = True,
        minimum_confidence: float = 0.0,
    ) -> str:
        """The project-facts block for a frame.

        Ordered by confidence, capped by `limit`, and labelled. A caller that
        wants only what is currently true passes `include_unverified=False` —
        but the default keeps stale facts *visible and marked*, because
        "SHAMSU used to believe X" is often exactly what a user needs to see
        when the agent does something surprising.
        """
        selected = [
            fact
            for fact in self.facts()
            if fact.confidence >= minimum_confidence and (include_unverified or fact.verified)
        ]
        if not selected:
            return ""

        # Verified first, then by confidence: a trusted current fact should
        # never be evicted by a stale one that once scored higher.
        selected.sort(key=lambda fact: (not fact.verified, -fact.confidence, fact.subject))
        return "\n".join(fact.render() for fact in selected[:limit])

    # -- decisions ---------------------------------------------------------

    def record_decision(
        self,
        title: str,
        decision: str,
        *,
        context: str = "",
        alternatives: Sequence[str] = (),
        consequences: Sequence[str] = (),
        related_paths: Sequence[str] = (),
        related_tasks: Sequence[TaskId] = (),
        supersedes: DecisionId | None = None,
    ) -> ArchitectureDecision:
        """Record an ADR, superseding a previous one when given.

        Raises:
            KeyError: `supersedes` names a decision that does not exist. A
                dangling supersedes pointer breaks the only chain that can
                answer "what did we decide before?".
        """
        if supersedes is not None and self.decision(supersedes) is None:
            raise KeyError(f"no decision {supersedes!r} to supersede")

        record = ArchitectureDecision(
            decision_id=DecisionId(new_id()),
            project_id=self._project_id,
            title=title,
            context=context,
            decision=decision,
            alternatives=tuple(alternatives),
            consequences=tuple(consequences),
            related_paths=tuple(related_paths),
            related_tasks=tuple(related_tasks),
            supersedes=supersedes,
        )

        with self._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO architecture_decisions (
                    decision_id, project_id, title, context, decision, alternatives,
                    consequences, status, related_paths, related_tasks, supersedes,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.decision_id,
                    record.project_id,
                    record.title,
                    record.context,
                    record.decision,
                    json.dumps(list(record.alternatives)),
                    json.dumps(list(record.consequences)),
                    record.status.value,
                    json.dumps(list(record.related_paths)),
                    json.dumps(list(record.related_tasks)),
                    record.supersedes,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
            if supersedes is not None:
                connection.execute(
                    "UPDATE architecture_decisions SET status=?, updated_at=? WHERE decision_id=?",
                    (DecisionStatus.SUPERSEDED.value, utcnow().isoformat(), supersedes),
                )

        return record

    def decision(self, decision_id: DecisionId) -> ArchitectureDecision | None:
        with self._store.reading() as connection:
            row = connection.execute(
                "SELECT * FROM architecture_decisions WHERE decision_id=?", (decision_id,)
            ).fetchone()
        return None if row is None else _decision_from_row(row)

    def decisions(self, *, status: DecisionStatus | None = None) -> Sequence[ArchitectureDecision]:
        """Decisions, oldest first. Superseded ones are kept and returned."""
        query = "SELECT * FROM architecture_decisions WHERE project_id=?"
        params: tuple[Any, ...] = (self._project_id,)
        if status is not None:
            query += " AND status=?"
            params += (status.value,)

        with self._store.reading() as connection:
            rows = connection.execute(query + " ORDER BY created_at, rowid", params).fetchall()
        return [_decision_from_row(row) for row in rows]

    def decisions_for_paths(self, paths: Sequence[str]) -> Sequence[ArchitectureDecision]:
        """Accepted decisions touching any of these files.

        The retrieval that makes ADRs useful during a task: the agent about to
        edit `src/shamsu/state/store.py` should see the decision that says why
        SQLite is authoritative, and not the other eleven.
        """
        wanted = {path.replace("\\", "/").lstrip("./") for path in paths}
        return tuple(
            record
            for record in self.decisions(status=DecisionStatus.ACCEPTED)
            if wanted & set(record.related_paths)
        )

    # -- lessons -----------------------------------------------------------

    def remember_failure(
        self,
        signature: str,
        statement: str,
        *,
        resolution: str = "",
        task_id: TaskId | None = None,
        related_paths: Sequence[str] = (),
    ) -> MemoryRecord:
        """Record a failure lesson, merging with any earlier one.

        A recurrence increments `occurrences` rather than inserting a second
        row, so "this has happened four times" is answerable. A resolution
        arriving later fills in one that was previously empty — the failure is
        usually recorded before anyone knows what fixes it.
        """
        existing = self.lesson(signature) if signature else None
        now = utcnow()

        if existing is None:
            record = MemoryRecord(
                memory_id=MemoryId(new_id()),
                project_id=self._project_id,
                task_id=task_id,
                kind=MemoryKind.FAILURE_LESSON,
                signature=signature,
                statement=statement,
                resolution=resolution,
                confidence=0.7 if resolution else 0.4,
                related_paths=tuple(related_paths),
                created_at=now,
                updated_at=now,
            )
            self._insert_memory(record)
            return record

        updated = existing.model_copy(
            update={
                "occurrences": existing.occurrences + 1,
                # A lesson that keeps recurring with a known fix is a *better*
                # lesson, not a worse one: it has been observed to apply again.
                "resolution": resolution or existing.resolution,
                "confidence": min(1.0, existing.confidence + (0.2 if resolution else 0.05)),
                "related_paths": tuple(dict.fromkeys([*existing.related_paths, *related_paths])),
                "updated_at": now,
            }
        )
        self._update_memory(updated)
        return updated

    def lesson(self, signature: str) -> MemoryRecord | None:
        """The lesson for an error signature, if this has happened before."""
        if not signature:
            return None
        with self._store.reading() as connection:
            row = connection.execute(
                "SELECT * FROM memory_records WHERE project_id=? AND signature=?",
                (self._project_id, signature),
            ).fetchone()
        return None if row is None else _memory_from_row(row)

    def lessons(self) -> Sequence[MemoryRecord]:
        with self._store.reading() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_records WHERE project_id=?"
                " ORDER BY occurrences DESC, updated_at DESC",
                (self._project_id,),
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    # -- persistence -------------------------------------------------------

    def _insert_fact(self, fact: ProjectFact) -> None:
        with self._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO project_facts (
                    fact_id, project_id, kind, subject, statement, origin,
                    source_event_id, confidence, confirmations, contradictions,
                    evidence_paths, evidence_hash, verified, superseded_by,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                _fact_row(fact),
            )

    def _update_fact(self, fact: ProjectFact) -> None:
        with self._store.transaction() as connection:
            connection.execute(
                """
                UPDATE project_facts SET
                    statement=?, origin=?, source_event_id=?, confidence=?,
                    confirmations=?, contradictions=?, evidence_paths=?,
                    evidence_hash=?, verified=?, superseded_by=?, updated_at=?
                WHERE fact_id=?
                """,
                (
                    fact.statement,
                    fact.origin.value,
                    fact.source_event_id,
                    fact.confidence,
                    fact.confirmations,
                    fact.contradictions,
                    json.dumps(list(fact.evidence_paths)),
                    fact.evidence_hash,
                    int(fact.verified),
                    fact.superseded_by,
                    fact.updated_at.isoformat(),
                    fact.fact_id,
                ),
            )

    def _insert_memory(self, record: MemoryRecord) -> None:
        with self._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO memory_records (
                    memory_id, project_id, task_id, kind, signature, statement,
                    resolution, confidence, occurrences, related_paths,
                    created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    record.memory_id,
                    record.project_id,
                    record.task_id,
                    record.kind.value,
                    record.signature,
                    record.statement,
                    record.resolution,
                    record.confidence,
                    record.occurrences,
                    json.dumps(list(record.related_paths)),
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )

    def _update_memory(self, record: MemoryRecord) -> None:
        with self._store.transaction() as connection:
            connection.execute(
                """
                UPDATE memory_records SET
                    statement=?, resolution=?, confidence=?, occurrences=?,
                    related_paths=?, updated_at=?
                WHERE memory_id=?
                """,
                (
                    record.statement,
                    record.resolution,
                    record.confidence,
                    record.occurrences,
                    json.dumps(list(record.related_paths)),
                    record.updated_at.isoformat(),
                    record.memory_id,
                ),
            )


def combined_hash(paths: Sequence[str], hashes: dict[str, str]) -> str:
    """A single hash over the current content of `paths`.

    A missing path contributes the literal `<missing>` rather than being
    skipped, so deleting a file a fact depended on invalidates the fact.
    Skipping would make deletion the one change memory never notices.
    """
    parts = [f"{path}:{hashes.get(path, '<missing>')}" for path in sorted(paths)]
    return hash_text("\n".join(parts))


def _same_claim(stored: str, incoming: str) -> bool:
    """Whether two statements say the same thing.

    Whitespace- and case-insensitive equality, and nothing cleverer. A fuzzy
    match would silently treat "uses pytest" and "does not use pytest" as
    agreement at some similarity threshold, and the contradiction path exists
    precisely to catch that.
    """
    return " ".join(stored.lower().split()) == " ".join(incoming.lower().split())


def _fact_row(fact: ProjectFact) -> tuple[Any, ...]:
    return (
        fact.fact_id,
        fact.project_id,
        fact.kind.value,
        fact.subject,
        fact.statement,
        fact.origin.value,
        fact.source_event_id,
        fact.confidence,
        fact.confirmations,
        fact.contradictions,
        json.dumps(list(fact.evidence_paths)),
        fact.evidence_hash,
        int(fact.verified),
        fact.superseded_by,
        fact.created_at.isoformat(),
        fact.updated_at.isoformat(),
    )


def _fact_from_row(row: sqlite3.Row) -> ProjectFact:
    return ProjectFact(
        fact_id=FactId(row["fact_id"]),
        project_id=ProjectId(row["project_id"]),
        kind=FactKind(row["kind"]),
        subject=row["subject"],
        statement=row["statement"],
        origin=FactOrigin(row["origin"]),
        source_event_id=ToolEventId(row["source_event_id"]) if row["source_event_id"] else None,
        confidence=row["confidence"],
        confirmations=row["confirmations"],
        contradictions=row["contradictions"],
        evidence_paths=tuple(json.loads(row["evidence_paths"])),
        evidence_hash=row["evidence_hash"],
        verified=bool(row["verified"]),
        superseded_by=FactId(row["superseded_by"]) if row["superseded_by"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _decision_from_row(row: sqlite3.Row) -> ArchitectureDecision:
    return ArchitectureDecision(
        decision_id=DecisionId(row["decision_id"]),
        project_id=ProjectId(row["project_id"]),
        title=row["title"],
        context=row["context"],
        decision=row["decision"],
        alternatives=tuple(json.loads(row["alternatives"])),
        consequences=tuple(json.loads(row["consequences"])),
        status=DecisionStatus(row["status"]),
        related_paths=tuple(json.loads(row["related_paths"])),
        related_tasks=tuple(TaskId(item) for item in json.loads(row["related_tasks"])),
        supersedes=DecisionId(row["supersedes"]) if row["supersedes"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _memory_from_row(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        memory_id=MemoryId(row["memory_id"]),
        project_id=ProjectId(row["project_id"]),
        task_id=TaskId(row["task_id"]) if row["task_id"] else None,
        kind=MemoryKind(row["kind"]),
        signature=row["signature"],
        statement=row["statement"],
        resolution=row["resolution"],
        confidence=row["confidence"],
        occurrences=row["occurrences"],
        related_paths=tuple(json.loads(row["related_paths"])),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


__all__ = [
    "CONFIRM_STEP",
    "CONTRADICT_STEP",
    "TRUSTED",
    "MemoryStore",
    "combined_hash",
]
