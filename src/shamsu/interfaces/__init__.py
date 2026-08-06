"""Protocols defining every seam in the v2 runtime.

Depend on these, not on concrete implementations. A package that needs a
capability imports the Protocol here and writes against it; the wiring happens
once, at the composition root.

This is the same discipline as v1's `interfaces.py`, which worked well enough
that consumers of unbuilt dependencies could write a stub and keep moving. What
is different is scope: these describe the *runtime's* seams, and the runtime --
not the model -- is what holds the system together.

Delivered so far
----------------
``cancellation``       Cancellation tokens, `Cancelled`, `FeedbackInterrupt`.
``enums``              The shared vocabulary. Effectively frozen.
``ids``                Distinct `NewType` identifiers.
``tools``              Tool contracts and the gateway.
``artifacts``          Artifact store, generators, freshness, contradictions.
``models``             Local model client and output contracts.
``code_intelligence``  Structural retrieval and impact analysis.
``context``            The context compiler and its token budget.

Landing with their record types
-------------------------------
``state``    StateStore and the typed records -- PR 3, alongside the SQLite
             schema they persist to.
``runtime``  RunController -- PR 4, alongside run registration and events.

Both are deliberately absent rather than stubbed: a protocol whose method
signatures reference record types that do not exist yet is not a contract, it
is a placeholder that will need rewriting.
"""

from shamsu.interfaces.artifacts import (
    Artifact,
    ArtifactGenerationError,
    ArtifactGenerator,
    ArtifactMeta,
    ArtifactStore,
    Contradiction,
    SourceRef,
)
from shamsu.interfaces.cancellation import (
    CancellationToken,
    Cancelled,
    FeedbackInterrupt,
    NullCancellationToken,
)
from shamsu.interfaces.code_intelligence import (
    CodeIndex,
    ImpactReport,
    LineRange,
    SearchHit,
    SemanticIndex,
    SymbolRef,
)
from shamsu.interfaces.context import (
    ContextCompiler,
    ContextFrame,
    ContextSection,
    TokenBudget,
)
from shamsu.interfaces.enums import (
    AgentState,
    ApprovalDecision,
    ArtifactKind,
    ArtifactStatus,
    EvidenceKind,
    FailureKind,
    Phase,
    Risk,
    RunStatus,
    StepOutcome,
    TaskKind,
)
from shamsu.interfaces.ids import (
    ApprovalId,
    ArtifactId,
    CheckpointId,
    EvidenceId,
    FailureId,
    PlanId,
    ProjectId,
    RunId,
    StepId,
    TaskId,
    ToolEventId,
)
from shamsu.interfaces.models import (
    ModelClient,
    ModelContractError,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelTimeout,
    ModelUnavailable,
    ModelUsage,
    OutputNormalizer,
)
from shamsu.interfaces.tools import (
    Tool,
    ToolContract,
    ToolGateway,
    ToolPolicyViolation,
    ToolRequest,
    ToolResult,
)

__all__ = [
    "AgentState",
    "ApprovalDecision",
    "ApprovalId",
    "Artifact",
    "ArtifactGenerationError",
    "ArtifactGenerator",
    "ArtifactId",
    "ArtifactKind",
    "ArtifactMeta",
    "ArtifactStatus",
    "ArtifactStore",
    "CancellationToken",
    "Cancelled",
    "CheckpointId",
    "CodeIndex",
    "ContextCompiler",
    "ContextFrame",
    "ContextSection",
    "Contradiction",
    "EvidenceId",
    "EvidenceKind",
    "FailureId",
    "FailureKind",
    "FeedbackInterrupt",
    "ImpactReport",
    "LineRange",
    "ModelClient",
    "ModelContractError",
    "ModelMessage",
    "ModelRequest",
    "ModelResponse",
    "ModelTimeout",
    "ModelUnavailable",
    "ModelUsage",
    "NullCancellationToken",
    "OutputNormalizer",
    "Phase",
    "PlanId",
    "ProjectId",
    "Risk",
    "RunId",
    "RunStatus",
    "SearchHit",
    "SemanticIndex",
    "SourceRef",
    "StepId",
    "StepOutcome",
    "SymbolRef",
    "TaskId",
    "TaskKind",
    "TokenBudget",
    "Tool",
    "ToolContract",
    "ToolEventId",
    "ToolGateway",
    "ToolPolicyViolation",
    "ToolRequest",
    "ToolResult",
]
