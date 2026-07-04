"""Stateful ReAct chat loop using Ollama's native tool calling."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ollama

from shamsu.agents.chat_state import ChatState
from shamsu.agents.markdown_fallback import MarkdownWriteFallback
from shamsu.llm.manager import OLLAMA_BASE_URL, _validate_local_llm_url
from shamsu.runtime.models import SPECIALIST_MODELS
from shamsu.safety.clarify import ask_clarifying_question
from shamsu.session.manager import SessionLogger
from shamsu.tools.agent_tools import AgentToolRegistry

# Circuit-breaker ceiling used only in long-running mode — a backstop, not
# the normal stop condition (the repetition guard is what actually catches
# a stuck loop; this just bounds worst-case cost on a local machine).
LONG_RUNNING_MAX_TOOL_ROUNDS = 40

AGENT_SYSTEM_PROMPT = """You are SHAMSU, a local coding agent running inside one workspace.

Rules:
- Be brief. Do not add filler.
- For greetings or casual chat, answer naturally in one short sentence.
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
        long_running: bool = False,
        clarify_prompt: Callable[[str], str] | None = ask_clarifying_question,
        on_activity: Callable[[str], None] | None = None,
    ) -> None:
        _validate_local_llm_url(base_url)
        self.workspace_root = Path(workspace_root).resolve()
        self.session_logger = session_logger
        self.model_name = model_name or SPECIALIST_MODELS["qa"]
        self.client = client or ollama.AsyncClient(host=base_url)
        self.tools = tools or AgentToolRegistry(self.workspace_root, session_logger=session_logger)
        # Optional hook to surface live tool activity (e.g. "Writing game.js")
        # to the REPL while the loop runs. None keeps the loop silent (tests).
        self.on_activity = on_activity
        self.state = state or ChatState(
            _system_prompt(self.workspace_root),
            session_logger=session_logger,
        )
        self.long_running = long_running
        self.max_tool_rounds = LONG_RUNNING_MAX_TOOL_ROUNDS if long_running else max_tool_rounds
        # Only used when long_running=True; None disables the clarifying
        # question (falls back to a plain stop message) — useful for tests.
        self.clarify_prompt = clarify_prompt if long_running else None
        self.markdown_fallback = MarkdownWriteFallback(self.tools)

    async def run(self, user_input: str) -> AgentLoopResult:
        self.state.append_user(user_input)
        last_call_signature: tuple[str, str] | None = None
        for round_index in range(self.max_tool_rounds):
            try:
                response = await self.client.chat(
                    model=self.model_name,
                    messages=self.state.messages(),
                    tools=self.tools.tool_schemas(),
                    stream=False,
                    options={"temperature": 0.1, "num_ctx": 8192},
                )
            except Exception as exc:
                final = _friendly_ollama_error(exc)
                self.state.append_assistant(final)
                return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)
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
                signature = (name, json.dumps(arguments, sort_keys=True, default=str))
                if self.long_running and signature == last_call_signature:
                    return self._handle_stuck_repetition(name, round_index)
                last_call_signature = signature
                if self.on_activity:
                    self.on_activity(_describe_tool_call(name, arguments))
                result = self.tools.execute(name, arguments)
                if self.on_activity and not result.ok:
                    self.on_activity(f"failed: {result.message}")
                self.state.append_tool(_tool_call_id(call, name), name, result.to_json())
        final = f"I stopped after {self.max_tool_rounds} tool rounds to avoid looping."
        self.state.append_assistant(final)
        return AgentLoopResult(final=final, tool_rounds=self.max_tool_rounds, stopped=True)

    def _handle_stuck_repetition(self, tool_name: str, round_index: int) -> AgentLoopResult:
        """The exact same tool call repeated consecutively — the agent is
        stuck, not making progress. In long-running mode, ask a genuine
        clarifying question instead of silently looping or giving up.
        """
        question = (
            f"I tried to repeat the exact same action ({tool_name}) without making "
            f"progress. What should I do differently, or should I stop?"
        )
        if self.clarify_prompt is None:
            final = f"I stopped because I kept repeating the same action ({tool_name}) without making progress."
            self.state.append_assistant(final)
            return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)
        answer = self.clarify_prompt(question)
        self.state.append_assistant(question)
        self.state.append_user(answer)
        final = (
            f"{question}\n\nYou said: {answer}\n\n"
            f"I've noted that — ask me to continue and I'll factor it in."
        )
        if self.session_logger:
            self.session_logger.log(
                "agent.clarify",
                {"tool_name": tool_name, "question": question, "answer": answer},
                "Asked a clarifying question after a stuck repetition",
                workflow_id="agent-chat",
            )
        return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)


def _describe_tool_call(name: str, arguments: dict[str, Any]) -> str:
    """A short human-readable label for a tool call, for live REPL activity."""
    if name == "write_file":
        return f"Writing {arguments.get('filepath', '?')}"
    if name == "read_file":
        return f"Reading {arguments.get('filepath', '?')}"
    if name == "run_command":
        return f"Running: {arguments.get('command', '?')}"
    if name == "list_files":
        return f"Listing {arguments.get('path', '.')}"
    if name == "search_index":
        return f"Searching: {arguments.get('query', '?')}"
    return f"Tool: {name}"


def _system_prompt(workspace: Path) -> str:
    return f"{AGENT_SYSTEM_PROMPT}\nWorkspace: {workspace}\n"


def _friendly_ollama_error(exc: Exception) -> str:
    message = str(exc).strip()
    lowered = message.lower()
    if "connect" in lowered or "connection" in lowered or "not running" in lowered:
        return (
            "Local AI is unavailable because Ollama is not running. "
            "Run `/models repair` to start/repair the local runtime, then try again."
        )
    if "model" in lowered and ("not found" in lowered or "pull" in lowered):
        return (
            "The required local model is missing. "
            "Run `/models pull` to download missing models, then try again."
        )
    return f"Local AI failed safely: {message or exc.__class__.__name__}"


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
