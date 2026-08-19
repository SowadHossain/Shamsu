"""Gateway from Telegram UI events to SHAMSU's existing sessions and runtime."""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shamsu.action_ledger import store as action_store
from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.agents.chat_loop import AgentChatLoop, AgentLoopResult
from shamsu.agents.simple_chat import simple_mode_enabled
from shamsu.cli.request_lifecycle import finish_current_run
from shamsu.integrations.telegram.approvals import TelegramApprovalBroker
from shamsu.integrations.telegram.models import (
    OutboundMessage,
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
from shamsu.ui.progress import ProgressReporter


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
        notify = getattr(self.approval_broker, "notify", None)
        progress = TelegramProgressReporter(
            notify=notify,
            telegram_chat_id=metadata.telegram_chat_id,
            session_logger=logger,
        )
        progress.start_task("SHAMSU remote task")
        try:
            tools = AgentToolRegistry(
                self.workspace,
                approval_func=approval_func if approval_func is not None else (lambda _request: False),
                session_logger=logger,
                action_ledger=ledger,
            )
            if simple_mode_enabled():
                # Telegram resumes the LATEST session in the workspace, which is
                # normally the one the desktop REPL is in. Running the legacy
                # loop here therefore wrote ITS tool vocabulary -
                # `project.inspect`, `file.read`, `code.search`, and a `test.run`
                # denied by an AUTHOR phase contract - straight into a
                # simple-mode transcript, where the model then imitated calls it
                # could not make. Observed live 2026-08-18; the desktop side
                # showed no turn for it, because Telegram writes no chat log.
                final = self._run_simple(text, logger, tools, ledger, progress)
            else:
                loop = AgentChatLoop(
                    self.workspace,
                    session_logger=logger,
                    tools=tools,
                    action_ledger=ledger,
                    run_id=ledger.run_id,
                    original_user_request=text,
                    progress=progress,
                    max_runtime_seconds=_telegram_task_timeout_seconds(self.approval_broker),
                )
                result: AgentLoopResult = asyncio.run(loop.run(text))
                final = result.final
            ledger.record_final_response(final)
            finish_current_run(self.workspace, ledger)
            progress.done("SHAMSU finished the task.")
            summary = action_store.load_summary(self.workspace, ledger.run_id) or {}
            manifest = action_store.load_manifest(self.workspace, ledger.run_id) or {}
            return RoutedMessageResult(
                final,
                run_id=ledger.run_id,
                status=str(summary.get("status") or manifest.get("status") or "completed"),
            )
        except Exception as exc:
            progress.failed("SHAMSU stopped with an error.")
            ledger.fail(str(exc))
            raise
        finally:
            clear_current_run()

    def _run_simple(self, text, logger, tools, ledger, progress) -> str:
        """The same loop the desktop uses, on the same session, same seven tools."""
        from shamsu.agents.chat_loop import _default_ollama_client
        from shamsu.agents.simple_chat import SimpleChatLoop
        from shamsu.llm.manager import OLLAMA_BASE_URL
        from shamsu.runtime.timeouts import TimeoutConfig

        loop = SimpleChatLoop(
            self.workspace,
            client=_default_ollama_client(OLLAMA_BASE_URL, TimeoutConfig()),
            tools=tools,
            session_logger=logger,
            action_ledger=ledger,
            on_activity=lambda message: progress.step(str(message)),
        )
        result = asyncio.run(loop.run(text))
        return result.final

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


class TelegramProgressReporter(ProgressReporter):
    def __init__(
        self,
        *,
        notify,
        telegram_chat_id: int,
        session_logger,
        min_interval_seconds: float = 8.0,
    ) -> None:
        super().__init__(session_logger=session_logger, title="SHAMSU")
        self.notify = notify
        self.telegram_chat_id = telegram_chat_id
        self.min_interval_seconds = min_interval_seconds
        self._last_sent_at = 0.0
        self._last_sent_message = ""

    def _emit(self, kind: str, message: str, payload: dict) -> None:
        super()._emit(kind, message, payload)
        if self.notify is None or not self._should_notify(kind, message, payload):
            return
        self._last_sent_at = time.monotonic()
        self._last_sent_message = message
        self.notify(OutboundMessage(self.telegram_chat_id, _format_progress_message(kind, message, payload)))

    def _should_notify(self, kind: str, message: str, payload: dict) -> bool:
        if kind in {"progress.done", "progress.failed", "progress.warning", "progress.command_start"}:
            return True
        if kind == "progress.tool_start":
            return True
        if kind == "progress.tool_result":
            return payload.get("ok") is False
        now = time.monotonic()
        if kind == "progress.step" and message != self._last_sent_message:
            return now - self._last_sent_at >= self.min_interval_seconds
        return False


def _format_progress_message(kind: str, message: str, payload: dict) -> str:
    if kind == "progress.tool_start":
        return f"Working: {message}"
    if kind == "progress.tool_result":
        return f"Tool needs attention: {message}"
    if kind == "progress.command_start":
        return f"Running: {message}"
    if kind == "progress.done":
        return f"Done: {message}"
    if kind == "progress.failed":
        return f"Failed: {message}"
    return f"Working: {message}"


def _telegram_task_timeout_seconds(approval_broker: TelegramApprovalBroker | None) -> float:
    raw = os.environ.get("SHAMSU_TELEGRAM_TASK_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    approval_timeout = float(getattr(approval_broker, "decision_timeout_seconds", 900.0) or 900.0)
    return max(1800.0, approval_timeout + 300.0)
