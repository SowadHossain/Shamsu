"""Run control, the agent state machine, and execution limits.

Owns the loop. Nothing here asks a model what to do next -- it asks the model
to make one narrow decision and then decides the transition itself.

The load-bearing idea is that `RunController` owns a `RunToken`, and that token
is a required parameter on every blocking call in the system. A component
cannot forget to observe cancellation, because it cannot block without being
handed the thing that reports it. v1 had an equivalent control plane that the
live loop simply never imported; making the token a parameter is what stops
that from recurring.

Milestone 2. See plan sections 10, 11, 28.
"""

from shamsu.runtime.controller import RunAlreadyFinished, RunController, UnknownRun
from shamsu.runtime.events import EventKind, RunEvent
from shamsu.runtime.limits import DEFAULT_LIMITS, ExecutionLimits, LimitExceeded
from shamsu.runtime.tokens import RunToken

__all__ = [
    "DEFAULT_LIMITS",
    "EventKind",
    "ExecutionLimits",
    "LimitExceeded",
    "RunAlreadyFinished",
    "RunController",
    "RunEvent",
    "RunToken",
    "UnknownRun",
]
