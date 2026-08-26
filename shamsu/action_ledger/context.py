"""Optional ambient handle to the current run's ActionLedger.

ActionLedger is explicitly threaded into the few classes that need it as a
real constructor dependency (SimpleChatLoop, CommandRunner - see their
__init__ signatures). The remaining readers are best-effort logging call
sites scattered across the tool layer, which do not otherwise share a
collaborator; threading a parameter through all of them would touch far more
of the request path than an audit log warrants. For those, a
contextvar-scoped "current run" is used instead: set once per prompt, read
where a log line is being written, cleared when the prompt finishes. Never
used to feed ActionLedger data back into a model.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shamsu.action_ledger.ledger import ActionLedger

_current_run: ContextVar["ActionLedger | None"] = ContextVar("_current_run", default=None)


def get_current_run() -> "ActionLedger | None":
    return _current_run.get()


def set_current_run(ledger: "ActionLedger | None") -> None:
    _current_run.set(ledger)


def clear_current_run() -> None:
    _current_run.set(None)
