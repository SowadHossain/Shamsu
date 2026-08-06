"""Lightweight project memory (plan section 13.1, layer 2).

Graphiti is not on the critical path. What a coding agent actually needs to
remember between tasks is small, checkable, and mostly structured: how this
project runs its tests, what was decided about its architecture, and which
failures it has seen before.

Three kinds of knowledge, kept apart because they age differently. Facts depend
on files and can go stale. Decisions are history and never do -- a decision that
was made stays made, even when the code it produced has been rewritten. Lessons
are keyed by error signature so a failure is recognisable across tasks.

The property this package exists to protect is the second half of Milestone 9's
exit condition: memory must improve task success **without increasing
stale-context errors**. So confidence is derived from how a fact was learned
rather than declared, contradictions are recorded instead of overwritten, and
anything unverified carries a label into the frame.

Milestone 9. See plan section 13.
"""

from shamsu.memory.records import (
    BASE_CONFIDENCE,
    TRUSTED,
    ArchitectureDecision,
    MemoryRecord,
    ProjectFact,
)
from shamsu.memory.store import (
    CONFIRM_STEP,
    CONTRADICT_STEP,
    MemoryStore,
    combined_hash,
)

__all__ = [
    "BASE_CONFIDENCE",
    "CONFIRM_STEP",
    "CONTRADICT_STEP",
    "TRUSTED",
    "ArchitectureDecision",
    "MemoryRecord",
    "MemoryStore",
    "ProjectFact",
    "combined_hash",
]
