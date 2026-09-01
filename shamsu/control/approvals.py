"""One approval, answerable from any surface.

The tool layer calls a synchronous `approval_func(request) -> bool` and blocks.
That contract is kept exactly; what changes is where the answer comes from.
Before, each surface owned its own: the REPL blocked on a console read, and
Telegram held a `threading.Event` in a dict. Both were invisible outside their
own process, so a run started in a browser could only be approved at a terminal
nobody was sitting at.

Now the question goes into the control store and the first answer from anywhere
wins. The agent does not know or care which surface answered.

**It fails closed.** No answer, an expired question, a vanished record, a
shutdown mid-wait - every one of those is a denial. The only path to `True` is
somebody actively saying yes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from shamsu.control.store import ALLOW, ControlStore, approval_timeout
from shamsu.safety.commands import redact


class SharedApprovalBroker:
    """Turns a blocking `approval_func` into a question anyone can answer."""

    def __init__(
        self,
        store: ControlStore | None = None,
        *,
        timeout_seconds: float | None = None,
        on_raised: Callable[[str], None] | None = None,
        on_resolved: Callable[[str, str], None] | None = None,
    ) -> None:
        self.store = store or ControlStore()
        # Resolved now rather than at import, so a timeout set from inside
        # SHAMSU reaches a broker built by a process already running.
        self.timeout_seconds = float(
            timeout_seconds if timeout_seconds is not None else approval_timeout()
        )
        # Hooks so a surface can push the card out immediately rather than
        # waiting for its next poll - and, just as importantly, retract it when
        # somebody else answers.
        self.on_raised = on_raised
        self.on_resolved = on_resolved

    def approval_func(
        self,
        *,
        workspace: Path | str,
        session_id: str,
        run_id: str = "",
        should_stop: Callable[[], bool] = lambda: False,
    ) -> Callable[[Any], bool]:
        def approve(request: Any) -> bool:
            approval_id = self.store.raise_approval(
                workspace=workspace,
                session_id=session_id,
                run_id=run_id,
                action_type=str(getattr(request, "action_type", "") or ""),
                # Redacted going in, not on the way out to each surface: the
                # description is written once and read by three of them, and a
                # rule applied at three read sites is a rule that will be
                # forgotten at one of them.
                description=redact(str(getattr(request, "description", "") or "")),
                risk_level=str(getattr(request, "risk_level", "") or ""),
                preview=redact(str(getattr(request, "preview", "") or ""))[:2000],
                timeout_seconds=self.timeout_seconds,
            )
            if self.on_raised is not None:
                try:
                    self.on_raised(approval_id)
                except Exception:  # noqa: BLE001 - a notification is not the decision
                    pass
            decision = self.store.wait_for_decision(
                approval_id,
                timeout_seconds=self.timeout_seconds,
                should_stop=should_stop,
            )
            if self.on_resolved is not None:
                try:
                    self.on_resolved(approval_id, decision)
                except Exception:  # noqa: BLE001
                    pass
            return decision == ALLOW

        return approve

    # -- what a surface calls when someone taps a button ------------------

    def resolve(self, approval_id: str, approved: bool, surface: str) -> bool:
        """Answer. False means somebody else already did.

        Returning False rather than raising is deliberate: two people answering
        at once is normal, not exceptional, and the second one should be told
        "already handled" rather than shown an error.
        """
        from shamsu.control.store import DENY

        decided = self.store.resolve_approval(
            approval_id, ALLOW if approved else DENY, surface
        )
        if decided and self.on_resolved is not None:
            try:
                self.on_resolved(approval_id, ALLOW if approved else DENY)
            except Exception:  # noqa: BLE001
                pass
        return decided

    def pending(self, workspace: Path | str | None = None, session_id: str = ""):
        return self.store.pending_approvals(workspace, session_id)
