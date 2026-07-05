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
from shamsu.runtime.models import model_for_role
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
- If the user asks you to create, write, save, generate, add, edit, or update a file,
  your next action must be a write_file tool call or a clarification question.
- To create OR change a file, call write_file with the COMPLETE new file content. It
  overwrites, so never send a partial file or a diff — send the whole file every time.
- A file change only counts if the write_file tool result says ok. If a tool result shows
  an error, the change did NOT happen: do not assume success, read the file if needed and
  call write_file again with the full corrected content.
- Never reply with conversational filler like "noted" or "ask me to continue". Either call a
  tool to make progress or state the concrete result. Do not repeat an identical tool call.
- If you need to run code/tests, call run_command.
- If a slash command starts with /, do not answer it. The CLI handles slash commands.
- Keep all paths relative to the workspace.
- Do not access files outside the workspace.
- After tool results, summarize exactly what happened and what remains.
"""

# How many times the exact same tool call may repeat before we stop the loop.
_MAX_REPEATED_CALLS = 3


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
        self.model_name = model_name or model_for_role("qa")
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
        repeat_count = 0
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
                    if fallback.tool_results:
                        for index, tool_result in enumerate(fallback.tool_results):
                            self.state.append_tool(
                                f"markdown_fallback_{index}",
                                "write_file",
                                tool_result.to_json(),
                            )
                    else:
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
                    # The exact same call repeated — instead of stopping with
                    # conversational filler, push a firm correction back into the
                    # conversation and let the model try a DIFFERENT action.
                    repeat_count += 1
                    if repeat_count >= _MAX_REPEATED_CALLS:
                        return self._give_up_on_repetition(name, round_index)
                    self.state.append_user(_repetition_correction(name))
                    break  # re-prompt without executing the repeat
                last_call_signature = signature
                repeat_count = 0
                if self.on_activity:
                    self.on_activity(_describe_tool_call(name, arguments))
                result = self.tools.execute(name, arguments)
                if self.on_activity and not result.ok:
                    self.on_activity(f"failed: {result.message}")
                self.state.append_tool(_tool_call_id(call, name), name, result.to_json())
                if name == "write_file" and not result.ok:
                    # A write that did not land is the #1 cause of the model
                    # "hallucinating success" and then compiling half-written
                    # files. Make the failure loud and demand a full re-write.
                    self.state.append_user(
                        _write_failure_correction(str(arguments.get("filepath", "the file")), result.message)
                    )
        final = f"I stopped after {self.max_tool_rounds} tool rounds to avoid looping."
        self.state.append_assistant(final)
        return AgentLoopResult(final=final, tool_rounds=self.max_tool_rounds, stopped=True)

    def _give_up_on_repetition(self, tool_name: str, round_index: int) -> AgentLoopResult:
        """The same tool call repeated past the limit despite corrections — stop
        cleanly rather than burn rounds. No clarifying question, no filler."""
        final = (
            f"I stopped because the same action ({tool_name}) kept repeating without making "
            f"progress. It likely needs a different approach or more detail."
        )
        self.state.append_assistant(final)
        if self.session_logger:
            self.session_logger.log(
                "agent.stuck",
                {"tool_name": tool_name},
                "Stopped after repeated identical tool calls",
                workflow_id="agent-chat",
            )
        return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)


def _repetition_correction(tool_name: str) -> str:
    return (
        f"STOP. You just issued the exact same {tool_name} call again, which did not make "
        f"progress. Do NOT repeat it and do NOT reply with an apology or 'noted'. Take a "
        f"DIFFERENT concrete action now: if a file write failed, read the file then call "
        f"write_file with the COMPLETE corrected content; otherwise call a different tool to "
        f"diagnose or advance. Respond with a tool call, not prose."
    )


def _write_failure_correction(filepath: str, message: str) -> str:
    return (
        f"Your write_file to {filepath} did NOT succeed: {message}. The file was NOT changed. "
        f"Do not assume the fix was applied and do not move on. Call write_file again for "
        f"{filepath} with the ENTIRE corrected file content."
    )


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
