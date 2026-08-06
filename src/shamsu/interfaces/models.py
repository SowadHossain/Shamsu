"""The model seam.

Small local models only. Two rules shape this interface:

1. **Every call has a declared output contract.** The caller says what shape it
   expects; a response that does not satisfy it is a failure, not something to
   be creatively salvaged. v1 spent 16 quote-repair attempts and a pile of
   recovery counters on this problem.
2. **Every call is cancellable.** `generate` takes a token and must abandon the
   request when it fires, mid-stream if necessary.

No implementation in this package contacts a network by default. Tests and CI
run against a deterministic fake -- see `tests/fixtures`. Live local inference
is exercised only on a GPU-equipped development machine.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from shamsu.interfaces.cancellation import CancellationToken

ContractT = TypeVar("ContractT", bound=BaseModel)


class ModelMessage(BaseModel):
    """One message in a request.

    Note there is no conversation history type here. The context compiler
    builds a fresh frame per call; it does not replay a transcript. Dropping
    v1's prompt-conversation replay model is deliberate (plan section 8.4).
    """

    model_config = ConfigDict(frozen=True)

    role: str = Field(pattern="^(system|user|assistant)$")
    content: str


class ModelRequest(BaseModel):
    """One narrow decision put to the model."""

    model_config = ConfigDict(frozen=True)

    messages: tuple[ModelMessage, ...]
    max_output_tokens: int = Field(gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    stop: tuple[str, ...] = ()

    output_schema: Mapping[str, Any] | None = Field(
        default=None,
        description="JSON Schema the response must satisfy, when structured output is required.",
    )


class ModelUsage(BaseModel):
    """Token accounting, feeding the `tokens_per_verified_task` metric."""

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int = 0
    output_tokens: int = 0


class ModelResponse(BaseModel):
    """A raw model response, before contract parsing."""

    model_config = ConfigDict(frozen=True)

    text: str
    usage: ModelUsage = ModelUsage()
    truncated: bool = Field(
        default=False,
        description="Whether generation stopped at the token limit rather than naturally.",
    )
    duration_seconds: float = 0.0


class ModelContractError(Exception):
    """The response did not satisfy the declared output contract.

    Carries the raw text so a failure capsule can record what the model
    actually said. The runtime decides whether to retry; the client never
    silently repairs, reinterprets, or guesses.
    """

    def __init__(self, message: str, raw_text: str) -> None:
        super().__init__(message)
        self.raw_text = raw_text


class ModelTimeout(Exception):
    """The model did not respond within the caller's budget."""


class ModelUnavailable(Exception):
    """No local model could be reached.

    Distinct from a timeout: the runtime should report this as an environment
    problem, not retry it as a transient failure.
    """


@runtime_checkable
class ModelClient(Protocol):
    """A local language model."""

    @property
    def name(self) -> str: ...

    @property
    def context_tokens(self) -> int:
        """The model's context window. The compiler budgets against this."""
        ...

    def count_tokens(self, text: str) -> int:
        """Token count for budgeting.

        Must not require a live model -- the compiler calls this while building
        a frame, long before any request is sent.
        """
        ...

    async def generate(
        self,
        request: ModelRequest,
        cancel: CancellationToken,
    ) -> ModelResponse:
        """Produce one response.

        Raises:
            ModelTimeout, ModelUnavailable
            Cancelled: `cancel` fired; the in-flight request was abandoned.
        """
        ...

    async def generate_typed(
        self,
        request: ModelRequest,
        contract: type[ContractT],
        cancel: CancellationToken,
    ) -> ContractT:
        """Produce a response parsed into `contract`.

        Raises:
            ModelContractError: the response did not satisfy the contract.
        """
        ...


@runtime_checkable
class OutputNormalizer(Protocol):
    """Recovers structured output from imperfect small-model text.

    A migration candidate from `legacy-code/shamsu/llm/output.py`, which is
    battle-tested against real small-model failure modes. The constraint that
    was missing in v1: normalisation may only ever *reshape* what the model
    said. Inferring intent from prose is out of scope -- that is what caused
    v1's implicit success classification.
    """

    def normalize(self, raw: str, schema: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Return parsed data, or None if it cannot be recovered honestly."""
        ...

    def attempts(self) -> Sequence[str]:
        """Recovery strategies tried, for the failure capsule."""
        ...


__all__ = [
    "ModelClient",
    "ModelContractError",
    "ModelMessage",
    "ModelRequest",
    "ModelResponse",
    "ModelTimeout",
    "ModelUnavailable",
    "ModelUsage",
    "OutputNormalizer",
]
