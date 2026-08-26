"""Mobile-sized Telegram rendering helpers."""
from __future__ import annotations

from dataclasses import dataclass

from shamsu.integrations.telegram.models import (
    PendingApproval,
    TelegramRuntimeStatus,
    TelegramSessionSummary,
)
from shamsu.safety.commands import redact

MAX_MESSAGE_CHARS = 3900


@dataclass(frozen=True)
class Page:
    text: str
    index: int
    total: int


class TelegramFormatter:
    def home(self, status: TelegramRuntimeStatus, *, connected: bool) -> str:
        session_name = status.session.display_name if status.session else "No active session"
        project = status.session.workspace if status.session else "-"
        state = "Connected" if connected else "Waiting for pairing"
        return "\n".join(
            [
                "SHAMSU Remote",
                "",
                f"Status: {state}",
                "",
                f"Active session: {session_name}",
                f"Project: {project}",
                f"Current status: {status.status}",
                "",
                "What would you like SHAMSU to do?",
            ]
        )

    def help(self) -> str:
        return "\n".join(
            [
                "SHAMSU Remote",
                "",
                "WORK",
                "Send a normal message to give SHAMSU a task.",
                "",
                "SESSIONS",
                "/sessions - View and switch sessions",
                "/new - Create a session",
                "",
                "WORK STATUS",
                "/status - Current task",
                "/plan - Current plan",
                "/changes - Changed files",
                "/tests - Latest tests",
                "/logs - Recent activity",
                "",
                "CONTROL",
                "/pause",
                "/resume",
                "/cancel",
                "",
                "SETTINGS",
                "/settings",
            ]
        )

    def status(self, status: TelegramRuntimeStatus) -> str:
        session = status.session.display_name if status.session else "No active session"
        task = status.task or "Idle"
        return "\n".join(
            [
                f"SHAMSU - {status.status.title()}",
                "",
                f"Session: {session}",
                f"Task: {task}",
                f"Actions: {status.actions} / {status.max_actions}",
                f"Updated: {status.updated_at or '-'}",
            ]
        )

    def sessions(
        self,
        sessions: list[TelegramSessionSummary],
        *,
        active_session_id: str = "",
    ) -> str:
        if not sessions:
            return "Your SHAMSU Sessions\n\nNo sessions yet."
        lines = ["Your SHAMSU Sessions", ""]
        for item in sessions[:20]:
            marker = "*" if item.session_id == active_session_id else "-"
            state = item.status.title() if item.status else "Idle"
            lines.extend(
                [
                    f"{marker} {item.display_name}",
                    f"  {item.workspace}",
                    f"  {state}" + (f": {item.current_task}" if item.current_task else ""),
                    "",
                ]
            )
        return "\n".join(lines).rstrip()

    def plan(self, title: str, steps: list[tuple[str, str]]) -> str:
        lines = [f"Plan - {title or 'Current Task'}", ""]
        if not steps:
            lines.append("No active plan.")
            return "\n".join(lines)
        for index, (status, label) in enumerate(steps, 1):
            marker = {
                "completed": "done",
                "active": "now",
                "verifying": "check",
                "blocked": "blocked",
                "failed": "failed",
                "cancelled": "cancelled",
            }.get(status, "todo")
            lines.append(f"{marker} {index}. {label}")
        return "\n".join(lines)

    def changes(self, summary: str) -> str:
        return "Changes\n\n" + (summary.strip() or "No changed files recorded.")

    def tests(self, summary: str) -> str:
        return "Tests\n\n" + (summary.strip() or "No test evidence recorded.")

    def logs(self, lines: list[str]) -> str:
        if not lines:
            return "Recent Activity\n\nNo recent activity."
        return "Recent Activity\n\n" + "\n".join(lines[-12:])

    def approval(self, approval: PendingApproval) -> str:
        parts = [
            "Approval Required",
            "",
            "SHAMSU wants to:",
            redact(approval.description),
            "",
            f"Risk: {approval.risk_level}",
        ]
        if approval.preview:
            parts.extend(["", "Preview:", redact(approval.preview)[:1000]])
        if approval.working_dir:
            parts.extend(["", f"Project: {approval.working_dir}"])
        return "\n".join(parts)

    def settings(self) -> str:
        return "\n".join(
            [
                "Telegram Settings",
                "",
                "Task progress: Important only",
                "Task completion: On",
                "Approval requests: On",
                "Failures: On",
                "Tool-by-tool updates: Off",
            ]
        )

    def paginate(self, text: str, *, max_chars: int = MAX_MESSAGE_CHARS) -> list[Page]:
        clean = redact(text or "")
        if len(clean) <= max_chars:
            return [Page(clean, 1, 1)]
        chunks: list[str] = []
        remaining = clean
        while remaining:
            chunk = remaining[:max_chars]
            split_at = max(chunk.rfind("\n"), chunk.rfind(" "))
            if split_at > max_chars // 2:
                chunk = chunk[:split_at]
            chunks.append(chunk.rstrip())
            remaining = remaining[len(chunk):].lstrip()
        total = len(chunks)
        return [Page(f"Page {index} / {total}\n\n{chunk}", index, total) for index, chunk in enumerate(chunks, 1)]

