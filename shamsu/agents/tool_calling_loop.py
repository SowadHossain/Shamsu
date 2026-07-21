"""Native Ollama /api/chat tool-calling loop with narrow action tools."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.llm.manager import LLMManager
from shamsu.llm.output import parse_model_turn, tool_call_to_message_dict
from shamsu.runtime.models import model_for_role
from shamsu.runtime.run_control import complete_run, register_run
from shamsu.session.manager import SessionLogger
from shamsu.tools.registry import ToolRegistry
from shamsu.types import RunStatus, ToolCall, ToolResult

DEFAULT_MAX_TOOL_ITERATIONS = 8
DEFAULT_MAX_RUNTIME_SECONDS = 300

ACTION_AGENT_SYSTEM_PROMPT = """You are SHAMSU's action tool-calling agent.

Use only the registered action tools. Do not ask for or invent tools.
Retrieval tools such as code search and symbol lookup are unavailable because
Graphiti/Codebase-Memory handles retrieval outside this loop.

Rules:
- Call tools only when needed to run approved actions.
- Never claim a command, setup, or test ran unless a tool result confirms it.
- If a tool result is blocked, denied, or invalid, report that plainly.
- Keep final answers brief and concrete.
"""


@dataclass(frozen=True)
class ToolCallingAgentResult:
    run_id: str
    final: str
    status: RunStatus
    iterations: int = 0

    @property
    def stopped(self) -> bool:
        return self.status in {RunStatus.CANCELLED, RunStatus.FAILED, RunStatus.TIMED_OUT}


class ToolCallingAgentLoop:
    def __init__(
        self,
        workspace_root: Path,
        llm: LLMManager | Any | None = None,
        tools: ToolRegistry | None = None,
        model_name: str | None = None,
        run_id: str | None = None,
        session_logger: SessionLogger | None = None,
        action_ledger: ActionLedger | None = None,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.llm = llm or LLMManager(session_logger=session_logger, action_ledger=action_ledger)
        self.tools = tools or ToolRegistry(
            self.workspace_root,
            session_logger=session_logger,
            action_ledger=action_ledger,
        )
        self.model_name = model_name or model_for_role("qa")
        self.run_id = run_id or (action_ledger.run_id if action_ledger else f"toolrun-{uuid.uuid4().hex[:12]}")
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self.max_tool_iterations = max_tool_iterations
        self.max_runtime_seconds = max_runtime_seconds
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": ACTION_AGENT_SYSTEM_PROMPT}]
        # Names the shared output salvager may recover calls for (native calls
        # always pass through so an unknown tool still surfaces honestly).
        self._registered_tool_names = {
            str((schema.get("function") or {}).get("name") or "")
            for schema in self.tools.tool_schemas()
        }

    async def run(self, user_input: str) -> ToolCallingAgentResult:
        control = register_run(
            self.run_id,
            session_logger=self.session_logger,
            action_ledger=self.action_ledger,
        )
        self.messages.append({"role": "user", "content": user_input})
        started = time.monotonic()
        final = ""
        try:
            for iteration in range(self.max_tool_iterations):
                control.iterations = iteration
                if time.monotonic() - started > self.max_runtime_seconds:
                    final = "I stopped because the run exceeded its max runtime."
                    complete_run(self.run_id, RunStatus.TIMED_OUT, final)
                    return ToolCallingAgentResult(self.run_id, final, RunStatus.TIMED_OUT, iteration)
                if control.cancel_event.is_set():
                    final = "Run cancelled."
                    complete_run(self.run_id, RunStatus.CANCELLED, final)
                    return ToolCallingAgentResult(self.run_id, final, RunStatus.CANCELLED, iteration)
                self._inject_feedback(control)
                try:
                    response = await self._call_model(control)
                except asyncio.CancelledError:
                    if control.cancel_event.is_set():
                        final = "Run cancelled."
                        complete_run(self.run_id, RunStatus.CANCELLED, final)
                        return ToolCallingAgentResult(self.run_id, final, RunStatus.CANCELLED, iteration)
                    self._inject_feedback(control)
                    continue
                # Single normalization boundary (same parser as the chat loop):
                # native tool_calls when present, else salvaged from content.
                turn = parse_model_turn(response, self._registered_tool_names)
                content = turn.text
                tool_calls = turn.tool_calls
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": [tool_call_to_message_dict(call) for call in tool_calls],
                    }
                )
                if not tool_calls:
                    final = content
                    complete_run(self.run_id, RunStatus.COMPLETED, final)
                    return ToolCallingAgentResult(self.run_id, final, RunStatus.COMPLETED, iteration)
                for tool_call in tool_calls:
                    if control.cancel_event.is_set():
                        final = "Run cancelled."
                        complete_run(self.run_id, RunStatus.CANCELLED, final)
                        return ToolCallingAgentResult(self.run_id, final, RunStatus.CANCELLED, iteration)
                    result = self._execute_tool(tool_call)
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id or tool_call.name,
                            "name": tool_call.name,
                            "content": result.to_json(),
                        }
                    )
                    self._inject_feedback(control)
            final = f"I stopped after {self.max_tool_iterations} tool iterations to avoid looping."
            complete_run(self.run_id, RunStatus.FAILED, final)
            return ToolCallingAgentResult(self.run_id, final, RunStatus.FAILED, self.max_tool_iterations)
        except Exception as exc:
            final = f"Tool-calling run failed safely: {exc}"
            complete_run(self.run_id, RunStatus.FAILED, final)
            return ToolCallingAgentResult(self.run_id, final, RunStatus.FAILED, control.iterations)

    async def _call_model(self, control) -> dict[str, Any]:
        task = asyncio.create_task(
            self.llm.chat_with_tools(
                self.model_name,
                self.messages,
                self.tools.tool_schemas(),
                role="action-agent",
                workflow_id=self.run_id,
            )
        )
        control.current_model_task = task
        try:
            return await task
        finally:
            if control.current_model_task is task:
                control.current_model_task = None

    def _execute_tool(self, call: ToolCall) -> ToolResult:
        if not self.tools.has_tool(call.name):
            result = ToolResult(False, f"Unknown tool: {call.name}", {"tool": call.name})
            self._log_tool(call, result)
            return result
        validation = self.tools.validate_arguments(call.name, call.arguments)
        if not validation.ok:
            self._log_tool(call, validation)
            return validation
        ledger_call_id = self.action_ledger.log_tool_call(call.name, call.arguments) if self.action_ledger else ""
        result = self.tools.execute(call.name, call.arguments)
        if self.action_ledger:
            self.action_ledger.log_tool_result(
                ledger_call_id,
                call.name,
                result.ok,
                result.message,
                result.data,
            )
        self._log_tool(call, result)
        return result

    def _log_tool(self, call: ToolCall, result: ToolResult) -> None:
        if self.session_logger:
            self.session_logger.log(
                "agent.tool_result",
                {
                    "run_id": self.run_id,
                    "tool_name": call.name,
                    "ok": result.ok,
                    "message": result.message,
                },
                f"Action tool result: {call.name}",
                workflow_id=self.run_id,
            )

    def _inject_feedback(self, control) -> None:
        injected = False
        while True:
            try:
                text = control.feedback_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.messages.append(
                {
                    "role": "user",
                    "content": f"High-priority user feedback for this active run:\n{text}",
                }
            )
            injected = True
        if injected:
            control.feedback_event.clear()


