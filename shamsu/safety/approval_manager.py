"""Central approval wrapper with consistent logging and optional memory."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict

from shamsu.action_ledger.context import get_current_run
from shamsu.action_ledger.ledger import ActionLedger
from shamsu.safety.approval import ask_approval
from shamsu.safety.commands import is_auto_approvable_action
from shamsu.safety.permission_store import PermissionMemory
from shamsu.session.manager import SessionLogger
from shamsu.types import ApprovalRequest


class ApprovalManager:
    def __init__(
        self,
        approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
        session_logger: SessionLogger | None = None,
        memory: PermissionMemory | None = None,
        remember_prompt: Callable[[str], str] | None = None,
        menu_prompt: Callable[[ApprovalRequest, bool], tuple[bool, str]] | None = None,
        action_ledger: ActionLedger | None = None,
    ) -> None:
        self.approval_func = approval_func
        self.session_logger = session_logger
        # memory/remember_prompt default to None, which keeps ask() behaving
        # exactly as before for any caller that doesn't opt into the feature.
        self.memory = memory
        self.remember_prompt = remember_prompt
        # When set (interactive REPL), a single Claude-Code-style numbered menu
        # handles both the yes/no choice AND the "don't ask again" option, so
        # the user answers one prompt instead of two. When None, the legacy
        # approval_func (+ optional separate remember_prompt) path is used, so
        # existing callers/tests are unaffected.
        self.menu_prompt = menu_prompt
        self.action_ledger = action_ledger

    def ask(self, request: ApprovalRequest) -> bool:
        auto_approvable = is_auto_approvable_action(request.action_type)
        if auto_approvable and self.memory and self.memory.is_remembered(request.action_type):
            remembered_scope = self.memory.list_remembered().get(request.action_type, "session")
            self._log(
                "approval.auto_approved",
                {
                    "request": asdict(request),
                    "action_type": request.action_type,
                    "approved": True,
                    "decision_scope": remembered_scope,
                    "decision_source": "remembered_policy",
                },
                f"Auto-approved (remembered): {request.action_type}",
            )
            return True

        self._log(
            "approval.request",
            {"request": asdict(request)},
            f"Approval requested: {request.action_type}",
        )

        if self.menu_prompt is not None:
            offer_remember = bool(auto_approvable and self.memory)
            approved, scope = self.menu_prompt(request, offer_remember)
            self._log(
                "approval.result",
                {
                    "request": asdict(request),
                    "action_type": request.action_type,
                    "approved": approved,
                    "decision_scope": scope if approved else "none",
                    "decision_source": "interactive_menu",
                },
                f"Approval {'granted' if approved else 'denied'}: {request.action_type}",
            )
            if approved and offer_remember and scope in {"session", "workspace"}:
                self.memory.remember(request.action_type, scope)
                self._log(
                    "approval.remembered",
                    {"action_type": request.action_type, "scope": scope},
                    f"Will auto-approve future '{request.action_type}' actions ({scope})",
                )
            return approved

        approved = self.approval_func(request)
        self._log(
            "approval.result",
            {
                "request": asdict(request),
                "action_type": request.action_type,
                "approved": approved,
                "decision_scope": "once" if approved else "none",
                "decision_source": "approval_callback",
            },
            f"Approval {'granted' if approved else 'denied'}: {request.action_type}",
        )

        if approved and auto_approvable and self.memory and self.remember_prompt:
            scope = self.remember_prompt(request.action_type)
            if scope in {"session", "workspace"}:
                self.memory.remember(request.action_type, scope)
                self._log(
                    "approval.remembered",
                    {"action_type": request.action_type, "scope": scope},
                    f"Will auto-approve future '{request.action_type}' actions ({scope})",
                )
        return approved

    def _log(self, event_type: str, payload: dict, summary: str) -> None:
        if self.session_logger:
            self.session_logger.log(event_type, payload, summary, workflow_id="approval")
        ledger = self.action_ledger or get_current_run()
        if ledger is not None:
            ledger_event = event_type.replace(".", "_")
            if event_type == "approval.result":
                ledger_event = "approval_granted" if payload.get("approved") else "approval_denied"
            ledger.log_event(ledger_event, **payload)
