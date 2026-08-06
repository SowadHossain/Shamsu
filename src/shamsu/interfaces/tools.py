"""The tool contract and gateway seam.

One registry, not two. v1 had `ToolRegistry` (3 tools) and `AgentToolRegistry`
(42 tools in a 3,731-line file) with different validation and gating paths,
which meant "is this tool allowed right now?" had two different answers
depending on which surface you asked.

Here a tool is a `ToolContract` plus a callable, and the gateway is the only
thing that may invoke one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.enums import ArtifactKind, EvidenceKind, Phase, Risk


class ToolContract(BaseModel):
    """Everything the runtime needs to decide whether a tool may run.

    Declared once, next to the implementation. The gateway enforces every
    field; none of this is advisory.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Stable dotted name, e.g. 'file.patch'.")
    purpose: str = Field(description="One line, shown to the model.")

    allowed_phases: frozenset[Phase] = Field(
        description="Phases in which this tool may execute. Enforced by the gateway."
    )
    risk: Risk
    requires_approval: bool = False
    reversible: bool = Field(
        description="Whether the effect can be undone by a checkpoint rollback."
    )

    timeout_seconds: float = Field(gt=0, le=3600)
    max_output_bytes: int = Field(
        gt=0,
        description=(
            "Output is capped BEFORE entering any history or context. v1's most "
            "important token-discipline mechanism, kept because the failure mode it "
            "prevents -- one oversized read crowding out everything else -- is real."
        ),
    )

    produces_evidence: frozenset[EvidenceKind] = frozenset()
    invalidates: frozenset[ArtifactKind] = frozenset()

    mutating: bool = Field(
        default=False,
        description=(
            "Whether the tool changes state outside the runtime. At most one "
            "mutating call is permitted per model decision (plan section 11)."
        ),
    )


class ToolRequest(BaseModel):
    """A validated request to run one tool."""

    model_config = ConfigDict(frozen=True)

    tool: str
    arguments: Mapping[str, Any]


class ToolResult(BaseModel):
    """The outcome of one tool execution.

    `ok=False` with an honest `error` always beats an invented success. This is
    invariant 5 and it is load-bearing: the completion gate reads these.
    """

    model_config = ConfigDict(frozen=True)

    tool: str
    ok: bool
    output: str = ""
    error: str | None = None

    truncated: bool = Field(
        default=False,
        description="Whether `output` was capped at the contract's max_output_bytes.",
    )
    original_bytes: int | None = Field(
        default=None,
        description=(
            "Size before truncation, when truncated. v1's ledger recorded this "
            "inconsistently across call sites; here it is set by the gateway alone."
        ),
    )
    duration_seconds: float = 0.0
    evidence: frozenset[EvidenceKind] = frozenset()
    invalidated: frozenset[ArtifactKind] = frozenset()


class ToolPolicyViolation(Exception):
    """Raised when a tool call is refused before execution.

    Refusal reasons: unknown tool, wrong phase, missing approval, invalid
    arguments, or a second mutating call in one decision.
    """


@runtime_checkable
class Tool(Protocol):
    """A single executable capability."""

    @property
    def contract(self) -> ToolContract: ...

    def input_schema(self) -> Mapping[str, Any]:
        """JSON Schema for the arguments, shown to the model."""
        ...

    async def execute(
        self,
        arguments: Mapping[str, Any],
        cancel: CancellationToken,
    ) -> ToolResult:
        """Run the tool. Must respect the contract's timeout and output cap."""
        ...


@runtime_checkable
class ToolGateway(Protocol):
    """The only path from a model decision to a side effect.

    Responsible, in order: resolve the tool, validate arguments against the
    schema, check the phase allowlist, check approval, enforce the
    one-mutation-per-decision rule, execute under timeout and cancellation, cap
    the output, and register evidence and artifact invalidation.
    """

    def available(self, phase: Phase) -> Sequence[ToolContract]:
        """Contracts reachable in `phase`. This is what the model is shown.

        The model is never shown a tool it cannot call, so a wrong-phase call
        is a runtime bug rather than an expected model mistake.
        """
        ...

    async def invoke(
        self,
        request: ToolRequest,
        phase: Phase,
        cancel: CancellationToken,
    ) -> ToolResult:
        """Execute one tool call under full policy enforcement.

        Raises:
            ToolPolicyViolation: refused before any side effect occurred.
        """
        ...


__all__ = [
    "Tool",
    "ToolContract",
    "ToolGateway",
    "ToolPolicyViolation",
    "ToolRequest",
    "ToolResult",
]
