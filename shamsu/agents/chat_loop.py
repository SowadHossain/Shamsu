"""Stateful ReAct chat loop using Ollama's native tool calling."""
from __future__ import annotations

import asyncio
import json
import os as _os
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import ollama

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.agents.chat_state import ChatState
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

File tools:
- read_file reads a file. If it says "Not a file" / returns candidates, the file was NOT read:
  do not claim any knowledge of its contents.
- When read_file returns candidates, your NEXT response must call read_file on one candidate,
  or call find_file/grep_files - not prose. Never say "I will read X next" without emitting a
  read_file tool call in that SAME response.
- A path copied from a build/compile error or traceback may not be the real workspace path
  (a build can report `src/App.tsx` when the file is at `client/src/App.tsx`, or `index.html`
  when it is under `client/`). read_file auto-resolves a unique match; if it cannot, call
  find_file with the file's BASENAME as the query, or list_files, to locate the real path
  before editing. Never call grep_files with an empty query.
- Use find_file when you are unsure of a path (search by name). Use grep_files when you know a
  symbol or text but not the file. Use file_info to check a path before editing it.
- Use edit_file for small, targeted changes: pass the exact old_string and new_string. It must
  match exactly once (or set replace_all=true).
- Use write_file only to create a new file or fully rewrite one, passing the COMPLETE content.
- If the user asks you to create, write, save, generate, add, edit, or update a file,
  your next action must be an edit_file/write_file tool call or a clarification question.
- A file change only counts if the edit_file/write_file tool result says ok. If a tool result
  shows an error, the change did NOT happen: do not assume success, read the file if needed and
  call the tool again with the corrected content.
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

# Read/discovery tools whose failures get an explicit recovery instruction so the
# model reaches for a candidate/find_file/grep_files instead of stalling.
_READ_TOOLS = {"read_file", "file_info", "find_file", "grep_files"}

# Prose that signals the model is *promising* to read a file rather than actually
# calling the tool ("I will read entities.ts next") - the exact stall this loop
# guards against after a failed read.
_READ_STALL_PHRASES = (
    "i will read",
    "i'll read",
    "let me read",
    "i will now read",
    "next i will",
    "i will try",
    "i'll try",
    "let me try",
    "let's correct",
    "let me check the file",
    "i will check the file",
    "i will open",
    "let me open",
)

# Cap on prose-only stall recoveries after a failed read, so a model that keeps
# refusing to call a tool cannot spin the loop forever.
_MAX_READ_RECOVERIES = 3


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
        user_input = self._append_long_term_memory(user_input)
        user_input = await self._append_plan(user_input)
        self.state.append_user(user_input)
        repeated_calls: Counter[tuple[str, str]] = Counter()
        unconfirmed_failed_writes: dict[str, str] = {}
        # The most recent read_file failure that has not yet been recovered from,
        # plus a cap on prose-only "I'll read X next" stalls after such a failure.
        last_failed_read: dict[str, Any] | None = None
        read_recovery_attempts = 0
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
                # A read just failed and the model answered with a bare promise
                # ("I will read entities.ts next") instead of a tool call - the
                # exact stall that used to end the loop on a hollow statement.
                # Push one more recovery round rather than returning the promise.
                if last_failed_read is not None and _looks_like_read_stall(content):
                    if read_recovery_attempts < _MAX_READ_RECOVERIES:
                        read_recovery_attempts += 1
                        self.state.append_user(
                            _read_failure_correction(
                                str(last_failed_read.get("filepath", "the file")),
                                str(last_failed_read.get("message", "Not a file.")),
                                list(last_failed_read.get("candidates", [])),
                            )
                        )
                        continue
                    final = _read_blocked_final(last_failed_read)
                    self.state.append_assistant(final)
                    return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)
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
                if name == "read_file":
                    if result.ok:
                        # A successful read (including an auto-resolved candidate)
                        # clears the outstanding failure so the stall guard resets.
                        last_failed_read = None
                    else:
                        candidates = (
                            result.data.get("candidates", []) if isinstance(result.data, dict) else []
                        )
                        last_failed_read = {
                            "filepath": str(arguments.get("filepath", "the file")),
                            "message": result.message,
                            "candidates": list(candidates or []),
                        }
                        # Make the failed read loud and prescriptive so the model
                        # recovers with a real tool call instead of a bare promise.
                        self.state.append_user(
                            _read_failure_correction(
                                last_failed_read["filepath"],
                                last_failed_read["message"],
                                last_failed_read["candidates"],
                            )
                        )
                if name == "run_command" and not result.ok and self.session_logger is not None:
                    # Persist the failing command + parsed errors so a later
                    # bug-fix request with no explicit target can reuse it.
                    data = result.data if isinstance(result.data, dict) else {}
                    errors = str(
                        data.get("diagnostics")
                        or data.get("stderr")
                        or data.get("stdout")
                        or result.message
                    )
                    try:
                        self.session_logger.set_last_failure(
                            str(arguments.get("command", "")),
                            errors,
                            int(data.get("exit_code", 1) or 1),
                        )
                    except Exception:
                        pass
                if name in {"find_file", "grep_files"} and not result.ok:
                    # A discovery tool that failed (usually an empty query) burns a
                    # round; steer the model to a concrete query, reusing the
                    # basename of a file a preceding read could not locate.
                    hint_name = (
                        _basename(str(last_failed_read.get("filepath", "")))
                        if last_failed_read
                        else ""
                    )
                    self.state.append_user(
                        _discovery_failure_correction(name, result.message, hint_name)
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
            # Persist the plan as the session's last tool/workflow plan so a
            # resumed session knows what the current request was working toward.
            try:
                self.session_logger.set_last_tool_plan([{"type": "plan", "text": plan.text}])
            except Exception:
                pass
        return f"{user_input}\n\nPlan from planner model:\n{plan.text}"

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


def _basename(filepath: str) -> str:
    return filepath.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or filepath


def _read_failure_correction(filepath: str, message: str, candidates: list[str]) -> str:
    if candidates:
        cand_text = ", ".join(candidates[:6])
        instruction = (
            f"The file was NOT read. Candidates: {cand_text}. Your NEXT response MUST call "
            "read_file with one of those candidate paths (or call find_file/grep_files to locate "
            "the right file)."
        )
    else:
        # No candidates: hand the model a concrete, ready-to-run discovery call so
        # it doesn't stall or reach for grep_files with an empty query.
        basename = _basename(filepath)
        instruction = (
            "The file was NOT read and no similar files were found. Your NEXT response MUST call "
            f'find_file with query="{basename}" (find_file searches by file name), or list_files '
            'with path="." to inspect the tree. Do NOT call grep_files without a concrete text '
            "query, and do NOT invent a path."
        )
    return (
        f'Your read_file call for "{filepath}" failed: {message} {instruction} '
        'Do NOT say "I will read..." or "let me read..." without emitting a read_file tool call in '
        "the SAME response, and do NOT claim you read or know the contents of that file."
    )


def _discovery_failure_correction(tool_name: str, message: str, hint_name: str) -> str:
    """A discovery tool (find_file/grep_files) failed - most often called with an
    empty query, which burns a loop round. Steer the model to a concrete query,
    preferring the basename of the file a preceding read_file could not find."""
    lowered = message.lower()
    if "missing query" in lowered or "query" in lowered:
        if hint_name:
            return (
                f"Your {tool_name} call failed: {message} You must pass a non-empty query. To find "
                f'the missing file, your NEXT response MUST call find_file with query="{hint_name}" '
                f"(find_file searches by file name). Do not repeat {tool_name} with an empty query."
            )
        return (
            f"Your {tool_name} call failed: {message} Pass a non-empty query - a file name for "
            "find_file, or a symbol/text string for grep_files - and try again."
        )
    return (
        f"Your {tool_name} call failed: {message} Adjust the arguments and try a different query or "
        "path, or state plainly that you are blocked."
    )


def _read_blocked_final(failed_read: dict[str, Any]) -> str:
    filepath = failed_read.get("filepath", "the file")
    candidates = failed_read.get("candidates") or []
    if candidates:
        return (
            f"I could not read {filepath}: it does not exist at that path. Closest matches in the "
            f"workspace: {', '.join(candidates[:6])}. Tell me which one to use and I'll read it."
        )
    return (
        f"I could not read {filepath}: it does not exist in the workspace and no similar files were "
        "found. Please double-check the path."
    )


def _looks_like_read_stall(content: str) -> bool:
    lowered = (content or "").lower()
    return any(phrase in lowered for phrase in _READ_STALL_PHRASES)


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
