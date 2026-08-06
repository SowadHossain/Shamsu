"""A deterministic `ModelClient` for tests.

The whole point of v2's architecture is that the runtime, not the model, holds
the system together -- which means almost all of it can be tested without a
model at all. This fake makes that concrete: scripted responses, no network, no
GPU, no nondeterminism.

It also models the failure modes that matter, because those are what v1 handled
badly: cancellation mid-call, contract violations, and truncation.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.models import (
    ModelContractError,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
    ModelUsage,
)

ContractT = TypeVar("ContractT", bound=BaseModel)


class FakeModelClient:
    """Replays scripted responses in order.

    Args:
        responses: Response texts, returned one per `generate` call.
        name: Reported model name.
        context_tokens: Reported context window.
        unavailable: When True, every call raises `ModelUnavailable`, for
            testing the degraded path.
    """

    def __init__(
        self,
        responses: Iterable[str] = (),
        *,
        name: str = "fake-small-model",
        context_tokens: int = 8192,
        unavailable: bool = False,
    ) -> None:
        self._responses: deque[str] = deque(responses)
        self._name = name
        self._context_tokens = context_tokens
        self._unavailable = unavailable
        self.requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def context_tokens(self) -> int:
        return self._context_tokens

    def count_tokens(self, text: str) -> int:
        """A deliberately crude but *stable* approximation.

        Roughly four characters per token. Real tokenisation belongs to the
        real client; what tests need is a function whose answer never changes
        between runs, so budget assertions are reproducible.
        """
        return max(1, (len(text) + 3) // 4)

    async def generate(
        self,
        request: ModelRequest,
        cancel: CancellationToken,
    ) -> ModelResponse:
        cancel.raise_if_cancelled()

        if self._unavailable:
            raise ModelUnavailable(f"{self._name} is not reachable")

        self.requests.append(request)

        if not self._responses:
            raise AssertionError(
                f"FakeModelClient exhausted: no scripted response for call "
                f"#{len(self.requests)}. Script one, or assert on the call count."
            )

        text = self._responses.popleft()
        return ModelResponse(
            text=text,
            usage=ModelUsage(
                prompt_tokens=sum(self.count_tokens(m.content) for m in request.messages),
                output_tokens=self.count_tokens(text),
            ),
            truncated=False,
        )

    async def generate_typed(
        self,
        request: ModelRequest,
        contract: type[ContractT],
        cancel: CancellationToken,
    ) -> ContractT:
        response = await self.generate(request, cancel)
        try:
            return contract.model_validate(json.loads(response.text))
        except (json.JSONDecodeError, ValidationError) as exc:
            # Surface the raw text, exactly as the real client must: a failure
            # capsule needs to record what the model actually said.
            raise ModelContractError(
                f"response did not satisfy {contract.__name__}: {exc}",
                raw_text=response.text,
            ) from exc

    @property
    def calls(self) -> int:
        """How many times `generate` completed."""
        return len(self.requests)


class CancelAfter:
    """A `CancellationToken` that flips to cancelled after N observations.

    Lets a test place cancellation at a precise point in a sequence of
    checkpoints, which is how the "is this actually cancellable?" property gets
    tested rather than assumed.
    """

    def __init__(self, checks: int, reason: str = "test cancellation") -> None:
        self._remaining = checks
        self._reason = reason

    @property
    def cancelled(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False

    @property
    def reason(self) -> str | None:
        return self._reason if self._remaining <= 0 else None

    def raise_if_cancelled(self) -> None:
        from shamsu.interfaces.cancellation import Cancelled

        if self.cancelled:
            raise Cancelled(self._reason)

    async def wait_cancelled(self) -> str:
        return self._reason


__all__ = ["CancelAfter", "FakeModelClient"]
