"""Run-control adapter used by the production runtime engine."""
from __future__ import annotations

import contextlib
from collections.abc import Iterator

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.runtime.run_control import ControlledRun, bind_run, complete_run, register_run
from shamsu.session.manager import SessionLogger
from shamsu.types import RunStatus


class RunController:
    def __init__(
        self,
        *,
        run_id: str,
        session_logger: SessionLogger | None = None,
        action_ledger: ActionLedger | None = None,
        max_runtime_seconds: float | None = None,
    ) -> None:
        self.run_id = run_id
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self.max_runtime_seconds = max_runtime_seconds

    def start(self) -> ControlledRun:
        return register_run(
            self.run_id,
            session_logger=self.session_logger,
            action_ledger=self.action_ledger,
            max_runtime_seconds=self.max_runtime_seconds,
        )

    @contextlib.contextmanager
    def bound(self, control: ControlledRun) -> Iterator[ControlledRun]:
        with bind_run(control):
            yield control

    def complete(self, status: RunStatus, message: str) -> None:
        complete_run(self.run_id, status, message)
