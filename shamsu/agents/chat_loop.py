"""Stateful ReAct chat loop using Ollama's native tool calling."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ollama

from shamsu.agents.chat_state import ChatState
from shamsu.agents.markdown_fallback import MarkdownWriteFallback
from shamsu.llm.manager import OLLAMA_BASE_URL, _validate_local_llm_url
from shamsu.runtime.models import SPECIALIST_MODELS
from shamsu.session.manager import SessionLogger
from shamsu.tools.agent_tools import AgentToolRegistry

AGENT_SYSTEM_PROMPT = """You are SHAMSU, a local coding agent running inside one workspace.

Rules:
- Be brief. Do not add filler.
- Use tools for file reads, file writes, searches, and commands.
- Never claim you created, edited, read, or ran anything unless a tool result confirms it.
- If you need to create or modify a file, call write_file or ask for clarification.
- If you need to run code/tests, call run_command.
- If a slash command starts with /, do not answer it. The CLI handles slash commands.
- Keep all paths relative to the workspace.
- Do not access files outside the workspace.
- After tool results, summarize exactly what happened and what remains.
"""


@dataclass(frozen=True)
class AgentLoopResult:
    final: str
    tool_rounds: int = 0
    stopped: bool = False


class AgentChatLoop:
    def __init__(
        self,
        workspace_root: Path,
        session_logger: SessionLogger | None = None,
        model_name: str | None = None,
        base_url: str = OLLAMA_BASE_URL,
        client: Any | None = None,
        tools: AgentToolRegistry | None = None,
        state: ChatState | None = None,
        max_tool_rounds: int = 5,
    ) -> None:
        _validate_local_llm_url(base_url)
        self.workspace_root = Path(workspace_root).resolve()
        self.session_logger = session_logger
        self.model_name = model_name or SPECIALIST_MODELS["qa"]
        self.client = client or ollama.AsyncClient(host=base_url)
        self.tools = tools or AgentToolRegistry(self.workspace_root, session_logger=session_logger)
        self.state = state or ChatState(
            _system_prompt(self.workspace_root),
            session_logger=session_logger,
        )
        self.max_tool_rounds = max_tool_rounds
        self.markdown_fallback = MarkdownWriteFallback(self.tools)

    async def run(self, user_input: str) -> AgentLoopResult:
        self.state.append_user(user_input)
        for round_index in range(self.max_tool_rounds):
            response = await self.client.chat(
                model=self.model_name,
                messages=self.state.messages(),
                tools=self.tools.tool_schemas(),
                stream=False,
                options={"temperature": 0.1, "num_ctx": 8192},
            )
            message = _message_from_response(response)
            content = str(_get(message, "content", "") or "")
            tool_calls = _tool_calls_from_message(message)
            self.state.append_assistant(content, tool_calls=tool_calls)
            if not tool_calls:
                fallback = self.markdown_fallback.maybe_write(user_input, content)
                if fallback.handled:
                    self.state.append_tool(
                        "markdown_fallback",
                        "write_file",
                        fallback.tool_result.to_json() if fallback.tool_result else fallback.summary,
                    )
                    continue
                return AgentLoopResult(final=content, tool_rounds=round_index)
            for call in tool_calls:
                name = _tool_call_name(call)
                arguments = _tool_call_arguments(call)
                result = self.tools.execute(name, arguments)
                self.state.append_tool(_tool_call_id(call, name), name, result.to_json())
        final = "I stopped after 5 tool rounds to avoid looping."
        self.state.append_assistant(final)
        return AgentLoopResult(final=final, tool_rounds=self.max_tool_rounds, stopped=True)


def _system_prompt(workspace: Path) -> str:
    return f"{AGENT_SYSTEM_PROMPT}\nWorkspace: {workspace}\n"


def _message_from_response(response: Any) -> Any:
    return _get(response, "message", response)


def _tool_calls_from_message(message: Any) -> list[dict[str, Any]]:
    calls = _get(message, "tool_calls", []) or []
    normalized: list[dict[str, Any]] = []
    for call in calls:
        if isinstance(call, dict):
            normalized.append(call)
        else:
            function = _get(call, "function", {})
            normalized.append(
                {
                    "id": _get(call, "id", ""),
                    "function": {
                        "name": _get(function, "name", ""),
                        "arguments": _get(function, "arguments", {}),
                    },
                }
            )
    return normalized


def _tool_call_id(call: dict[str, Any], fallback: str) -> str:
    return str(call.get("id") or fallback)


def _tool_call_name(call: dict[str, Any]) -> str:
    function = call.get("function") or {}
    return str(function.get("name") or "")


def _tool_call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") or {}
    arguments = function.get("arguments") or {}
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return arguments if isinstance(arguments, dict) else {}


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)
