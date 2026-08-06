"""Local model clients, output contracts, and output normalisation.

Small local models only. Every call has a declared output contract; a response
that does not satisfy it is a failure, not something to be creatively
interpreted.

Contracts are deliberately narrow. A small model asked for one action with
three fields is reliable; the same model asked for a plan, a rationale, and a
next step in one response is not. "One narrow decision per call" is what makes
these parseable in practice.

No client here contacts a network by default, and no test exercises live
inference — see `tests/fixtures/fake_model.py`.

Milestone 4. See plan sections 19, 20.
"""

from shamsu.models.contracts import (
    CONTRACTS,
    ImplementationPlan,
    InvestigationStep,
    PlanStepProposal,
    ProjectAssessment,
    ToolCall,
    contract_for,
    schema_hint,
)
from shamsu.models.normalization import (
    Normalised,
    extract_json_object,
    normalise,
    parse_json_response,
    strip_reasoning,
)

__all__ = [
    "CONTRACTS",
    "Normalised",
    "ImplementationPlan",
    "InvestigationStep",
    "PlanStepProposal",
    "ProjectAssessment",
    "ToolCall",
    "contract_for",
    "extract_json_object",
    "normalise",
    "parse_json_response",
    "schema_hint",
    "strip_reasoning",
]
