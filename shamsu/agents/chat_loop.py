"""Stateful ReAct chat loop using Ollama's native tool calling."""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ollama

from shamsu.agents.chat_state import ChatState
from shamsu.agents.markdown_fallback import MarkdownWriteFallback
from shamsu.llm.manager import OLLAMA_BASE_URL, _validate_local_llm_url
from shamsu.memory.service import MemoryService
from shamsu.runtime.models import model_for_role
from shamsu.safety.clarify import ask_clarifying_question
from shamsu.session.manager import SessionLogger
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.ui.progress import ProgressReporter, summarize_tool_args, summarize_tool_result

# Circuit-breaker ceiling used only in long-running mode â€” a backstop, not
# the normal stop condition (the repetition guard is what actually catches
# a stuck loop; this just bounds worst-case cost on a local machine).
DEFAULT_MAX_TOOL_ROUNDS = 8
LONG_RUNNING_MAX_TOOL_ROUNDS = 50

AGENT_SYSTEM_PROMPT = """You are SHAMSU, a local-first coding agent running inside one workspace.

Rules:
- Be brief. Do not add filler.
- For greetings or casual chat, answer naturally in one short sentence.
- Use deterministic tools before guessing.
- Use local tools to inspect files before making claims about them.
- Search the index before answering workspace-specific questions.
- Use tools for file reads, file writes, searches, and commands.
- Never claim you created, edited, deleted, searched, read, or ran anything unless a tool result confirms it.
- Read relevant files before editing.
- If the user asks you to create, write, save, generate, add, edit, or update a file,
  your next action must be a write_file tool call or a clarification question.
- To create OR change a file, call write_file with the COMPLETE new file content. It
  overwrites, so never send a partial file or a diff â€” send the whole file every time.
- A file change only counts if the write_file tool result says ok. If a tool result shows
  an error, the change did NOT happen: do not assume success, read the file if needed and
  call write_file again with the full corrected content.
- Never reply with conversational filler like "noted" or "ask me to continue". Either call a
  tool to make progress or state the concrete result. Do not repeat an identical tool call.
- If you need to run code/tests, call run_command.
- If a slash command starts with /, do not answer it. The CLI handles slash commands.
- Keep all paths relative to the workspace.
- All file operations must stay inside the workspace. Do not access files outside the workspace.
- Dangerous commands, sudo/admin commands, global installs, destructive deletes, and commands outside the workspace are not allowed.
- File writes and risky commands require approval.
- Do not reveal private reasoning. Give brief explanations and action summaries only.
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
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
        long_running: bool = False,
        clarify_prompt: Callable[[str], str] | None = ask_clarifying_question,
        on_activity: Callable[[str], None] | None = None,
        progress: ProgressReporter | None = None,
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
        self.progress = progress
        self.state = state or ChatState(
            _system_prompt(self.workspace_root),
            session_logger=session_logger,
        )
        self.long_running = long_running
        self.max_tool_rounds = LONG_RUNNING_MAX_TOOL_ROUNDS if long_running else max_tool_rounds
        # Only used when long_running=True; None disables the clarifying
        # question (falls back to a plain stop message) â€” useful for tests.
        self.clarify_prompt = clarify_prompt if long_running else None
        self.markdown_fallback = MarkdownWriteFallback(self.tools)

    async def run(self, user_input: str) -> AgentLoopResult:
        user_input = self._append_long_term_memory(user_input)
        self.state.append_user(user_input)
        repeated_calls: Counter[tuple[str, str]] = Counter()
        unconfirmed_failed_writes: dict[str, str] = {}
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
                json_action_call = _json_action_tool_call(content)
                if json_action_call:
                    tool_calls = [json_action_call]
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
                if unconfirmed_failed_writes:
                    final = _failed_write_final(unconfirmed_failed_writes)
                    self.state.append_assistant(final)
                    return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)
                return AgentLoopResult(final=content, tool_rounds=round_index)
            for call in tool_calls:
                name = _tool_call_name(call)
                arguments = _tool_call_arguments(call)
                signature = (name, json.dumps(arguments, sort_keys=True, default=str))
                repeated_calls[signature] += 1
                if repeated_calls[signature] >= _MAX_REPEATED_CALLS:
                    return self._give_up_on_repetition(name, arguments, round_index)
                if self.on_activity:
                    self.on_activity(_describe_tool_call(name, arguments))
                if self.progress:
                    self.progress.tool_start(name, summarize_tool_args(name, arguments))
                self._log_tool_call(name, arguments)
                result = self.tools.execute(name, arguments)
                self._log_tool_result(name, result)
                if self.progress:
                    self.progress.tool_result(name, summarize_tool_result(result), ok=result.ok)
                if self.on_activity and not result.ok:
                    self.on_activity(f"failed: {result.message}")
                self.state.append_tool(_tool_call_id(call, name), name, result.to_json())
                if name == "write_file":
                    filepath = str(arguments.get("filepath", "the file"))
                    if result.ok:
                        unconfirmed_failed_writes.pop(filepath, None)
                    else:
                        unconfirmed_failed_writes[filepath] = result.message
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

    def _append_long_term_memory(self, user_input: str) -> str:
        try:
            memory_context = MemoryService(self.workspace_root).render_relevant(
                user_input,
                task_type="agent-chat",
                limit=8,
            )
        except Exception:
            return user_input
        if not memory_context:
            return user_input
        if self.session_logger:
            self.session_logger.log(
                "memory.retrieved",
                {"specialist": "agent-chat", "has_memory": True},
                "Retrieved relevant Graphiti memories",
                workflow_id="agent-chat",
            )
        return f"{user_input}\n\n{memory_context}"
    def _give_up_on_repetition(self, tool_name: str, arguments: dict[str, Any], round_index: int) -> AgentLoopResult:
        """The same tool call repeated past the limit despite corrections â€” stop
        cleanly rather than burn rounds. No clarifying question, no filler."""
        final = (
            f"I stopped because the same {tool_name} call kept repeating without meaningful progress. "
            f"Target/context: {summarize_tool_args(tool_name, arguments)}. It likely needs a "
            "different patch, a changed file state, or more detail."
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

    def _log_tool_call(self, name: str, arguments: dict[str, Any]) -> None:
        if not self.session_logger:
            return
        self.session_logger.log(
            "agent.tool_call",
            {"tool_name": name, "arguments": _compact_value(arguments, limit=2000)},
            f"Agent called tool: {name}",
            workflow_id="agent-chat",
        )

    def _log_tool_result(self, name: str, result: Any) -> None:
        if not self.session_logger:
            return
        self.session_logger.log(
            "agent.tool_result",
            {
                "tool_name": name,
                "ok": bool(getattr(result, "ok", False)),
                "message": str(getattr(result, "message", "")),
                "data": _compact_value(getattr(result, "data", {}), limit=4000),
            },
            f"Agent tool result: {name}",
            workflow_id="agent-chat",
        )


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


def _failed_write_final(failed_writes: dict[str, str]) -> str:
    details = "; ".join(f"{path}: {message}" for path, message in failed_writes.items())
    return (
        "I could not confirm the file edit. The latest write_file attempt failed, so the "
        f"file was not edited successfully. Details: {details}"
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


def _json_action_tool_call(content: str) -> dict[str, Any] | None:
    stripped = content.strip()
    if not stripped or not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    action = data.get("action")
    arguments = data.get("arguments")
    allowed = {"list_files", "read_file", "write_file", "run_command", "search_index"}
    if action not in allowed or not isinstance(arguments, dict):
        return None
    return {
        "id": f"json_action_{action}",
        "function": {"name": action, "arguments": arguments},
    }


def _compact_value(value: Any, limit: int = 6000) -> Any:
    if isinstance(value, str):
        return _truncate_text(value, limit)
    if isinstance(value, list):
        compacted = [_compact_value(item, max(limit // 4, 500)) for item in value[:20]]
        if len(value) > 20:
            compacted.append(f"... [truncated {len(value) - 20} item(s)]")
        return compacted
    if isinstance(value, dict):
        items = list(value.items())[:40]
        per_item_limit = max(limit // max(len(items), 1), 500)
        compacted = {str(key): _compact_value(item, per_item_limit) for key, item in items}
        if len(value) > len(items):
            compacted["..."] = f"truncated {len(value) - len(items)} key(s)"
        return compacted
    return value


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [truncated {len(text) - limit} chars]"


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)

