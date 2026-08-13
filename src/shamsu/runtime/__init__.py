"""Run control, the agent state machine, and execution limits.

Owns the loop. Nothing here asks a model what to do next -- it asks the model
to make one narrow decision and then decides the transition itself.

The load-bearing idea is that `RunController` owns a `RunToken`, and that token
is a required parameter on every blocking call in the system. A component
cannot forget to observe cancellation, because it cannot block without being
handed the thing that reports it. v1 had an equivalent control plane that the
live loop simply never imported; making the token a parameter is what stops
that from recurring.

**`AgentSession` is exported lazily, and that is load-bearing.** The layer map
puts the dependency edge runtime -> agent: the runtime invokes bounded
controllers in `agent/`. But `agent.planning` needs `ExecutionLimits`, which
lives here, so there is one back-edge -- and eagerly importing
`runtime.session` from this file closed it into a cycle:

    shamsu.agent.__init__ -> agent.planning -> runtime.limits
        -> runtime.__init__ -> runtime.session -> agent.planning  (partial)

Every `import shamsu.agent.*` in a cold interpreter raised `ImportError:
cannot import name 'Planner' from partially initialized module`. The test
suite hid it because some earlier module always imported `shamsu.runtime`
first, which happens to resolve the chain in a working order -- so the failure
only appeared when a single agent module was imported on its own.

Deferring the one import that closes the cycle fixes it without moving any
code: `from shamsu.runtime import AgentSession` still works, and
`import shamsu.runtime.limits` no longer drags the whole agent package in
behind it.

Milestone 2. See plan sections 10, 11, 28.
"""

from typing import TYPE_CHECKING, Any

from shamsu.runtime.controller import RunAlreadyFinished, RunController, UnknownRun
from shamsu.runtime.events import EventKind, RunEvent
from shamsu.runtime.limits import DEFAULT_LIMITS, ExecutionLimits, LimitExceeded
from shamsu.runtime.tokens import RunToken

if TYPE_CHECKING:
    # Type checkers resolve the cycle statically, so they can see the real
    # names. Only the runtime import needs deferring.
    from shamsu.runtime.session import AgentSession, SessionResult

#: Names served from `runtime.session` on first access.
_DEFERRED = frozenset({"AgentSession", "SessionResult"})

__all__ = [
    "AgentSession",
    "DEFAULT_LIMITS",
    "EventKind",
    "ExecutionLimits",
    "LimitExceeded",
    "RunAlreadyFinished",
    "RunController",
    "RunEvent",
    "RunToken",
    "SessionResult",
    "UnknownRun",
]


def __getattr__(name: str) -> Any:
    """Resolve `AgentSession` and `SessionResult` on first use (PEP 562)."""
    if name in _DEFERRED:
        from shamsu.runtime import session

        return getattr(session, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
