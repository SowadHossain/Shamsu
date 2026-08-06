"""The context compiler seam.

The model receives a compiled frame, never a transcript and never a repository.
Each frame is assembled fresh for one decision, under an explicit token budget,
from authoritative state plus retrieved evidence.

Budget (plan section 19.2, for an 8K window):

    system and phase rules            500
    task and acceptance criteria      500
    current step and plan summary     500
    project facts and artifacts       900
    relevant source code            2,800
    latest observations               700
    tool definitions                  400
    output reserve                  1,700
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from shamsu.interfaces.enums import Phase
from shamsu.interfaces.tools import ToolContract


class TokenBudget(BaseModel):
    """Per-section token allocation for one compiled frame."""

    model_config = ConfigDict(frozen=True)

    system_and_phase: int = 500
    task_and_criteria: int = 500
    step_and_plan: int = 500
    facts_and_artifacts: int = 900
    source_code: int = 2800
    observations: int = 700
    tool_definitions: int = 400
    output_reserve: int = 1700

    @property
    def input_total(self) -> int:
        """Everything except the output reserve."""
        return (
            self.system_and_phase
            + self.task_and_criteria
            + self.step_and_plan
            + self.facts_and_artifacts
            + self.source_code
            + self.observations
            + self.tool_definitions
        )

    @property
    def total(self) -> int:
        return self.input_total + self.output_reserve


class ContextSection(BaseModel):
    """One rendered section of a frame.

    `stale_warning` is why this type exists rather than a plain string: a
    section built from a stale artifact must carry that fact to the model.
    Plan section 17.1 forbids sending stale structural claims unlabelled.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    content: str
    tokens: int = Field(ge=0)
    stale_warning: str | None = None


class ContextFrame(BaseModel):
    """The complete input to one model call."""

    model_config = ConfigDict(frozen=True)

    phase: Phase
    sections: tuple[ContextSection, ...]
    allowed_tools: tuple[ToolContract, ...]
    output_contract: str = Field(description="Name of the schema the response must satisfy.")

    budget: TokenBudget
    tokens_used: int = Field(ge=0)
    dropped_sections: tuple[str, ...] = Field(
        default=(),
        description=(
            "Sections omitted to fit the budget. Recorded, not silent: a decision "
            "made without the source code section is a different kind of decision."
        ),
    )

    def render(self) -> str:
        """Assemble the frame into prompt text."""
        parts: list[str] = []
        for section in self.sections:
            header = f"[{section.name.upper()}]"
            if section.stale_warning:
                header += f"\n(STALE: {section.stale_warning})"
            parts.append(f"{header}\n{section.content}")
        return "\n\n".join(parts)


@runtime_checkable
class ContextCompiler(Protocol):
    """Builds compact task packets from authoritative state.

    Implementations must be deterministic given the same state: the same inputs
    produce the same frame. This is what makes a failed decision reproducible.
    """

    def compile(
        self,
        phase: Phase,
        budget: TokenBudget,
        allowed_tools: Sequence[ToolContract],
    ) -> ContextFrame:
        """Assemble the frame for the next decision.

        Sections are filled in priority order and dropped from the lowest
        priority upward when the budget binds. Hot context -- current task,
        current step, acceptance criteria, latest result -- is never dropped;
        if it does not fit, that is an error, not a silent truncation.
        """
        ...


__all__ = [
    "ContextCompiler",
    "ContextFrame",
    "ContextSection",
    "TokenBudget",
]
