"""A deterministic model client, for exercising everything but the model.

Not a mock and not a test fixture — it ships, and `shamsu --model fake` uses
it. Two things it is for:

* **Smoke-testing the interface.** Watching the display work needs a run that
  proceeds, and on a machine with no GPU there is otherwise nothing to watch.
* **A reference for what a real client must satisfy.** Every response here is
  valid against its contract, so a failure with this client is a runtime bug
  rather than a model that emitted something unparseable.

**It does not pretend to be intelligent.** It answers each contract with the
narrowest valid response and concludes as soon as it is allowed to. A run
driven by this client completes only if the runtime lets a task complete
*without doing anything* — which is a property worth being able to check.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.models import (
    ModelContractError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from shamsu.models.normalization import parse_json_response

ContractT = TypeVar("ContractT", bound=BaseModel)

#: Answers by contract name. Each is the narrowest thing that satisfies the
#: shape without claiming anything untrue: the classifier says "planned", the
#: planner proposes one investigate step, and the executor concludes.
_ANSWERS: dict[str, dict[str, Any]] = {
    "TaskClassification": {"kind": "planned", "reason": "scripted client"},
    "InvestigationStep": {
        "action": "conclude",
        "conclusion": "The scripted model performs no work.",
    },
    "ImplementationPlan": {
        "summary": "Scripted plan: inspect only.",
        "steps": [
            {
                "title": "Inspect the workspace",
                "intent": "The scripted model does not change code.",
                "kind": "investigate",
            }
        ],
        "grounded_in": [],
    },
    "ProjectAssessment": {"summary": "Scripted assessment; nothing was inspected."},
    "CompletionClaim": {"claim": "task_complete", "summary": "scripted"},
}


class ScriptedModel:
    """Answers each contract with a fixed, valid response."""

    def __init__(
        self,
        responses: Iterable[str] = (),
        *,
        name: str = "scripted",
        context_tokens: int = 8192,
    ) -> None:
        self._queued: deque[str] = deque(responses)
        self._name = name
        self._context_tokens = context_tokens
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def context_tokens(self) -> int:
        return self._context_tokens

    def count_tokens(self, text: str) -> int:
        """Roughly four characters per token.

        An approximation, and a *stable* one. Real tokenisation belongs to a
        real client; what the compiler needs here is a function whose answer
        never changes between runs.
        """
        return max(1, (len(text) + 3) // 4)

    async def generate(self, request: ModelRequest, cancel: CancellationToken) -> ModelResponse:
        cancel.raise_if_cancelled()
        self.calls += 1

        text = self._queued.popleft() if self._queued else "{}"
        return ModelResponse(
            text=text,
            usage=ModelUsage(
                prompt_tokens=sum(self.count_tokens(m.content) for m in request.messages),
                output_tokens=self.count_tokens(text),
            ),
        )

    async def generate_typed(
        self,
        request: ModelRequest,
        contract: type[ContractT],
        cancel: CancellationToken,
    ) -> ContractT:
        cancel.raise_if_cancelled()

        if self._queued:
            # A queued response is used as given, including a malformed one --
            # that is how the interface's failure paths get exercised.
            response = await self.generate(request, cancel)
            payload, reason = parse_json_response(response.text)
            if payload is None:
                raise ModelContractError(reason, raw_text=response.text)
            return _validate(contract, payload, response.text)

        self.calls += 1
        answer = _ANSWERS.get(contract.__name__)
        if answer is None:
            raise ModelContractError(
                f"the scripted client has no answer for {contract.__name__}",
                raw_text="",
            )
        return _validate(contract, answer, json.dumps(answer))


def _validate(contract: type[ContractT], payload: dict[str, Any], raw: str) -> ContractT:
    try:
        return contract.model_validate(payload)
    except ValidationError as exc:
        raise ModelContractError(
            f"response did not satisfy {contract.__name__}: {exc}", raw_text=raw
        ) from exc


__all__ = ["ScriptedModel"]
