"""Stateful ReAct chat loop using Ollama's native tool calling."""
from __future__ import annotations

import asyncio
import json
import os as _os
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ollama

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.agents.chat_state import ChatState
from shamsu.agents.clarification import format_question
from shamsu.agents.markdown_fallback import MarkdownWriteFallback
from shamsu.agents.planner import create_plan
from shamsu.context.builder import ContextBuilder
from shamsu.context.manager import ContextBudgetManager
from shamsu.interfaces import IContextBuilder, ILLMManager
from shamsu.llm.manager import OLLAMA_BASE_URL, LLMManager, _validate_local_llm_url
from shamsu.memory.service import MemoryService
from shamsu.runtime.models import model_for_role
from shamsu.safety.clarify import ask_clarifying_question
from shamsu.session.manager import SessionLogger
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.ui.progress import ProgressReporter, summarize_tool_args, summarize_tool_result

# Circuit-breaker ceiling used only in long-running mode — a backstop, not
# the normal stop condition (the repetition guard is what actually catches
# a stuck loop; this just bounds worst-case cost on a local machine).
DEFAULT_MAX_TOOL_ROUNDS = 8
LONG_RUNNING_MAX_TOOL_ROUNDS = 50

# Guard against a local model that never responds.  Local inference can stall
# indefinitely when the model is swapping or the GPU is saturated; these caps
# bound the worst-case wall-clock cost on a developer machine.
# Override with env var SHAMSU_MODEL_TIMEOUT_SECONDS (integer).
_MODEL_CALL_TIMEOUT_SECONDS: int = int(_os.environ.get("SHAMSU_MODEL_TIMEOUT_SECONDS", "120"))

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
  overwrites, so never send a partial file or a diff â€" send the whole file every time.
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

## Clarification rules
- Ask the user a clear question (call ask_user) when required input is missing and tools cannot safely infer it.
- Do not guess between multiple destructive or ambiguous choices.
- Use read-only tools (find_file, grep_files, list_files, read_file) to gather missing context before asking.
- For multiple file candidates, call ask_user with the candidates as options so the user can choose.
- Ask for a commit message, branch/remote, or a specific target when those are required and ambiguous.

## Visible process rules
- Do not expose hidden chain-of-thought.
- Do show concise action summaries: what tool you used, its result, and what remains.
- Never say "I will use/read/run..." without making the corresponding tool call in the same turn.
- If blocked, say exactly what input is needed, or call ask_user.
"""

# How many times the exact same tool call may repeat before we stop the loop.
_MAX_REPEATED_CALLS = 3

# How many times we correct a prose-only "I will read X next" reply that did not
# actually call a tool, before giving up (a backstop against a chatty model).
_MAX_PROSE_CORRECTIONS = 2

# Phrases that signal the assistant *promised* a tool action but did not call
# one. Used only when there are no tool calls in the reply.
_DEFERRED_ACTION_PATTERNS = (
    r"\bi('?ll| will| am going to| am gonna| shall)\b.*\b(read|open|write|edit|run|check|look|search|fix|create|update|inspect|try)\b",
    r"\blet me\b.*\b(read|open|write|edit|run|check|look|search|fix|create|update|inspect|try)\b",
    r"\bnext(,| i)\b.*\b(read|open|write|edit|run|check|look|search|fix|will)\b",
    r"\bi('?ll| will)\b\s+(correct|retry|redo|do that|handle)\b",
)

# Callback used to surface structured trace events (route/plan/blockers/etc.)
# to the REPL. None keeps the loop silent (tests, non-interactive callers).
TraceCallback = Callable[[str, str, "dict[str, Any] | None", str], None]


@dataclass(frozen=True)
class AgentLoopResult:
    final: str
    tool_rounds: int = 0
    stopped: bool = False
    awaiting_user: bool = False


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
        on_trace: TraceCallback | None = None,
        progress: ProgressReporter | None = None,
        action_ledger: ActionLedger | None = None,
        llm: ILLMManager | None = None,
        context_builder: IContextBuilder | None = None,
        budget_manager: ContextBudgetManager | None = None,
    ) -> None:
        _validate_local_llm_url(base_url)
        self.workspace_root = Path(workspace_root).resolve()
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self.model_name = model_name or model_for_role("qa")
        self.llm = llm or LLMManager(session_logger=session_logger, action_ledger=action_ledger)
        self.context_builder = context_builder or ContextBuilder()
        self.client = client or ollama.AsyncClient(host=base_url)
        self.tools = tools or AgentToolRegistry(self.workspace_root, session_logger=session_logger)
        # Optional hook to surface live tool activity (e.g. "Writing game.js")
        # to the REPL while the loop runs. None keeps the loop silent (tests).
        self.on_activity = on_activity
        self.on_trace = on_trace
        self.progress = progress
        self.budget_manager = budget_manager
        self.state = state or ChatState(
            _system_prompt(self.workspace_root),
            session_logger=session_logger,
        )
        self.long_running = long_running
        self.max_tool_rounds = LONG_RUNNING_MAX_TOOL_ROUNDS if long_running else max_tool_rounds
        # Only used when long_running=True; None disables the clarifying
        # question (falls back to a plain stop message) â€" useful for tests.
        self.clarify_prompt = clarify_prompt if long_running else None
        self.markdown_fallback = MarkdownWriteFallback(self.tools)

    async def run(self, user_input: str) -> AgentLoopResult:
        original_input = user_input
        user_input = self._append_long_term_memory(user_input)
        user_input = await self._append_plan(user_input)
        self.state.append_user(user_input)
        repeated_calls: Counter[tuple[str, str]] = Counter()
        unconfirmed_failed_writes: dict[str, str] = {}
        prose_corrections = 0
        for round_index in range(self.max_tool_rounds):
            # Show context-window usage before each model call.
            if self.budget_manager:
                _messages = self.state.messages()
                _msg_text = "\n".join(str(m.get("content", "")) for m in _messages)
                _budget = self.budget_manager.compute(self.model_name, "chat", _msg_text)
                self.budget_manager.show_indicator(_budget)
            try:
                response = await asyncio.wait_for(
                    self.client.chat(
                        model=self.model_name,
                        messages=self.state.messages(),
                        tools=self.tools.tool_schemas(),
                        stream=False,
                        options={"temperature": 0.1, "num_ctx": 8192},
                    ),
                    timeout=_MODEL_CALL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                final = (
                    f"The model did not respond within {_MODEL_CALL_TIMEOUT_SECONDS}s. "
                    "This usually means local inference is stalled (GPU saturated, model swapping, "
                    "or the context is too large). "
                    "Try a lighter model with `/models tier light`, reduce context, or restart Ollama."
                )
                self.state.append_assistant(final)
                return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)
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
                if _looks_like_deferred_action(content) and prose_corrections < _MAX_PROSE_CORRECTIONS:
                    # The model said it would take a tool action ("I will read X
                    # next") but did not call a tool. Do not end the turn on an
                    # empty promise: demand a real tool call, an ask_user, or an
                    # explicit "I am blocked" and give it one more round.
                    prose_corrections += 1
                    self.state.append_user(_PROSE_ONLY_CORRECTION)
                    self._emit_trace(
                        "workflow.blocked",
                        "Assistant promised a tool action without calling one; asking it to act or ask.",
                        {"attempt": prose_corrections},
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
                ledger_call_id = self.action_ledger.log_tool_call(name, arguments) if self.action_ledger else ""
                result = self.tools.execute(name, arguments)
                self._log_tool_result(name, result)
                if self.action_ledger:
                    self.action_ledger.log_tool_result(
                        ledger_call_id, name, bool(result.ok), result.message, result.data
                    )
                if self.progress:
                    self.progress.tool_result(name, summarize_tool_result(result), ok=result.ok)
                if self.on_activity and not result.ok:
                    self.on_activity(f"failed: {result.message}")
                self.state.append_tool(_tool_call_id(call, name), name, result.to_json())
                if name == "ask_user" and result.ok and isinstance(result.data, dict) and result.data.get("ask_user"):
                    # ask_user ends the turn: store the pending question and hand
                    # control back to the user (resolved on their next reply).
                    return self._handle_ask_user(
                        result.data.get("pending_question", {}), original_input, round_index
                    )
                if name == "read_file" and not result.ok:
                    # A wrong/ambiguous path is the #1 reason SHAMSU used to say
                    # "I'll read X next" and then stall. Turn the failure into a
                    # concrete next step: read the one strong candidate, ask the
                    # user to choose between several, or discover the path.
                    correction = self._read_failure_correction(
                        str(arguments.get("filepath", "")), result.message
                    )
                    self.state.append_user(correction)
                    self._emit_trace(
                        "tool.failed",
                        f"read_file {arguments.get('filepath', '')} failed: {result.message}",
                        {"filepath": str(arguments.get("filepath", ""))},
                    )
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
                    correction = _write_failure_correction(
                        str(arguments.get("filepath", "the file")), result.message
                    )
                    self.state.append_user(correction)
                    # WinError 32 (file locked) cannot be fixed by retrying the
                    # same write: stop the loop immediately so the user is told
                    # to close the locking process instead of watching it spin.
                    lowered_msg = result.message.lower()
                    if (
                        "winerror 32" in lowered_msg
                        or "being used by another process" in lowered_msg
                        or "sharing violation" in lowered_msg
                    ):
                        final = correction
                        self.state.append_assistant(final)
                        return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)
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
        if self.action_ledger:
            self.action_ledger.log_graphiti_retrieved(has_memory=True)
        return f"{user_input}\n\n{memory_context}"

    async def _append_plan(self, user_input: str) -> str:
        """One planner call per top-level request (not per tool round),
        mirroring CodeEditWorkflow/BugFixWorkflow - see shamsu/agents/planner.py.
        Best-effort: a planner failure must never block the chat loop itself."""
        try:
            plan = await create_plan(
                self.llm, self.context_builder, results=[], goal=user_input, task_id="agent-chat-plan",
            )
        except Exception:
            return user_input
        if not plan.text:
            return user_input
        if self.session_logger:
            self.session_logger.log(
                "planner.plan",
                {"plan": plan.text},
                "Planner produced a plan for this request",
                workflow_id="agent-chat",
            )
        # Surface the plan as a visible trace event (never the hidden reasoning
        # behind it - just the short, action-focused plan text).
        self._emit_trace("plan.created", plan.text)
        return f"{user_input}\n\nPlan from planner model:\n{plan.text}"

    def _emit_trace(
        self,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
        level: str = "normal",
    ) -> None:
        if self.on_trace is None:
            return
        try:
            self.on_trace(event_type, message, payload, level)
        except Exception:
            # Trace output is cosmetic; never let it break the agent loop.
            pass

    def _handle_ask_user(
        self, pending: dict[str, Any], original_input: str, round_index: int
    ) -> AgentLoopResult:
        pending = dict(pending or {})
        pending.setdefault("awaiting", "user_input")
        pending["created_from_prompt"] = original_input
        if self.session_logger:
            self.session_logger.set_pending_question(pending)
        final = format_question(pending)
        self.state.append_assistant(final)
        self._emit_trace(
            "clarification.needed",
            str(pending.get("question", "")),
            {"options": [str(option.get("label", "")) for option in pending.get("options", [])]},
        )
        return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True, awaiting_user=True)

    def _read_failure_correction(self, filepath: str, message: str) -> str:
        candidates: list[str] = []
        query = Path(filepath).name or filepath
        if query.strip():
            try:
                find_result = self.tools.find_file(query)
                if find_result.ok:
                    candidates = list(find_result.data.get("candidates", []))
            except Exception:
                candidates = []
        # Drop an exact self-match so we don't suggest the very path that failed.
        candidates = [candidate for candidate in candidates if candidate != filepath]
        if len(candidates) == 1:
            return (
                f"read_file {filepath} failed ({message}). The closest matching file is "
                f"{candidates[0]}. Call read_file with that exact path now - do not claim you read it yet."
            )
        if len(candidates) > 1:
            listed = "; ".join(candidates[:8])
            return (
                f"read_file {filepath} failed ({message}) and several files match: {listed}. "
                "Call ask_user with these as options so the user can choose, or read_file the correct "
                "one. Do NOT guess between them."
            )
        return (
            f"read_file {filepath} failed ({message}) and no candidate path matched. Use find_file or "
            "grep_files to locate the right file, or call ask_user for the correct path. Do not claim you read it."
        )

    def _give_up_on_repetition(self, tool_name: str, arguments: dict[str, Any], round_index: int) -> AgentLoopResult:
        """The same tool call repeated past the limit despite corrections â€" stop
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


_PROSE_ONLY_CORRECTION = (
    "You just said you will take another tool action, but you did not call a tool. "
    "Either call the tool now, ask the user a clear question with ask_user, or state "
    "plainly that you are blocked and what input you need. Do not reply with an "
    "intention alone."
)


def _looks_like_deferred_action(content: str) -> bool:
    """True when a tool-less reply merely *promises* a tool action."""
    text = " ".join(content.strip().lower().split())
    if not text:
        return False
    return any(re.search(pattern, text) for pattern in _DEFERRED_ACTION_PATTERNS)


def _repetition_correction(tool_name: str) -> str:
    return (
        f"STOP. You just issued the exact same {tool_name} call again, which did not make "
        f"progress. Do NOT repeat it and do NOT reply with an apology or 'noted'. Take a "
        f"DIFFERENT concrete action now: if a file write failed, read the file then call "
        f"write_file with the COMPLETE corrected content; otherwise call a different tool to "
        f"diagnose or advance. Respond with a tool call, not prose."
    )


def _write_failure_correction(filepath: str, message: str) -> str:
    lowered = message.lower()
    if "winerror 32" in lowered or "being used by another process" in lowered or "sharing violation" in lowered:
        extra = (
            " The file is locked by another process (WinError 32 / sharing violation). "
            "Close the dev server, file watcher, or any editor that may be holding the file open, "
            "then try writing again. Do NOT continue or claim success until the write is confirmed."
        )
    else:
        extra = ""
    return (
        f"Your write_file to {filepath} did NOT succeed: {message}.{extra} The file was NOT changed. "
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
    if name == "find_file":
        return f"Finding file: {arguments.get('query', '?')}"
    if name == "grep_files":
        return f"Searching files for: {arguments.get('query', '?')}"
    if name == "ask_user":
        return "Asking you a question"
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
