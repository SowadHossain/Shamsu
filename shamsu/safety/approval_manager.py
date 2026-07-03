"""Central approval wrapper with consistent logging."""
from __future__ import annotations

from collections.abc import Callable

from shamsu.safety.approval import ask_approval
from shamsu.session.manager import SessionLogger
from shamsu.types import ApprovalRequest


class ApprovalManager:
    def __init__(
        self,
        approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
        session_logger: SessionLogger | None = None,
    ) -> None:
        self.approval_func = approval_func
        self.session_logger = session_logger

    def ask(self, request: ApprovalRequest) -> bool:
        self._log(
            "approval.request",
            {"request": request},
            f"Approval requested: {request.action_type}",
        )
        approved = self.approval_func(request)
        self._log(
            "approval.result",
            {"action_type": request.action_type, "approved": approved},
            f"Approval {'granted' if approved else 'denied'}: {request.action_type}",
        )
        return approved

    def _log(self, event_type: str, payload: dict, summary: str) -> None:
        if self.session_logger:
            self.session_logger.log(event_type, payload, summary, workflow_id="approval")
