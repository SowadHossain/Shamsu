"""Request-scoped approval injection for noninteractive callers."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shamsu.types import ApprovalRequest


ApprovalFunc = Callable[["ApprovalRequest"], bool]

_approval_override: ContextVar[ApprovalFunc | None] = ContextVar(
    "_approval_override", default=None
)


def get_approval_override() -> ApprovalFunc | None:
    return _approval_override.get()


@contextmanager
def approval_override(approval_func: ApprovalFunc) -> Iterator[None]:
    """Use ``approval_func`` for default approval prompts in this context."""
    token = _approval_override.set(approval_func)
    try:
        yield
    finally:
        _approval_override.reset(token)
