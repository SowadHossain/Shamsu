"""Base class for typed tools.

A tool is a `ToolContract` plus a typed input model plus an implementation.
Deriving the model-facing JSON schema from the input model means the schema the
model is shown and the validation the gateway performs are the same artifact --
they cannot drift apart, which is the failure mode that produces "the model
keeps calling this wrong" bug reports.

Subclasses implement `run`, which receives a *validated* input object. They
never see raw arguments, so an implementation cannot skip validation by
accident.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.tools import ToolContract, ToolPolicyViolation, ToolResult

InputT = TypeVar("InputT", bound=BaseModel)


class Tool(ABC, Generic[InputT]):
    """One executable capability."""

    #: Declared once, next to the implementation, so the policy and the code it
    #: governs cannot be edited independently.
    contract: ToolContract

    #: Pydantic model describing this tool's arguments.
    input_model: type[InputT]

    @property
    def name(self) -> str:
        return self.contract.name

    def input_schema(self) -> Mapping[str, Any]:
        """JSON Schema for the arguments, as shown to the model."""
        return self.input_model.model_json_schema()

    def parse(self, arguments: Mapping[str, Any]) -> InputT:
        """Validate raw arguments.

        Raises:
            ToolPolicyViolation: the arguments do not satisfy the schema. Raised
                rather than returned because a malformed call must not reach
                `run` at all -- refusal happens before any side effect.
        """
        try:
            return self.input_model.model_validate(dict(arguments))
        except ValidationError as exc:
            raise ToolPolicyViolation(
                f"{self.name}: invalid arguments — {_summarise(exc)}"
            ) from exc

    @abstractmethod
    async def run(self, arguments: InputT, cancel: CancellationToken) -> ToolResult:
        """Execute with validated arguments.

        Implementations should return `ToolResult(ok=False, error=...)` for an
        expected failure -- a file that is not there, a command that exited
        non-zero. Raise only for genuinely unexpected conditions; the gateway
        converts those into an honest failed result rather than letting them
        escape into the runtime.
        """

    def write_targets(self, arguments: InputT) -> tuple[str, ...]:
        """Workspace paths this call would modify.

        Empty by default, which is correct for every read-only tool. A
        *mutating* tool that writes files must override this, or a `WriteScope`
        cannot constrain it — `tests/unit/test_tool_gateway.py` asserts that
        every mutating tool has made that choice deliberately.
        """
        return ()

    # -- helpers for implementations --------------------------------------

    def ok(
        self,
        output: str,
        *,
        started: float | None = None,
        **extra: Any,
    ) -> ToolResult:
        """A successful result, with this tool's declared evidence attached."""
        return ToolResult(
            tool=self.name,
            ok=True,
            output=output,
            duration_seconds=_elapsed(started),
            evidence=self.contract.produces_evidence,
            invalidated=self.contract.invalidates,
            **extra,
        )

    def failed(self, error: str, *, started: float | None = None) -> ToolResult:
        """An honest failure.

        No evidence and no invalidation: a tool that did not do its job did not
        produce proof of doing it. This is what keeps `ok=False` from silently
        advancing the completion gate.
        """
        return ToolResult(
            tool=self.name,
            ok=False,
            error=error,
            duration_seconds=_elapsed(started),
        )


def _elapsed(started: float | None) -> float:
    return 0.0 if started is None else max(0.0, time.monotonic() - started)


def _summarise(exc: ValidationError, limit: int = 3) -> str:
    """Render validation errors compactly.

    The model sees this text and has to act on it, so it names the fields and
    what was wrong with them rather than reproducing pydantic's full report.
    """
    parts: list[str] = []
    for error in exc.errors()[:limit]:
        location = ".".join(str(item) for item in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['msg']}")
    remaining = len(exc.errors()) - limit
    if remaining > 0:
        parts.append(f"(+{remaining} more)")
    return "; ".join(parts)


__all__ = ["Tool"]
