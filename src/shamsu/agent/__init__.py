"""Task classification, planning, step execution, repair, and completion.

Each is a bounded controller invoked by the runtime, not a loop in its own
right. The model answers "what next?"; the runtime decides whether there is a
next.

Milestones 4-7. See plan sections 20, 21, 25, 27.
"""

from shamsu.agent.readonly import (
    InvestigationResult,
    Observation,
    ReadOnlyAgent,
    is_grounded,
)

__all__ = ["InvestigationResult", "Observation", "ReadOnlyAgent", "is_grounded"]
