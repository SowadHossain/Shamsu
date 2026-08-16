"""Gateway from Telegram UI events to SHAMSU's existing sessions and runtime."""
from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shamsu.action_ledger import store as action_store
from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.agents.chat_loop import AgentChatLoop, AgentLoopResult
from shamsu.cli.request_lifecycle import finish_current_run
from shamsu.integrations.telegram.approvals import TelegramApprovalBroker
from shamsu.integrations.telegram.models import (
    TelegramInboundMetadata,
    TelegramRuntimeStatus,
    TelegramSessionSummary,
)
from shamsu.runtime.run_control import (
    active_runs_for_session,
    add_feedback,
    cancel_run,
    pause_run,
    resume_run,
)
from shamsu.runtime.task_state import PlanStepStatus, RuntimeStateStore
from shamsu.session.manager import SessionManager
from shamsu.tools.agent_tools import AgentToolRegistry


@dataclass(frozen=True)
class RoutedMessageResult:
    text: str
    run_id: str = ""
    status: str = ""


class SessionGateway(Protocol):
    def list_sessions(self) -> list[TelegramSessionSummary]:
        ...

    def ensure_default_session(self) -> str:
        ...

    def switch_session(self, session_id: str) -> TelegramSessionSummary:
        ...

    def create_session(self, title: str | None = None) -> TelegramSessionSummary:
        ...

    def status(self, session_id: str) -> TelegramRuntimeStatus:
        ...

    def plan(self, session_id: str) -> tuple[str, list[tuple[str, str]]]:
        ...

    def changes(self, session_id: str) -> str:
        ...

    def tests(self, session_id: str) -> str:
        ...

    def logs(self, session_id: str) -> list[str]:
        ...

    async def route_user_message(
        self,
        text: str,
        *,
        metadata: TelegramInboundMetadata,
    ) -> RoutedMessageResult:
        ...

    def cancel_current_run(self, session_id: str) -> bool:
        ...

    def pause_current_run(self, session_id: str) -> bool:
        ...

    def resume_current_run(self, session_id: str) -> bool:
        ...


class LocalShamsuSessionGateway:
    def __init__(
        self,
        workspace: Path,
        *,
        approval_broker: TelegramApprovalBroker | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.session_manager = SessionManager(self.workspace)
        self.runtime_store = RuntimeStateStore(self.workspace)
        self.approval_broker = approval_broker

    def list_sessions(self) -> list[TelegramSessionSummary]:
        return [self._summary_for_metadata(item) for item in self.session_manager.list_sessions()]

    def ensure_default_session(self) -> str:
        logger = self.session_manager.get_or_create_latest()
        return logger.session_id

    def switch_session(self, session_id: str) -> TelegramSessionSummary:
        metadata = self.session_manager.resolve(session_id)
        return self._summary_for_metadata(metadata)

    def create_session(self, title: str | None = None) -> TelegramSessionSummary:
        logger = self.session_manager.create_session(title)
        return self._summary_for_metadata(logger.metadata)

    def status(self, session_id: str) -> TelegramRuntimeStatus:
        session = self.switch_session(session_id)
        active = active_runs_for_session(session_id)
        if active:
            run = active[-1]
            task = self.runtime_store.load_task(f"task-{run.run_id}")
            plan = self.runtime_store.load_task_plan(task.task_id) if task else None
            active_step = (
                next((step for step in plan.steps if step.status == PlanStepStatus.ACTIVE), None)
                if plan
                else None
            )
            completed = len([step for step in plan.steps if step.status == PlanStepStatus.COMPLETED]) if plan else 0
            total = len(plan.steps) if plan else 0
            return TelegramRuntimeStatus(
                session=session,
                status=run.status.value,
                task=task.user_request if task else run.last_message,
                phase=task.current_phase if task else "",
                plan_completed=completed,
                plan_total=total,
                current_step=active_step.title if active_step else "",
                actions=task.action_count if task else run.iterations,
                last_action=str((task.last_tool_call or {}).get("name") or "") if task else "",
                updated_at=run.updated_at,
                run_id=run.run_id,
            )
        run_id = action_store.resolve_run_id(self.workspace, "last") or ""
        manifest = action_store.load_manifest(self.workspace, run_id) if run_id else None
        return TelegramRuntimeStatus(
            session=session,
            status=str((manifest or {}).get("status") or session.status or "idle"),
            task=str((manifest or {}).get("prompt_preview") or session.current_task),
            updated_at=str((manifest or {}).get("finished_at") or session.last_activity),
            run_id=run_id,
        )

    def plan(self, session_id: str) -> tuple[str, list[tuple[str, str]]]:
        status = self.status(session_id)
        if not status.run_id:
            return "", []
        task = self.runtime_store.load_task(f"task-{status.run_id}")
        plan = self.runtime_store.load_task_plan(task.task_id) if task else None
        if plan is None:
            return status.task, []
        return plan.title, [(step.status.value, step.title) for step in plan.steps]

    def changes(self, session_id: str) -> str:
        run_id = self.status(session_id).run_id
        if not run_id:
            return ""
        mutations = action_store.load_mutations(self.workspace, run_id)
        paths = []
        for mutation in mutations:
            paths.extend(str(path) for path in mutation.get("touched_files", []) if path)
        unique = sorted(dict.fromkeys(paths))
        if not unique:
            return "No recorded file changes for the active or latest run."
        stat = self._git_stat()
        return "\n".join([f"{len(unique)} files changed", "", *[f"- {path}" for path in unique[:30]], "", stat]).strip()

    def tests(self, session_id: str) -> str:
        run_id = self.status(session_id).run_id
        if not run_id:
            return ""
        events = action_store.load_events(self.workspace, run_id)
        testish = [
            event
            for event in events
            if "test" in str(event.get("type", "")).lower()
            or "pytest" in str(event.get("command", "")).lower()
        ]
        if not testish:
            return "No test evidence recorded for the active or latest run."
        return "\n".join(str(event.get("summary") or event.get("type") or "") for event in testish[-8:])

    def logs(self, session_id: str) -> list[str]:
        logger = self.session_manager.logger_for(session_id)
        return [
            f"{event.get('timestamp', '')[-14:-6]} {event.get('summary') or event.get('event_type')}"
            for event in logger.tail(30)
            if event.get("summary") or event.get("event_type")
        ]

    async def route_user_message(
        self,
        text: str,
        *,
        metadata: TelegramInboundMetadata,
    ) -> RoutedMessageResult:
        active = active_runs_for_session(metadata.session_id)
        if active:
            run = active[-1]
            accepted = add_feedback(run.run_id, text)
            return RoutedMessageResult(
                "Added your feedback to the active SHAMSU run." if accepted else "The active run could not accept feedback.",
                run_id=run.run_id,
                status=run.status.value,
            )
        return await asyncio.to_thread(self._run_agent_sync, text, metadata)

    def cancel_current_run(self, session_id: str) -> bool:
        active = active_runs_for_session(session_id)
        return bool(active and cancel_run(active[-1].run_id))

    def pause_current_run(self, session_id: str) -> bool:
        active = active_runs_for_session(session_id)
        return bool(active and pause_run(active[-1].run_id))

    def resume_current_run(self, session_id: str) -> bool:
        active = active_runs_for_session(session_id)
        return bool(active and resume_run(active[-1].run_id))

    def _run_agent_sync(self, text: str, metadata: TelegramInboundMetadata) -> RoutedMessageResult:
        logger = self.session_manager.resume_session(metadata.session_id)
        logger.log(
            "user.prompt",
            {"prompt": text},
            "Telegram user submitted prompt",
            workflow_id="telegram",
        )
        logger.log(
            "telegram.user_message",
            {
                "telegram_user_id": metadata.telegram_user_id,
                "telegram_chat_id": metadata.telegram_chat_id,
                "telegram_message_id": metadata.telegram_message_id,
            },
            "Telegram message routed into SHAMSU session",
            workflow_id="telegram",
        )
        ledger = start_run(self.workspace, text, session_logger=logger)
        set_current_run(ledger)
        approval_func = None
        if self.approval_broker is not None:
            approval_func = self.approval_broker.approval_func(
                telegram_user_id=metadata.telegram_user_id,
                telegram_chat_id=metadata.telegram_chat_id,
                session_id=metadata.session_id,
                run_id=ledger.run_id,
            )
        try:
            tools = AgentToolRegistry(
                self.workspace,
                approval_func=approval_func if approval_func is not None else (lambda _request: False),
                session_logger=logger,
                action_ledger=ledger,
            )
            loop = AgentChatLoop(
                self.workspace,
                session_logger=logger,
                tools=tools,
                action_ledger=ledger,
                run_id=ledger.run_id,
                original_user_request=text,
            )
            result: AgentLoopResult = asyncio.run(loop.run(text))
            ledger.record_final_response(result.final)
            finish_current_run(self.workspace, ledger)
            summary = action_store.load_summary(self.workspace, ledger.run_id) or {}
            manifest = action_store.load_manifest(self.workspace, ledger.run_id) or {}
            return RoutedMessageResult(
                result.final,
                run_id=result.run_id or ledger.run_id,
                status=str(summary.get("status") or manifest.get("status") or result.status.value),
            )
        except Exception as exc:
            ledger.fail(str(exc))
            raise
        finally:
            clear_current_run()

    def _summary_for_metadata(self, metadata) -> TelegramSessionSummary:
        active = active_runs_for_session(metadata.session_id)
        run_id = active[-1].run_id if active else ""
        status = active[-1].status.value if active else ("idle" if metadata.status == "active" else metadata.status)
        return TelegramSessionSummary(
            session_id=metadata.session_id,
            display_name=metadata.title,
            workspace=metadata.workspace,
            status=status,
            current_task=metadata.last_user_prompt,
            current_run=run_id,
            active_branch=self._branch(),
            last_activity=metadata.updated_at,
        )

    def _branch(self) -> str:
        try:
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception:
            return ""
        return result.stdout.strip()

    def _git_stat(self) -> str:
        try:
            result = subprocess.run(
                ["git", "diff", "--shortstat"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception:
            return ""
        return result.stdout.strip()
