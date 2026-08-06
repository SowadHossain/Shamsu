"""Distinct identifier types.

These are ``NewType`` aliases over ``str``, so mypy rejects passing a
``TaskId`` where a ``RunId`` is expected. This is cheap and catches a whole
class of wiring bug that v1 could only catch at runtime.
"""

from __future__ import annotations

from typing import NewType

ProjectId = NewType("ProjectId", str)
RunId = NewType("RunId", str)
TaskId = NewType("TaskId", str)
PlanId = NewType("PlanId", str)
StepId = NewType("StepId", str)
ToolEventId = NewType("ToolEventId", str)
EvidenceId = NewType("EvidenceId", str)
ApprovalId = NewType("ApprovalId", str)
CheckpointId = NewType("CheckpointId", str)
ArtifactId = NewType("ArtifactId", str)
FailureId = NewType("FailureId", str)
FactId = NewType("FactId", str)
DecisionId = NewType("DecisionId", str)
MemoryId = NewType("MemoryId", str)

__all__ = [
    "ApprovalId",
    "ArtifactId",
    "CheckpointId",
    "DecisionId",
    "EvidenceId",
    "FactId",
    "FailureId",
    "MemoryId",
    "PlanId",
    "ProjectId",
    "RunId",
    "StepId",
    "TaskId",
    "ToolEventId",
]
