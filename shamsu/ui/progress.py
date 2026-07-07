"""Safe visible progress reporting for CLI workflows."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from shamsu.safety.commands import redact
from shamsu.session.manager import SessionLogger


@dataclass
class ProgressEvent:
    kind: str
    message: str
    payload: dict[str, Any]


class ProgressReporter:
    def __init__(
        self,
        console: Console | None = None,
        session_logger: SessionLogger | None = None,
        title: str = "Agent",
        max_steps: int | None = None,
        verbose: bool = False,
    ) -> None:
        self.console = console
        self.session_logger = session_logger
        self.title = title
        self.max_steps = max_steps
        self.verbose = verbose
        self.step_index = 0
        self.started = time.monotonic()
        self.last_message = ""

    def start_task(self, title: str | None = None, max_steps: int | None = None) -> None:
        if title:
            self.title = title
        if max_steps is not None:
            self.max_steps = max_steps
        self.step(f"{self.title} started")

    def step(self, message: str, **payload: Any) -> None:
        self._emit("progress.step", message, payload)

    def tool_start(self, tool_name: str, args_summary: str = "") -> None:
        message = f"Using tool: {tool_name}"
        if args_summary:
            message = f"{message} ({args_summary})"
        self._emit("progress.tool_start", message, {"tool_name": tool_name, "args": args_summary})

    def tool_result(self, tool_name: str, result_summary: str, ok: bool = True) -> None:
        status = "ok" if ok else "failed"
        self._emit(
            "progress.tool_result",
            f"Tool result: {tool_name} {status} - {result_summary}",
            {"tool_name": tool_name, "ok": ok, "result": result_summary},
        )

    def command_start(self, command: str) -> None:
        self._emit("progress.command_start", f"Running command: {command}", {"command": command})

    def command_result(self, ok: bool, exit_code: int | None, summary: str) -> None:
        status = "succeeded" if ok else "failed"
        self._emit(
            "progress.command_result",
            f"Command {status}: {summary}",
            {"ok": ok, "exit_code": exit_code, "summary": summary},
        )

    def warning(self, message: str) -> None:
        self._emit("progress.warning", message, {})

    def done(self, message: str) -> None:
        self._emit("progress.done", message, {})

    def failed(self, message: str) -> None:
        self._emit("progress.failed", message, {})

    def heartbeat(self, phase: str = "working") -> None:
        elapsed = int(time.monotonic() - self.started)
        self._emit(
            "progress.heartbeat",
            f"Still working... phase={phase}, elapsed={elapsed}s, last={self.last_message or 'starting'}",
            {"phase": phase, "elapsed_seconds": elapsed, "last_action": self.last_message},
        )

    def _emit(self, kind: str, message: str, payload: dict[str, Any]) -> None:
        self.step_index += 1
        safe_message = redact(_truncate(message, 600))
        self.last_message = safe_message
        prefix = f"[{self.step_index}"
        if self.max_steps:
            prefix += f"/{self.max_steps}"
        prefix += "]"
        if self.console:
            style = "dim"
            if kind.endswith("failed") or payload.get("ok") is False:
                style = "yellow"
            self.console.print(f"[{style}]{prefix} {safe_message}[/{style}]")
        if self.session_logger:
            self.session_logger.log(
                "progress.event",
                {"kind": kind, **_compact_payload(payload)},
                safe_message,
                workflow_id="progress",
            )


def summarize_tool_args(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name == "read_file":
        return f"file={arguments.get('filepath', '?')}"
    if tool_name == "write_file":
        content = str(arguments.get("content", ""))
        return f"file={arguments.get('filepath', '?')}, chars={len(content)}"
    if tool_name == "run_command":
        return f"command={arguments.get('command', '?')}"
    if tool_name == "search_index":
        return f"query={arguments.get('query', '?')}"
    if tool_name == "list_files":
        return f"path={arguments.get('path', '.')}"
    if tool_name == "find_file":
        return f"query={arguments.get('query', '?')}"
    if tool_name == "grep_files":
        return f"query={arguments.get('query', '?')}, path={arguments.get('path', '.')}"
    if tool_name == "ask_user":
        return f"question={_truncate(str(arguments.get('question', '?')), 80)}"
    return ", ".join(f"{key}={_truncate(str(value), 80)}" for key, value in arguments.items())


def summarize_tool_result(result: Any) -> str:
    ok = bool(getattr(result, "ok", False))
    message = str(getattr(result, "message", ""))
    data = getattr(result, "data", {}) or {}
    if isinstance(data, dict):
        if "exit_code" in data:
            stdout = str(data.get("stdout") or data.get("stderr") or "")
            return f"exit={data.get('exit_code')}, {message}; {_important_excerpt(stdout)}"
        if "content" in data:
            return f"{message} ({len(str(data.get('content', '')).splitlines())} lines)"
        if "results" in data:
            return f"{message} ({len(data.get('results') or [])} results)"
        if "candidates" in data:
            candidates = data.get("candidates") or []
            preview = ", ".join(str(item) for item in candidates[:3])
            return f"{message}{(' - ' + preview) if preview else ''}"
        if "matches" in data:
            return f"{message} ({len(data.get('matches') or [])} matches)"
        if "listing" in data:
            return f"{message}"
    return message or ("ok" if ok else "failed")


def _important_excerpt(text: str, limit: int = 220) -> str:
    lines = [line.strip() for line in redact(text).splitlines() if line.strip()]
    interesting = [
        line for line in lines
        if any(token in line.lower() for token in ("error", "failed", "traceback", "ts", "exception"))
    ]
    chosen = interesting[:3] or lines[:2]
    return _truncate(" | ".join(chosen), limit)


def _compact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {str(key): _truncate(redact(str(value)), 1000) for key, value in payload.items()}


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"
