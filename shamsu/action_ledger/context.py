"""Optional ambient handle to the current run's ActionLedger.

ActionLedger is explicitly threaded into the few classes that need it as a
real constructor dependency (AgentChatLoop, CommandRunner, PatchEngine - see
their __init__ signatures). But SHAMSU's CLI dispatcher
(shamsu/cli/repl.py::_handle_request) fans a single user prompt out across
more than a dozen workflow functions that don't otherwise share a
collaborator, and threading a new parameter through all of them would touch
far more of the request path than an audit log warrants. For those call
sites, a contextvar-scoped "current run" is used instead: set once per
prompt, read by a handful of best-effort logging call sites, cleared when
the prompt finishes. Never used to feed ActionLedger data back into a model.
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
