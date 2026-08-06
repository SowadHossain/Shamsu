"""Output contracts: the shapes a model response is allowed to take.

Every model call declares one. A response that does not satisfy its contract is
a failure — not something to be creatively reinterpreted. v1 spent sixteen
quote-repair attempts and a pile of recovery counters learning that lesson.

The contracts are deliberately narrow. A small model asked to emit one action
with three fields is reliable; the same model asked to emit a plan, a
rationale, and a next step in one response is not. "One narrow decision per
call" is not just an architectural slogan — it is what makes these parseable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolCall(BaseModel):
    """A request to run one tool."""

    model_config = ConfigDict(frozen=True)

    tool: str = Field(min_length=1)
    arguments: Mapping[str, Any] = Field(default_factory=dict)
    reason: str = Field(
        default="",
        max_length=300,
        description="One line on why this call. Recorded for the run timeline.",
    )


class InvestigationStep(BaseModel):
    """One decision in the read-only investigation loop.

    Exactly one of `tool` or `conclusion` is meaningful, selected by `action`.
    A discriminated shape rather than two optional fields, because "the model
    filled in both" and "the model filled in neither" are otherwise silent
    ambiguities the runtime has to guess about.
    """

    model_config = ConfigDict(frozen=True)

    action: Literal["call_tool", "conclude"]
    tool: ToolCall | None = None
    conclusion: str = Field(default="", max_length=4000)

    def validated(self) -> InvestigationStep:
        """Check the cross-field rule the schema alone cannot express.

        Raises:
            ValueError: the response is internally inconsistent.
        """
        if self.action == "call_tool" and self.tool is None:
            raise ValueError("action was 'call_tool' but no tool was given")
        if self.action == "conclude" and not self.conclusion.strip():
            raise ValueError("action was 'conclude' but the conclusion was empty")
        return self


class PlanStepProposal(BaseModel):
    """One proposed step, matching the planning contract in plan §21.

    `required_evidence` is free text here rather than an `EvidenceKind` enum on
    purpose: the model proposes what proof it thinks is needed, and the runtime
    maps that onto real evidence kinds. Letting the model emit enum members
    directly would let a hallucinated member become a completion requirement
    nothing can ever satisfy.
    """

    model_config = ConfigDict(frozen=True)

    title: str = Field(min_length=1, max_length=200)
    intent: str = Field(default="", max_length=600)
    files: tuple[str, ...] = Field(
        default=(), description="Files this step expects to read or change."
    )
    acceptance_criteria: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    risk: Literal["low", "medium", "high"] = "low"


class ImplementationPlan(BaseModel):
    """A grounded plan produced without modifying anything.

    This is Milestone 4's deliverable. `grounded_in` is what makes it
    checkable: the runtime can verify those files were actually read, and a
    plan citing files the agent never opened is not grounded, whatever it says
    about itself.
    """

    model_config = ConfigDict(frozen=True)

    summary: str = Field(min_length=1, max_length=1000)
    steps: tuple[PlanStepProposal, ...] = Field(min_length=1, max_length=12)
    grounded_in: tuple[str, ...] = Field(
        default=(), description="Files actually inspected while forming this plan."
    )
    open_questions: tuple[str, ...] = ()


class ProjectAssessment(BaseModel):
    """What the agent concluded about a project after inspecting it."""

    model_config = ConfigDict(frozen=True)

    summary: str = Field(min_length=1, max_length=1500)
    stack: tuple[str, ...] = ()
    entry_points: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()


#: Registry of contract name -> model, so a frame's `output_contract` string
#: resolves to something the runtime can actually parse against.
CONTRACTS: Mapping[str, type[BaseModel]] = {
    "InvestigationStep": InvestigationStep,
    "ImplementationPlan": ImplementationPlan,
    "ProjectAssessment": ProjectAssessment,
}


def contract_for(name: str) -> type[BaseModel]:
    """Resolve a contract by name.

    Raises:
        KeyError: unknown contract. A frame naming a contract that does not
            exist is a runtime bug, not a model error.
    """
    try:
        return CONTRACTS[name]
    except KeyError:
        raise KeyError(
            f"unknown output contract {name!r}; known: {', '.join(sorted(CONTRACTS))}"
        ) from None


def schema_hint(contract: type[BaseModel]) -> str:
    """A compact rendering of a contract for the prompt.

    The full JSON Schema for a nested model runs to hundreds of tokens against
    a 400-token tool-definition budget. This keeps the field names, types, and
    requiredness — which is what a model actually needs to emit valid output.
    """
    schema = contract.model_json_schema()
    required = set(schema.get("required", []))
    lines = [f"Respond with JSON matching {contract.__name__}:"]

    for name, spec in (schema.get("properties") or {}).items():
        kind = spec.get("type") or _reference_name(spec) or "any"
        marker = "required" if name in required else "optional"
        description = spec.get("description", "")
        suffix = f" — {description}" if description else ""
        lines.append(f"  {name} ({kind}, {marker}){suffix}")

    return "\n".join(lines)


def _reference_name(spec: Mapping[str, Any]) -> str | None:
    for key in ("$ref", "allOf", "anyOf"):
        value = spec.get(key)
        if isinstance(value, str):
            return value.rsplit("/", 1)[-1]
        if isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict) and "$ref" in first:
                return str(first["$ref"]).rsplit("/", 1)[-1]
    return None


__all__ = [
    "CONTRACTS",
    "ImplementationPlan",
    "InvestigationStep",
    "PlanStepProposal",
    "ProjectAssessment",
    "ToolCall",
    "contract_for",
    "schema_hint",
]
