"""Stateful ReAct chat loop using Ollama's native tool calling."""
from __future__ import annotations

import asyncio
import difflib
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
from shamsu.audit import SessionAuditLog
from shamsu.agents.chat_state import ChatState
from shamsu.agents.clarification import format_question
from shamsu.agents.markdown_fallback import MarkdownWriteFallback
from shamsu.agents.planner import create_plan
from shamsu.context.budget import (
    RESERVE_OUTPUT_TOKENS,
    SAFETY_MARGIN_TOKENS,
    count_tokens,
    ctx_window_for_model,
)
from shamsu.context.builder import ContextBuilder
from shamsu.context.manager import ContextBudgetManager
from shamsu.safety import read_only
from shamsu.interfaces import IContextBuilder, ILLMManager
from shamsu.llm.manager import OLLAMA_BASE_URL, LLMManager, _validate_local_llm_url
from shamsu.llm.output import parse_model_turn, tool_call_to_message_dict
from shamsu.memory.service import MemoryService
from shamsu.runtime.models import (
    model_for_role,
    model_is_reasoning,
    model_supports_native_tools,
)
from shamsu.session.manager import SessionLogger
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.ui.progress import ProgressReporter, summarize_tool_args, summarize_tool_result
from shamsu.verify.gate import verify_only

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

# How often to emit a "still waiting for the model" heartbeat during a long model
# call, so a slow local model reads as working rather than a frozen prompt.
_HEARTBEAT_INTERVAL_SECONDS: int = int(_os.environ.get("SHAMSU_HEARTBEAT_SECONDS", "15"))

# Interactive-chat context window. The budget module targets 8GB machines, so we
# don't hand a 131k-window model (e.g. mistral-nemo) its full context by default;
# a 32k cap already gives ~4x the previous hard-coded 8192. Override with
# SHAMSU_CHAT_MAX_CTX. The rolling-summary budget is carved out of the usable
# window to carry a compressed synopsis of turns evicted by budget-aware trimming.
_CHAT_MAX_CTX: int = int(_os.environ.get("SHAMSU_CHAT_MAX_CTX", "32768"))
_CHAT_SUMMARY_BUDGET_TOKENS = 512

# Per-tool-result token budget. A single big read_file/grep_files result can
# otherwise blow the window mid-loop: the budget-aware history trimmer always
# keeps the most recent message, so one oversized result survives and crowds out
# everything else. Cap each result's tokens BEFORE it enters history and tell the
# model how to see more (a narrower range/query). Override with
# SHAMSU_TOOL_RESULT_MAX_TOKENS.
_TOOL_RESULT_MAX_TOKENS: int = int(_os.environ.get("SHAMSU_TOOL_RESULT_MAX_TOKENS", "2000"))

# The interactive agent tool loop does real tool-calling/file work, so it runs on
# the CODER model by default (fast, tool-friendly) rather than the "thinking"
# qa/router model, whose long reasoning traces stalled the executor mid-run.
# Override the executor role via SHAMSU_CHAT_ROLE.
_CHAT_EXECUTOR_ROLE = _os.environ.get("SHAMSU_CHAT_ROLE", "coder").strip() or "coder"
# The per-turn planner is on by default, but can be disabled (SHAMSU_CHAT_PLANNER=0)
# on very small machines where the extra planner-model round-trip + model swap
# adds too much latency.
# One bounded strict-repair pass when an autonomous run's verify FAILS (E1).
# The repair machinery existed (freeform/full_pipeline) but the chat loop only
# ever reported failure. SHAMSU_AUTO_REPAIR=0 restores report-only.
_AUTO_REPAIR_ENABLED = _os.environ.get("SHAMSU_AUTO_REPAIR", "1").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
}

# Whether the planner may stop a run to ask the user a decision before work
# starts (J6). On by default: a wrong build costs far more than one question.
# SHAMSU_ASK_UPFRONT=0 restores straight-to-work behavior.
_ASK_UPFRONT_ENABLED = _os.environ.get("SHAMSU_ASK_UPFRONT", "1").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
}

_CHAT_PLANNER_ENABLED = _os.environ.get("SHAMSU_CHAT_PLANNER", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
# Honesty gate: after an AUTONOMOUS (long_running) run that wrote files, run a
# deterministic lightweight verifier once so the loop never signs off on a build
# it never checked (small models routinely hallucinate success). Off for normal
# interactive chat (the user is in the loop); disable entirely with
# SHAMSU_VERIFY_GATE=0.
_VERIFY_GATE_ENABLED = _os.environ.get("SHAMSU_VERIFY_GATE", "1").strip().lower() not in {
    "0", "false", "no", "off",
}

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
- Use append_file when adding content at the end of an existing file. Do not fake append by
  passing an empty old_string to edit_file.
- Use write_file only to create a new file or fully rewrite one, passing the COMPLETE content.
- If the user asks you to create, write, save, generate, add, edit, or update a file,
  your next action must be an edit_file/append_file/write_file tool call or a clarification question.
- A file change only counts if the edit_file/append_file/write_file tool result says ok. If a tool result
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

## Clarification rules
- Call ask_user whenever a decision belongs to the user, not just when you are stuck:
  choosing between valid approaches/designs, naming, scope, or anything destructive.
  One good question beats a confidently wrong build.
- Do not guess between multiple destructive or ambiguous choices.
- Use read-only tools (find_file, grep_files, list_files, read_file) to gather missing FACTS
  before asking - but never "research your way past" a judgment call that is the user's.
- For multiple file candidates, call ask_user with the candidates as options so the user can choose.
- Ask for a commit message, branch/remote, or a specific target when those are required and ambiguous.
- Example: task says "add auth" and nothing specifies sessions vs JWT -> ask_user with those
  two options. Example: two config files could be the target -> ask_user listing both.

## Visible process rules
- Do not expose hidden chain-of-thought.
- Do show concise action summaries: what tool you used, its result, and what remains.
- Never say "I will use/read/run..." without making the corresponding tool call in the same turn.
- If blocked, say exactly what input is needed, or call ask_user.
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
# How many times we correct a prose-only "I will read X next" reply that did not
# actually call a tool, before giving up (a backstop against a chatty model).
_MAX_PROSE_CORRECTIONS = 2
# How many empty model replies (no content, no tool call) to nudge past before
# giving up with a clear message instead of a blank "No response returned".
_MAX_EMPTY_RESPONSES = 2

_EMPTY_RESPONSE_CORRECTION = (
    "You returned an empty response. Do not return nothing. Either call a tool to make "
    "progress, or write the answer/code directly, or state plainly what input you need."
)

# Phrases that signal the assistant *promised* a tool action but did not call
# one. Used only when there are no tool calls in the reply.
_DEFERRED_ACTION_PATTERNS = (
    r"\bi('?ll| will| am going to| am gonna| shall)\b.*\b(read|open|write|edit|run|check|look|search|fix|create|update|inspect|try)\b",
    r"\blet me\b.*\b(read|open|write|edit|run|check|look|search|fix|create|update|inspect|try)\b",
    r"\bnext(,| i)\b.*\b(read|open|write|edit|run|check|look|search|fix|will)\b",
    r"\bi('?ll| will)\b\s+(correct|retry|redo|do that|handle)\b",
)

_MUTATION_TOOL_NAMES = frozenset(
    {"write_file", "edit_file", "append_file", "move_file", "delete_file"}
)

_WORKSPACE_CHANGE_RE = re.compile(
    r"\b(create|write|build|implement|fix|edit|update|modify|change|add|remove|delete|rename|move|make)\b",
    re.IGNORECASE,
)
_INFORMATION_REQUEST_RE = re.compile(
    r"^\s*(how|what|why|where|when|who|which|can you explain|could you explain|"
    r"would you explain|tell me|show me)\b",
    re.IGNORECASE,
)
_FALSE_FAILURE_RE = re.compile(
    r"\b(could not|couldn't|unable to|failed to|was not able|wasn't able)\s+"
    r"(apply|edit|write|create|update|change|modify|save|fix|complete)\b",
    re.IGNORECASE,
)

# Callback used to surface structured trace events (route/plan/blockers/etc.)
# to the REPL. None keeps the loop silent (tests, non-interactive callers).
TraceCallback = Callable[[str, str, "dict[str, Any] | None", str], None]


# Timeout / stall categories, so the CLI and logs can distinguish a genuine LLM
# timeout from an agent-loop stall (the model already answered but no valid tool
# call followed) instead of always blaming the GPU. See run().
TIMEOUT_LLM_NO_FIRST_TOKEN = "llm_no_first_token_timeout"
TIMEOUT_LLM_GENERATION = "llm_generation_timeout"
TIMEOUT_PLANNER_STALL = "planner_returned_but_executor_stalled"
TIMEOUT_TOOL_EXECUTION = "tool_execution_timeout"
TIMEOUT_TOOL_MISSING_AFTER_PROMISE = "tool_call_missing_after_promise"


@dataclass(frozen=True)
class AgentLoopResult:
    final: str
    tool_rounds: int = 0
    stopped: bool = False
    awaiting_user: bool = False
    timeout_category: str | None = None
    # Workspace-relative files this run confirmed writing, so a caller (e.g. the
    # plan runner) can verify the whole set once at the end.
    changed_files: tuple[str, ...] = ()


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
        on_activity: Callable[[str], None] | None = None,
        on_trace: TraceCallback | None = None,
        progress: ProgressReporter | None = None,
        action_ledger: ActionLedger | None = None,
        llm: ILLMManager | None = None,
        context_builder: IContextBuilder | None = None,
        budget_manager: ContextBudgetManager | None = None,
        audit: SessionAuditLog | None = None,
        read_only: bool = False,
        use_long_term_memory: bool = True,
        use_planner: bool = True,
    ) -> None:
        _validate_local_llm_url(base_url)
        self.workspace_root = Path(workspace_root).resolve()
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self.model_name = model_name or model_for_role(_CHAT_EXECUTOR_ROLE)
        # Capability flags drive the model I/O boundary: whether to hand this
        # model a native tools schema (vs. an in-prompt protocol + salvager) and
        # whether to ask it to `think` so reasoning stays out of the answer.
        self._supports_native_tools = model_supports_native_tools(self.model_name)
        self._is_reasoning = model_is_reasoning(self.model_name)
        self.llm = llm or LLMManager(session_logger=session_logger, action_ledger=action_ledger)
        self.context_builder = context_builder or ContextBuilder()
        self.client = client or ollama.AsyncClient(host=base_url)
        self.tools = tools or AgentToolRegistry(self.workspace_root, session_logger=session_logger)
        # Names the salvager is allowed to recover calls for (a JSON blob naming
        # an unregistered "tool" is treated as prose, not a call).
        self._registered_tool_names = {
            str((schema.get("function") or {}).get("name") or "")
            for schema in self.tools.tool_schemas()
        }
        # Optional hook to surface live tool activity (e.g. "Writing game.js")
        # to the REPL while the loop runs. None keeps the loop silent (tests).
        self.on_activity = on_activity
        self.on_trace = on_trace
        self.progress = progress
        self.budget_manager = budget_manager
        self.audit = audit
        self.state = state or ChatState(
            _system_prompt(
                self.workspace_root,
                include_tool_protocol=not self._supports_native_tools,
            ),
            session_logger=session_logger,
        )
        self.long_running = long_running
        self.max_tool_rounds = LONG_RUNNING_MAX_TOOL_ROUNDS if long_running else max_tool_rounds
        # The user explicitly forbade file changes ("do not modify files").
        # Propagated to the tool registry, which denies mutating tools outright
        # regardless of approval mode - an instruction, not a preference.
        self.read_only = read_only
        self.use_long_term_memory = use_long_term_memory
        self.use_planner = use_planner
        if read_only:
            self.tools.set_read_only(True)
        self.markdown_fallback = MarkdownWriteFallback(self.tools)

    async def _messages_within_budget(self, num_ctx: int) -> list[dict[str, Any]]:
        """Budget-aware replacement for the flat 30-message cap: keep the system
        prompt plus the largest recent suffix of the conversation that fits the
        model's window, folding older evicted turns into a compact rolling
        summary instead of silently dropping them."""
        reserve = RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
        usable = max(1, num_ctx - reserve)
        history_budget = max(1, usable - _CHAT_SUMMARY_BUDGET_TOKENS)
        tail, start_abs = self.state.select_for_budget(history_budget, count_tokens)
        if start_abs > 1:
            pending = self.state.newly_evicted(start_abs)
            if pending:
                summary = await self._summarize_evicted(
                    self.state.rolling_summary, pending, _CHAT_SUMMARY_BUDGET_TOKENS
                )
                self.state.update_rolling_summary(summary, start_abs)
        return self.state.build_ollama_messages(tail, include_summary=start_abs > 1)

    async def _summarize_evicted(
        self, prior_summary: str, evicted: list[Any], budget_tokens: int
    ) -> str:
        """Fold newly-dropped turns into a compact rolling summary so long chats
        compress instead of forgetting. Best-effort: returns *prior_summary* on
        any failure so the loop never breaks."""
        transcript = "\n".join(
            f"{message.role}: {message.content}" for message in evicted if str(message.content).strip()
        )
        if not transcript.strip():
            return prior_summary
        approx_words = max(40, budget_tokens // 2)
        system = (
            "You maintain a running summary of a coding-assistant conversation. "
            f"Rewrite it in under ~{approx_words} words, preserving decisions, file "
            "paths, unresolved tasks, and key facts; drop small talk. Output only the summary."
        )
        user = ""
        if prior_summary.strip():
            user += f"Summary so far:\n{prior_summary}\n\n"
        user += f"New conversation to fold in:\n{transcript}"
        try:
            response = await asyncio.wait_for(
                self.client.chat(
                    model=self.model_name,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    stream=False,
                    options={
                        "temperature": 0.1,
                        "num_ctx": min(ctx_window_for_model(self.model_name), _CHAT_MAX_CTX),
                    },
                ),
                timeout=_MODEL_CALL_TIMEOUT_SECONDS,
            )
        except Exception:
            return prior_summary
        message = _message_from_response(response)
        return str(_get(message, "content", "") or "").strip() or prior_summary

    async def _chat_with_heartbeat(
        self,
        messages: list[dict[str, Any]],
        num_ctx: int,
        round_index: int = 0,
    ) -> Any:
        """Call the model, emitting a periodic 'still waiting' heartbeat so a slow
        local model reads as working, not frozen. The timeout is unchanged."""

        async def _beat() -> None:
            elapsed = 0
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
                elapsed += _HEARTBEAT_INTERVAL_SECONDS
                if self.on_activity:
                    self.on_activity(f"still waiting for the model... {elapsed}s")

        chat_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            "options": {"temperature": 0.1, "num_ctx": num_ctx},
        }
        # Only hand a native tools schema to models that actually do native
        # tool-calling; for the rest the in-prompt protocol + output salvager
        # carry it (a schema confuses those models). Ask reasoning models to
        # think so the trace separates cleanly from the answer.
        if self._supports_native_tools:
            chat_kwargs["tools"] = self.tools.tool_schemas()
        if self._is_reasoning:
            chat_kwargs["think"] = True

        serialized_prompt = json.dumps(messages, ensure_ascii=True, default=str)
        ledger_call_id = ""
        if self.action_ledger:
            ledger_call_id = self.action_ledger.log_model_call_started(
                "agent-executor",
                self.model_name,
                serialized_prompt,
            )
            self.action_ledger.log_context_preview(
                {
                    "task_id": "agent-chat",
                    "step_id": round_index + 1,
                    "specialist": "agent-executor",
                    "token_estimate": count_tokens(serialized_prompt),
                    "messages": _compact_value(messages, limit=24000),
                    "snippets": [],
                    "omitted_context": {},
                },
                model_call_id=ledger_call_id,
            )

        beat = asyncio.ensure_future(_beat())
        try:
            try:
                coro = self.client.chat(**chat_kwargs)
            except TypeError:
                # Older ollama clients (or test doubles) may not accept `think`.
                chat_kwargs.pop("think", None)
                coro = self.client.chat(**chat_kwargs)
            response = await asyncio.wait_for(coro, timeout=_MODEL_CALL_TIMEOUT_SECONDS)
            if self.action_ledger:
                message = _message_from_response(response)
                visible = str(_get(message, "content", "") or "")
                tool_calls = _get(message, "tool_calls", []) or []
                response_preview = visible or json.dumps(
                    _compact_value(tool_calls, limit=6000),
                    ensure_ascii=True,
                    default=str,
                )
                self.action_ledger.log_model_call_finished(
                    "agent-executor",
                    self.model_name,
                    response_preview,
                    call_id=ledger_call_id,
                    meta={"round": round_index, "tool_call_count": len(tool_calls)},
                )
            return response
        except Exception as exc:
            if self.action_ledger:
                self.action_ledger.log_model_call_finished(
                    "agent-executor",
                    self.model_name,
                    call_id=ledger_call_id,
                    error=f"{type(exc).__name__}: {exc}",
                    meta={"round": round_index},
                )
            raise
        finally:
            beat.cancel()

    def _audit_final(self, final: str) -> None:
        if self.audit:
            self.audit.log_final(final)

    def _audit_file_change(self, name: str, arguments: dict[str, Any], result: Any) -> None:
        """Record local text-file changes (content + diff + rollback id)."""
        if (
            not self.audit
            or name not in {"write_file", "edit_file", "append_file"}
            or not getattr(result, "ok", False)
        ):
            return
        data = result.data if isinstance(result.data, dict) else {}
        filepath = str(arguments.get("filepath", ""))
        if name == "write_file":
            content = str(arguments.get("content") or "")
            diff = "\n".join(f"+{line}" for line in content.splitlines())
            action = "create" if data.get("created") else "overwrite"
        elif name == "append_file":
            content = str(arguments.get("content") or "")
            diff = "\n".join(f"+{line}" for line in content.splitlines())
            action = "append"
        else:  # edit_file
            old = str(arguments.get("old_string") or "")
            new = str(arguments.get("new_string") or "")
            content = new
            diff = "\n".join(
                difflib.unified_diff(
                    old.splitlines(), new.splitlines(), fromfile=filepath, tofile=filepath, lineterm=""
                )
            )
            action = "edit"
        self.audit.log_file_change(
            action=action,
            filepath=filepath,
            content=content,
            diff=diff,
            transaction_id=str(data.get("transaction_id", "")),
        )

    async def run(self, user_input: str) -> AgentLoopResult:
        original_input = user_input
        if self.audit:
            self.audit.log_prompt(original_input)
        if self.use_long_term_memory:
            user_input = self._append_long_term_memory(user_input)
        self._produced_plan = False
        self._pending_upfront_question: dict[str, Any] | None = None
        if self.use_planner and (self.long_running or _CHAT_PLANNER_ENABLED):
            user_input = await self._append_plan(user_input)
        self.state.append_user(user_input)
        # The planner judged this needs a decision only the user can make. Ask
        # BEFORE doing any work (J6): mid-loop, a model that can always do
        # *something* just does it, which is why the prompt-only nudge toward
        # ask_user measurably failed on design decisions. Asking first costs no
        # extra model call - the planner call already happened above.
        if self._pending_upfront_question and _ASK_UPFRONT_ENABLED:
            question = self._pending_upfront_question
            self._pending_upfront_question = None
            return self._handle_ask_user(question, original_input, 0)
        repeated_calls: Counter[tuple[str, str]] = Counter()
        unconfirmed_failed_writes: dict[str, str] = {}
        mutation_recovery_attempts = 0
        # Files this run actually wrote (confirmed ok), for the end-of-run verify gate.
        written_files: list[str] = []
        # The most recent read_file failure that has not yet been recovered from,
        # plus a cap on prose-only "I'll read X next" stalls after such a failure.
        last_failed_read: dict[str, Any] | None = None
        read_recovery_attempts = 0
        prose_corrections = 0
        empty_responses = 0
        # Whether any tool has actually executed yet - used to tell a genuine LLM
        # timeout apart from an executor stall when a model call times out.
        ran_any_tool = False
        successful_mutation = False
        # A non-mutating tool (run_command/read_file/...) that actually
        # succeeded. Such a turn has already done its work through tools, so a
        # fenced block in the reply is the RESULT, not a file to write - see the
        # markdown-fallback guard below.
        nonwrite_tool_succeeded = False
        for round_index in range(self.max_tool_rounds):
            num_ctx = min(ctx_window_for_model(self.model_name), _CHAT_MAX_CTX)
            messages = await self._messages_within_budget(num_ctx)
            # Show context-window usage before each model call.
            if self.budget_manager:
                _msg_text = "\n".join(str(m.get("content", "")) for m in messages)
                _budget = self.budget_manager.compute(self.model_name, "chat", _msg_text)
                self.budget_manager.show_indicator(_budget)
            # Surface what context is going to the model (verbose/raw trace).
            if self.on_trace is not None:
                approx_chars = sum(len(str(m.get("content", ""))) for m in messages)
                self._emit_trace(
                    "context.sent",
                    f"Sending {len(messages)} messages (~{approx_chars // 4} tokens) to {self.model_name}",
                    {"messages": len(messages), "model": self.model_name, "round": round_index},
                    level="verbose",
                )
            try:
                response = await self._chat_with_heartbeat(messages, num_ctx, round_index)
            except asyncio.TimeoutError:
                category = self._timeout_category(round_index, ran_any_tool)
                final = _timeout_message(category, _MODEL_CALL_TIMEOUT_SECONDS)
                self.state.append_assistant(final)
                self._emit_trace(
                    "llm.timeout",
                    f"Model call timed out (category: {category}).",
                    {"category": category, "round": round_index, "seconds": _MODEL_CALL_TIMEOUT_SECONDS},
                )
                if self.session_logger:
                    self.session_logger.log(
                        "llm.timeout",
                        {"category": category, "round": round_index, "produced_plan": self._produced_plan},
                        f"Model call timed out: {category}",
                        workflow_id="agent-chat",
                    )
                if self.audit:
                    self.audit.log_error(category, final)
                self._audit_final(final)
                return AgentLoopResult(
                    final=final, tool_rounds=round_index, stopped=True, timeout_category=category
                )
            except Exception as exc:
                final = _friendly_ollama_error(exc)
                self.state.append_assistant(final)
                if self.audit:
                    self.audit.log_error("llm_error", str(exc))
                self._audit_final(final)
                return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)
            # Single normalization boundary: native tool_calls when present,
            # else salvage calls out of the content (embedded JSON /
            # SEARCH-REPLACE / <tool_call>), split reasoning out, and strip any
            # leaked tool syntax from the visible answer. This is what stops raw
            # `{"name":"ask_user",...}` / diff markers from reaching the user.
            turn = parse_model_turn(response, self._registered_tool_names)
            content = turn.text
            tool_calls = [tool_call_to_message_dict(call) for call in turn.tool_calls]
            # Surface the visible (tool-syntax-stripped) message in `/trace raw`;
            # keep any reasoning trace out of the answer on a verbose channel.
            if content.strip():
                self._emit_trace("assistant.content", content, {"round": round_index}, level="raw")
            if turn.thinking:
                # Surface a short, dim reasoning glimpse at normal verbosity (the
                # reasoning was previously hidden/logged-only); keep the full
                # trace in the session log and the untruncated version at verbose.
                self._log_thinking(turn.thinking, round_index)
                self._emit_trace(
                    "assistant.thinking",
                    _thinking_preview(turn.thinking),
                    {"round": round_index, "chars": len(turn.thinking)},
                    level="normal",
                )
            if turn.salvaged and tool_calls:
                self._emit_trace(
                    "tool.salvaged",
                    f"Recovered {len(tool_calls)} tool call(s) from unstructured model output.",
                    {"round": round_index, "tools": [_tool_call_name(call) for call in tool_calls]},
                )
            self.state.append_assistant(content, tool_calls=tool_calls)
            if not tool_calls and not content.strip():
                # An empty model reply (no content, no tool call) is the "No
                # response returned" the user saw. Retry with a nudge rather than
                # ending the turn on nothing.
                if empty_responses < _MAX_EMPTY_RESPONSES:
                    empty_responses += 1
                    self.state.append_user(_EMPTY_RESPONSE_CORRECTION)
                    self._emit_trace(
                        "workflow.blocked",
                        "Model returned an empty response; asking it to act or answer.",
                        {"attempt": empty_responses},
                    )
                    continue
                final = _empty_response_final()
                self.state.append_assistant(final)
                self._audit_final(final)
                return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)
            if not tool_calls:
                fallback_prompt = content if written_files else user_input
                # The fallback exists for ONE shape: the model answered a file
                # task with prose+code instead of calling write_file, so nothing
                # happened. Once any tool has succeeded this turn, something DID
                # happen, and a fenced block is far more likely to be output or
                # illustration - the reading that cost a user their script when
                # `run_command` printed `5` and the fallback wrote it to disk.
                # Past that point the model must say it is writing a file.
                nothing_done_yet = not written_files and not nonwrite_tool_succeeded
                should_try_fallback = nothing_done_yet or _proposes_additional_file_write(content)
                fallback = (
                    self.markdown_fallback.maybe_write(
                        fallback_prompt, content, read_only=self.read_only
                    )
                    if should_try_fallback
                    else None
                )
                if fallback is not None and fallback.handled:
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
                    fallback_results = fallback.tool_results or (
                        [fallback.tool_result] if fallback.tool_result is not None else []
                    )
                    for tool_result in fallback_results:
                        if not tool_result.ok:
                            continue
                        successful_mutation = True
                        data = tool_result.data if isinstance(tool_result.data, dict) else {}
                        written = str(data.get("resolved_filepath") or data.get("filepath") or "")
                        if written and written not in written_files:
                            written_files.append(written)
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
                                original_input,
                            )
                        )
                        continue
                    # Recoveries exhausted: the user knows the right path even
                    # when the model doesn't - ask, with candidates as options.
                    failed_path = str(last_failed_read.get("filepath", "the file"))
                    candidates = [
                        str(candidate)
                        for candidate in list(last_failed_read.get("candidates", []))[:6]
                    ]
                    return self._ask_for_help_on_stall(
                        reason=f"I could not read {failed_path} after several attempts.",
                        question="Which file should I use (or what is the correct path)?",
                        original_input=user_input,
                        round_index=round_index,
                        options=[{"label": candidate, "description": ""} for candidate in candidates],
                    )
                if _looks_like_deferred_action(content):
                    # The model said it would take a tool action ("I will read X
                    # next") but did not call a tool. Do not end the turn on an
                    # empty promise: demand a real tool call, an ask_user, or an
                    # explicit "I am blocked" and give it one more round.
                    if prose_corrections < _MAX_PROSE_CORRECTIONS:
                        prose_corrections += 1
                        self.state.append_user(_PROSE_ONLY_CORRECTION)
                        self._emit_trace(
                            "workflow.blocked",
                            "Assistant promised a tool action without calling one; asking it to act or ask.",
                            {"attempt": prose_corrections, "category": "tool_call_missing_after_promise"},
                        )
                        continue
                    # Corrections exhausted: the model kept promising a tool
                    # action without calling one. A bare promise is NOT a valid
                    # final answer, so return an explicit "blocked" message
                    # instead of surfacing the hollow prose.
                    final = _prose_blocked_final()
                    self.state.append_assistant(final)
                    self._emit_trace(
                        "workflow.blocked",
                        "Assistant kept promising tool actions without calling one.",
                        {"category": "tool_call_missing_after_promise"},
                    )
                    self._audit_final(final)
                    return AgentLoopResult(
                        final=final,
                        tool_rounds=round_index,
                        stopped=True,
                        timeout_category=TIMEOUT_TOOL_MISSING_AFTER_PROMISE,
                    )
                if unconfirmed_failed_writes:
                    if mutation_recovery_attempts < 2:
                        mutation_recovery_attempts += 1
                        details = "; ".join(
                            f"{path}: {message}"
                            for path, message in unconfirmed_failed_writes.items()
                        )
                        self.state.append_user(
                            "A required file mutation is still unconfirmed: "
                            f"{details}. Your NEXT response must call read_file plus a corrected "
                            "edit_file, append_file, or write_file with the complete corrected file. Do not "
                            "reply with a success summary until a mutation tool returns ok."
                        )
                        continue
                    final = _failed_write_final(unconfirmed_failed_writes)
                    self.state.append_assistant(final)
                    self._audit_final(final)
                    return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)
                if _request_requires_workspace_change(original_input) and not successful_mutation:
                    final = _missing_mutation_final(content)
                    self.state.append_assistant(final)
                    self._emit_trace(
                        "workflow.failed",
                        "The request required a workspace change, but no mutation tool succeeded.",
                        {"category": "required_mutation_missing"},
                    )
                    if self.action_ledger:
                        self.action_ledger.log_event(
                            "mutation_required_but_missing",
                            model_response=content,
                        )
                    self._audit_final(final)
                    return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True)
                if successful_mutation and _model_claims_mutation_failed(content):
                    content = _mutation_evidence_final(written_files)
                final = await self._maybe_verify(content, written_files)
                self._audit_final(final)
                return AgentLoopResult(
                    final=final, tool_rounds=round_index, changed_files=tuple(written_files)
                )
            for call in tool_calls:
                name = _tool_call_name(call)
                arguments = _tool_call_arguments(call)
                signature = (name, json.dumps(arguments, sort_keys=True, default=str))
                repeated_calls[signature] += 1
                if repeated_calls[signature] >= _MAX_REPEATED_CALLS:
                    # Repeating the same call means the loop is missing a
                    # decision, not effort - ask for it instead of giving up.
                    return self._ask_for_help_on_stall(
                        reason=(
                            f"I kept repeating the same {name} call "
                            f"({summarize_tool_args(name, arguments)}) without making progress."
                        ),
                        question="What should I do differently, or which target should I use?",
                        original_input=user_input,
                        round_index=round_index,
                    )
                if self.on_activity:
                    self.on_activity(_describe_tool_call(name, arguments))
                if self.progress:
                    self.progress.tool_start(name, summarize_tool_args(name, arguments))
                self._log_tool_call(name, arguments)
                if self.audit:
                    self.audit.log_tool_call(name, arguments)
                ledger_call_id = self.action_ledger.log_tool_call(name, arguments) if self.action_ledger else ""
                result = self.tools.execute(name, arguments)
                raw_tool_json = result.to_json()
                budgeted_tool_json, tool_budget = _budget_tool_result_json_with_meta(
                    raw_tool_json,
                    _TOOL_RESULT_MAX_TOKENS,
                )
                ran_any_tool = True
                result_data = result.data if isinstance(result.data, dict) else {}
                mcp_mutation = (
                    name.startswith("mcp__")
                    and result.ok
                    and not bool(result_data.get("read_only", False))
                    and bool(result_data.get("touched_files"))
                )
                if (name in _MUTATION_TOOL_NAMES and result.ok) or mcp_mutation:
                    successful_mutation = True
                elif result.ok:
                    nonwrite_tool_succeeded = True
                self._log_tool_result(name, result)
                if self.audit:
                    self.audit.log_tool_result(
                        name, bool(result.ok), result.message, _compact_value(result.data, limit=4000)
                    )
                    self._audit_file_change(name, arguments, result)
                    if name == "run_command":
                        _data = result.data if isinstance(result.data, dict) else {}
                        self.audit.log_command(
                            str(arguments.get("command", "")),
                            int(_data.get("exit_code", 0) or 0),
                            str(_data.get("stdout", "")),
                            str(_data.get("stderr", "")),
                        )
                if self.action_ledger:
                    self.action_ledger.log_tool_result(
                        ledger_call_id,
                        name,
                        bool(result.ok),
                        result.message,
                        result.data,
                        exception_class=str(result.data.get("exception_class", "")),
                        traceback_path=str(result.data.get("traceback_path", "")),
                        original_tokens=tool_budget["original_tokens"],
                        returned_tokens=tool_budget["returned_tokens"],
                        max_tokens=tool_budget["max_tokens"],
                        truncated=tool_budget["truncated"],
                        full_result_text=raw_tool_json if tool_budget["truncated"] else "",
                    )
                if self.progress:
                    self.progress.tool_result(name, summarize_tool_result(result), ok=result.ok)
                if self.on_activity and not result.ok:
                    self.on_activity(f"failed: {result.message}")
                self.state.append_tool(
                    _tool_call_id(call, name),
                    name,
                    budgeted_tool_json,
                )
                if name.startswith("mcp__") and not result.ok:
                    self.state.append_user(
                        f"The MCP call {name} failed: {result.message} "
                        "The user already authorized the requested operation. Choose the matching "
                        "registered mcp__ tool, correct its arguments, and continue. Do not replace "
                        "it with a shell command or ask for confirmation that was already given."
                    )
                if not result.ok and "denied by user" in result.message.lower():
                    final = (
                        f"{name} was not run because approval was denied. "
                        "No action was taken."
                    )
                    self.state.append_assistant(final)
                    self._emit_trace(
                        "workflow.blocked",
                        final,
                        {"category": "approval_denied", "tool": name},
                    )
                    self._audit_final(final)
                    return AgentLoopResult(
                        final=final,
                        tool_rounds=round_index,
                        stopped=True,
                        changed_files=tuple(written_files),
                    )
                if name in {"write_file", "edit_file", "append_file"} and result.ok:
                    _data = result.data if isinstance(result.data, dict) else {}
                    written = str(
                        _data.get("resolved_filepath")
                        or _data.get("filepath")
                        or arguments.get("filepath")
                        or ""
                    )
                    if written and written not in written_files:
                        written_files.append(written)
                if mcp_mutation:
                    for written in result_data.get("touched_files", []):
                        written = str(written)
                        if written and written not in written_files:
                            written_files.append(written)
                if name == "search_index" and result.ok and isinstance(result.data, dict):
                    # Make the context feed visible (G9): the query + top hits with
                    # scores, at normal verbosity instead of behind a debug flag.
                    self._emit_trace(
                        "context.search",
                        _search_summary(str(arguments.get("query", "")), result.data.get("results", [])),
                        {"query": str(arguments.get("query", "")), "hits": len(result.data.get("results", []))},
                    )
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
                                original_input,
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
                    if bool(data.get("actionable", True)):
                        try:
                            self.session_logger.set_last_failure(
                                str(arguments.get("command", "")),
                                errors,
                                int(data.get("exit_code", 1) or 1),
                                classification=str(
                                    data.get("outcome_classification", "command_failure")
                                ),
                                operation_id=str(data.get("diagnostics_path", "")),
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
                        str(arguments.get("filepath", "")), result.message, original_input
                    )
                    self.state.append_user(correction)
                    self._emit_trace(
                        "tool.failed",
                        f"read_file {arguments.get('filepath', '')} failed: {result.message}",
                        {"filepath": str(arguments.get("filepath", ""))},
                    )
                if name in {"write_file", "append_file"}:
                    filepath = str(arguments.get("filepath", "the file"))
                    if result.ok:
                        unconfirmed_failed_writes.pop(filepath, None)
                    else:
                        unconfirmed_failed_writes[filepath] = result.message
                if name == "edit_file" and not result.ok:
                    # An edit that did not apply (ambiguous or not-found
                    # old_string) otherwise gets retried verbatim until the loop
                    # gives up with nothing changed. Steer the model to a unique
                    # match or a full rewrite instead.
                    filepath = str(arguments.get("filepath", "the file"))
                    unconfirmed_failed_writes[filepath] = result.message
                    self.state.append_user(
                        _edit_failure_correction(
                            filepath,
                            result.message,
                            result.data,
                            old_string=str(arguments.get("old_string", "")),
                            new_string=str(arguments.get("new_string", "")),
                        )
                    )
                elif name == "edit_file" and result.ok:
                    unconfirmed_failed_writes.pop(
                        str(arguments.get("filepath", "the file")), None
                    )
                if name == "append_file" and not result.ok:
                    correction = _append_failure_correction(
                        str(arguments.get("filepath", "the file")),
                        result.message,
                    )
                    self.state.append_user(correction)
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
        self._audit_final(final)
        return AgentLoopResult(
            final=final, tool_rounds=self.max_tool_rounds, stopped=True,
            changed_files=tuple(written_files),
        )

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
        Best-effort: a planner failure must never block the chat loop itself.

        Also captures the planner's upfront "this is the user's decision" verdict
        for `run()` to act on before any work starts (J6)."""
        self._pending_upfront_question = None
        try:
            plan = await create_plan(
                self.llm,
                self.context_builder,
                results=[],
                goal=user_input,
                task_id="agent-chat-plan",
                # results is always [] here, so without this the planner was
                # context-blind - grounded in nothing, free to invent files.
                workspace=self.workspace_root,
            )
        except Exception:
            return user_input
        if plan.needs_input:
            self._pending_upfront_question = {
                "question": plan.question,
                "options": list(plan.options),
                "allow_free_text": True,
                "source": "planner_upfront",
            }
        if not plan.text:
            return user_input
        self._produced_plan = True
        if self.audit:
            self.audit.log_planner(plan.text)
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
        # Surface the plan as a visible trace event (never the hidden reasoning
        # behind it - just the short, action-focused plan text).
        self._emit_trace("plan.created", plan.text)
        return f"{user_input}\n\nPlan from planner model:\n{plan.text}"

    async def _maybe_verify(self, content: str, written_files: list[str]) -> str:
        """After writes, run a deterministic lightweight verifier once.

        Never claims success it did not check: a build failure turns into a loud
        'UNCONFIRMED' note (the model may have claimed success), a pass adds a
        short confirmation, and an unverifiable change is left untouched. Safe
        lightweight checks run interactively; automatic repair is autonomous-only.
        A verifier error never breaks the turn."""
        if not _VERIFY_GATE_ENABLED or not written_files:
            return content
        try:
            outcome = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: verify_only(
                    self.workspace_root,
                    list(written_files),
                    command_runner=self.tools.command_runner,
                    lightweight=True,
                    session_logger=self.session_logger,
                ),
            )
        except Exception:
            return content
        self._emit_trace(
            "verify.result",
            outcome.summary,
            {"status": outcome.status(), "command": outcome.command, "files": len(written_files)},
        )
        if self.session_logger:
            try:
                self.session_logger.log(
                    "verify.result",
                    {
                        "status": outcome.status(),
                        "command": outcome.command,
                        "exit_code": outcome.exit_code,
                        "files": list(written_files),
                    },
                    f"Verify gate: {outcome.status()}",
                    workflow_id="agent-chat",
                )
            except Exception:
                pass
        if self.action_ledger:
            verifier_id = self.action_ledger.verifier_id_for(outcome.command, "agent_chat_verify")
            if outcome.unverifiable:
                self.action_ledger.log_verification_unavailable(
                    outcome.summary,
                    command=outcome.command,
                    verifier_id=verifier_id,
                    source="agent_chat_verify",
                    required=True,
                    files=list(written_files),
                )
            else:
                self.action_ledger.log_verification_result(
                    outcome.verified,
                    outcome.summary,
                    command=outcome.command,
                    verifier_id=verifier_id,
                    source="agent_chat_verify",
                    required=True,
                    files=list(written_files),
                    exit_code=outcome.exit_code,
                )
        if outcome.unverifiable:
            return content
        if outcome.failed:
            # One bounded repair before giving up (gap E1). The machinery to fix
            # a one-line syntax error after a 30-minute run existed all along
            # (RepairLoop, used by freeform/full_pipeline) and simply was never
            # invited here - the loop verified, reported failure, and left the
            # user to start over. Best-effort and capped: a repair error or a
            # still-failing repair falls through to the honest UNCONFIRMED note.
            repaired = await self._attempt_repair(written_files) if self.long_running else None
            if repaired is not None and repaired.verified:
                self._emit_trace(
                    "verify.result",
                    f"repaired: {repaired.summary}",
                    {"status": "verified_after_repair", "command": repaired.command},
                )
                return (
                    f"{content}\n\n[verified after repair] The first check failed "
                    f"({outcome.summary}), so I ran one repair pass; it now passes: {repaired.summary}"
                ).strip()
            return (
                f"{content}\n\n⚠ I could not confirm these changes: {outcome.summary} "
                + (
                    "A repair attempt did not fix it. "
                    if repaired is not None
                    else ""
                )
                + "Treat this as UNCONFIRMED and re-check the affected file(s) before relying on it."
            ).strip()
        return f"{content}\n\n[verified] {outcome.summary}".strip()

    async def _attempt_repair(self, written_files: list[str]):
        """Run one strict repair pass over the files this run wrote.

        Returns the repair's VerifyOutcome, or None when repair is disabled,
        unavailable (no schema-capable LLM), or errored - the caller then keeps
        the plain UNCONFIRMED behavior. The verifier stays lightweight so a
        mid-chat repair can never be the thing that runs pip/npm installs."""
        if not _AUTO_REPAIR_ENABLED:
            return None
        generate_async = getattr(self.llm, "generate_structured", None)
        if not callable(generate_async):
            return None
        session_logger = self.session_logger

        def _generate_sync(system: str, user: str, schema: dict) -> str:
            # Fresh manager + asyncio.run: this runs inside a worker thread with
            # no event loop, the same shape as repl._pipeline_generate.
            from shamsu.llm.manager import LLMManager

            return asyncio.run(
                LLMManager(session_logger=session_logger).generate_structured(
                    "coder", system, user, schema
                )
            )

        try:
            from shamsu.verify.gate import verify_and_repair

            return await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: verify_and_repair(
                    self.workspace_root,
                    list(written_files),
                    generate=_generate_sync,
                    command_runner=self.tools.command_runner,
                    max_attempts=1,
                    lightweight=True,
                    session_logger=session_logger,
                ),
            )
        except Exception as exc:
            if session_logger:
                try:
                    session_logger.log(
                        "verify.repair_error",
                        {"error": str(exc)},
                        "Auto-repair attempt errored; keeping UNCONFIRMED verdict",
                        workflow_id="agent-chat",
                    )
                except Exception:
                    pass
            return None

    def _timeout_category(self, round_index: int, ran_any_tool: bool) -> str:
        """Classify a model-call timeout so the CLI/logs stop blaming the GPU
        when the model already produced a plan or the loop already ran tools.

        - First model call, no plan, no tools  -> genuine no-first-token stall.
        - First model call but a plan returned  -> planner worked, executor stalled.
        - Any later round (tools already ran)   -> mid-run generation timeout.
        """
        if round_index == 0 and not ran_any_tool:
            if getattr(self, "_produced_plan", False):
                return TIMEOUT_PLANNER_STALL
            return TIMEOUT_LLM_NO_FIRST_TOKEN
        return TIMEOUT_LLM_GENERATION

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

    def _log_thinking(self, thinking: str, round_index: int) -> None:
        """Persist the full reasoning trace as a run artifact (the visible trace
        only shows a short glimpse).

        The untruncated text goes to the run's `cot/` folder via the
        ActionLedger; the session event keeps metadata plus the path, so raw
        reasoning stays out of the session timeline. Previously this clipped at
        4000 chars, which lost precisely the long traces worth reading.
        Best-effort; never breaks the loop."""
        cot_path = ""
        if self.action_ledger:
            try:
                cot_path = self.action_ledger.log_model_thinking(
                    "", "agent-chat", self.model_name, thinking
                )
            except Exception:
                pass
        if not self.session_logger:
            return
        try:
            self.session_logger.log(
                "llm.thinking",
                {
                    "model": self.model_name,
                    "round": round_index,
                    "thinking_chars": len(thinking or ""),
                    "cot_path": cot_path,
                },
                "Model reasoning trace",
                workflow_id="agent-chat",
            )
        except Exception:
            pass

    def _ask_for_help_on_stall(
        self,
        reason: str,
        question: str,
        original_input: str,
        round_index: int,
        options: list[dict[str, str]] | None = None,
    ) -> AgentLoopResult:
        """A stall guard tripped: ask the user for the missing input instead of
        just giving up (gap J2 - `safety/clarify.py` was built for exactly this
        and never wired; the loop always ended with a dead-end message).

        Routes through the same pending-question flow as the model's own
        ask_user calls, so the question survives across turns and the user's
        next reply resumes the work - no blocking input() (fragile on Windows,
        gap G1). Only the guards where the USER plausibly holds the answer end
        here (repetition -> a decision; failed reads -> the right path); model
        pathologies (empty replies, prose-only promises) still stop plainly,
        because no user answer can fix those."""
        pending = {
            "question": f"{reason} {question}".strip(),
            "options": list(options or []),
            "allow_free_text": True,
            "source": "stall_guard",
        }
        self._emit_trace(
            "workflow.blocked",
            f"Stall guard asked the user for help: {reason}",
            {"category": "stall_guard_ask"},
        )
        if self.session_logger:
            try:
                self.session_logger.log(
                    "agent.stuck",
                    {"reason": reason, "asked": True},
                    "Stalled; asked the user for input",
                    workflow_id="agent-chat",
                )
            except Exception:
                pass
        return self._handle_ask_user(pending, original_input, round_index)

    def _handle_ask_user(
        self, pending: dict[str, Any], original_input: str, round_index: int
    ) -> AgentLoopResult:
        pending = dict(pending or {})
        pending.setdefault("awaiting", "user_input")
        pending["created_from_prompt"] = original_input
        if self.session_logger:
            self.session_logger.set_pending_question(pending)
        if self.action_ledger:
            self.action_ledger.log_event(
                "run_needs_input",
                question=str(pending.get("question", "")),
                option_count=len(pending.get("options", [])),
            )
        final = format_question(pending)
        self.state.append_assistant(final)
        self._audit_final(final)
        self._emit_trace(
            "clarification.needed",
            str(pending.get("question", "")),
            {"options": [str(option.get("label", "")) for option in pending.get("options", [])]},
        )
        return AgentLoopResult(final=final, tool_rounds=round_index, stopped=True, awaiting_user=True)

    def _read_failure_correction(
        self, filepath: str, message: str, user_request: str = ""
    ) -> str:
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
        if not candidates and _request_explicitly_creates_path(user_request, filepath):
            return (
                f"read_file {filepath} confirmed that the requested new file does not exist. "
                f"The user explicitly asked to create {filepath}; call write_file now with its complete "
                "implementation. Do not ask whether to create it and do not modify another file."
            )
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
        # No candidate matched. Sending a small model hunting ("use find_file")
        # is one more hop it fumbles - and the light tier was observed ECHOING
        # this correction back as its final answer instead of following it.
        # Name the files that actually exist so the productive next call is the
        # easiest continuation.
        try:
            from shamsu.agents.plan_mode import workspace_source_files

            existing = workspace_source_files(self.workspace_root, limit=10)
        except Exception:
            existing = []
        if existing:
            return (
                f"read_file {filepath} failed ({message}) - that file does not exist. "
                f"Files that DO exist: {'; '.join(existing)}. The file was NOT read. "
                "Continue the user's task using one of these real paths, or call ask_user if none fits."
            )
        return (
            f"read_file {filepath} failed ({message}) and no candidate path matched. Use find_file or "
            "grep_files to locate the right file, or call ask_user for the correct path. Do not claim you read it."
        )

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


def _timeout_message(category: str, seconds: int) -> str:
    """Human-facing timeout message that names the real category instead of
    always attributing the stall to a saturated GPU."""
    base = f"The model call timed out after {seconds}s (category: {category})."
    if category == TIMEOUT_PLANNER_STALL:
        return (
            f"{base} The planner already returned a plan, so this is an agent-loop / executor "
            "stall waiting on the next model response - not necessarily a GPU problem. "
            "Retry, reduce context, or try `/models tier light`."
        )
    if category == TIMEOUT_LLM_GENERATION:
        return (
            f"{base} Generation stalled part-way through the run. "
            "Try a lighter model with `/models tier light`, or reduce context."
        )
    # TIMEOUT_LLM_NO_FIRST_TOKEN
    return (
        f"{base} The first model response never arrived. Local inference may be stalled "
        "(GPU saturated, model swapping, or the context is too large). "
        "Try `/models tier light`, reduce context, or restart Ollama."
    )


def _empty_response_final() -> str:
    return (
        "The local model returned an empty response, even after being asked to act or answer. "
        "This usually means the model is too small/loaded for this request or its context is "
        "too large. Try `/models tier light` or a shorter, more specific prompt."
    )


def _prose_blocked_final() -> str:
    """Final message when the model kept promising a tool action but never called
    one. Never surface the empty promise itself as the answer."""
    return (
        "I said I would take an action (read/open/edit/run something) but did not actually "
        "call a tool to do it, and could not after retrying. This is a tool-contract stall, "
        "not a finished task. Tell me the exact file path or command to use and I'll run it."
    )


def _request_requires_workspace_change(prompt: str) -> bool:
    # A prompt that forbids file changes cannot be failed for not changing
    # files. Without this, "Do not modify files" matched _WORKSPACE_CHANGE_RE on
    # its own `modify` and a correct, complete web answer was reported as
    # "I did not complete the requested workspace change".
    if read_only.applies(prompt):
        return False
    text = " ".join(read_only.strip(prompt or "").strip().split())
    if not text or _INFORMATION_REQUEST_RE.search(text):
        return False
    return bool(_WORKSPACE_CHANGE_RE.search(text))


def _missing_mutation_final(model_response: str) -> str:
    detail = " ".join((model_response or "").strip().split())
    suffix = f" The model's response was: {detail[:300]}" if detail else ""
    return (
        "I did not complete the requested workspace change because no file mutation "
        f"succeeded. No file was changed.{suffix}"
    )


def _model_claims_mutation_failed(content: str) -> bool:
    return bool(_FALSE_FAILURE_RE.search(content or ""))


def _mutation_evidence_final(written_files: list[str]) -> str:
    targets = ", ".join(written_files)
    if targets:
        return (
            f"The file mutation succeeded on disk for: {targets}. The model's follow-up "
            "claimed it failed, but the tool result confirms the change was applied."
        )
    return (
        "The workspace mutation succeeded. The model's follow-up claimed it failed, "
        "but the tool result confirms the change was applied."
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


def _append_failure_correction(filepath: str, message: str) -> str:
    return (
        f"Your append_file call for {filepath} did NOT succeed: {message}. The file was NOT "
        "changed. If the path is wrong, locate and read the existing file, then call append_file "
        "with the corrected path. If the file does not exist, call write_file with its complete "
        "initial content."
    )


def _edit_failure_correction(
    filepath: str,
    message: str,
    data: dict[str, Any] | None = None,
    *,
    old_string: str = "",
    new_string: str = "",
) -> str:
    """Steer the model past the two common edit_file failures.

    Without this, an ambiguous or not-found edit just gets retried with the same
    arguments and the loop gives up with nothing changed - live, a 7B model gave
    `old_string="return a + b"` to fix ONE of two identical lines, edit_file
    correctly refused as ambiguous, and the fix silently never landed. The
    recovery is deterministic: make the match unique, or rewrite the whole file.
    """
    lowered = message.lower()
    if not old_string and new_string:
        how = (
            "An empty old_string is not a valid replacement anchor. Since you are adding content "
            "to the end of an existing file, call append_file with filepath and content=new_string. "
            "Do not retry edit_file with an empty old_string."
        )
    elif old_string == new_string and old_string:
        how = (
            "old_string and new_string are identical, so this cannot fix anything. "
            "Call read_file, then use distinct exact strings; if quoting is difficult, use "
            "write_file with the complete corrected file instead."
        )
    elif "appears" in lowered and "time" in lowered:
        candidates = list((data or {}).get("candidate_contexts") or [])
        exact_blocks = ""
        if candidates:
            rendered = [
                f"lines {item.get('line_start')}-{item.get('line_end')}:\n{item.get('text', '')}"
                for item in candidates[:3]
            ]
            exact_blocks = " Exact candidate blocks from the file:\n" + "\n---\n".join(rendered)
        how = (
            "That old_string is not unique. Include enough SURROUNDING lines to match "
            "exactly one place - e.g. the enclosing `def`/function line and the line above "
            "or below the change - or, if every occurrence should change, set replace_all=true."
            + exact_blocks
        )
    elif "not found" in lowered or "no match" in lowered or "does not appear" in lowered:
        how = (
            "That old_string was not found verbatim. Call read_file on the file first and "
            "copy the EXACT current text (whitespace included) into old_string, or use "
            "write_file with the entire corrected file."
        )
    else:
        how = (
            "Fix the arguments and try again, or use write_file with the entire corrected file."
        )
    return (
        f"Your edit_file on {filepath} did NOT apply: {message}. The file was NOT changed. "
        f"Do not repeat the same call and do not claim success. {how}"
    )


def _basename(filepath: str) -> str:
    return filepath.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or filepath


def _read_failure_correction(
    filepath: str,
    message: str,
    candidates: list[str],
    user_request: str = "",
) -> str:
    if not candidates and _request_explicitly_creates_path(user_request, filepath):
        return (
            f'The read_file call confirmed that "{filepath}" does not exist. The user explicitly '
            f"asked to create {filepath}; your NEXT response MUST call write_file for that exact path "
            "with the complete implementation. Do not search for a replacement and do not ask whether to create it."
        )
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


def _request_explicitly_creates_path(user_request: str, filepath: str) -> bool:
    normalized_path = filepath.replace("\\", "/").lower().strip()
    basename = _basename(normalized_path).lower()
    request = user_request.lower()
    if not normalized_path or not request or (normalized_path not in request and basename not in request):
        return False
    return bool(
        re.search(
            r"\b(create|build|implement|generate|write|add|make)\b",
            request,
            re.IGNORECASE,
        )
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


def _looks_like_read_stall(content: str) -> bool:
    lowered = (content or "").lower()
    return any(phrase in lowered for phrase in _READ_STALL_PHRASES)


def _failed_write_final(failed_writes: dict[str, str]) -> str:
    details = "; ".join(f"{path}: {message}" for path, message in failed_writes.items())
    return (
        "I could not confirm the file edit. The latest mutation attempt failed, so the "
        f"file was not edited successfully. Details: {details}"
    )


def _describe_tool_call(name: str, arguments: dict[str, Any]) -> str:
    """A short human-readable label for a tool call, for live REPL activity."""
    if name == "write_file":
        return f"Writing {arguments.get('filepath', '?')}"
    if name == "append_file":
        return f"Appending to {arguments.get('filepath', '?')}"
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


# Injected for models that don't do native tool-calling (deepseek-r1/gemma3):
# the salvager is the primary parser, so the model needs to know the exact JSON
# shape to emit. Kept out of the prompt for tool-capable models, which get the
# native tools schema instead.
_TOOL_PROTOCOL_PROMPT = """
## Tool protocol
This model does not use native tool-calls. To use a tool, emit ONE JSON object
and nothing else in that reply:
{"name": "<tool>", "arguments": { ... }}
Examples:
{"name": "read_file", "arguments": {"filepath": "src/app.py"}}
{"name": "run_command", "arguments": {"command": "pytest -q"}}
{"name": "ask_user", "arguments": {"question": "Which file did you mean?"}}
Use the exact argument names each tool documents. Emit the JSON only when you
want to run a tool; otherwise answer normally in prose. Never wrap the JSON in
extra commentary on the same turn.
"""


def _thinking_preview(thinking: str, limit: int = 200) -> str:
    """A one-line, bounded glimpse of a reasoning trace for the normal-verbosity
    Reasoning line (the full trace goes to the session log / verbose mode)."""
    one_line = " ".join((thinking or "").split())
    if len(one_line) <= limit:
        return one_line
    return one_line[:limit].rstrip() + "..."


def _search_summary(query: str, results: Any) -> str:
    """`"query" -> path (score), path (score)` for the visible context feed."""
    hits = results if isinstance(results, list) else []
    parts: list[str] = []
    for hit in hits[:5]:
        if not isinstance(hit, dict):
            continue
        path = str(hit.get("file_path") or hit.get("filepath") or "?")
        try:
            score = float(hit.get("score") or 0.0)
            parts.append(f"{path} ({score:.2f})")
        except (TypeError, ValueError):
            parts.append(path)
    listed = ", ".join(parts) if parts else "no hits"
    return f'"{query}" -> {listed}'


def _system_prompt(workspace: Path, include_tool_protocol: bool = False) -> str:
    prompt = f"{AGENT_SYSTEM_PROMPT}\nWorkspace: {workspace}\n"
    if include_tool_protocol:
        prompt += _TOOL_PROTOCOL_PROMPT
    return prompt




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


def _tool_call_id(call: dict[str, Any], fallback: str) -> str:
    return str(call.get("id") or fallback)


_ADDITIONAL_FILE_WRITE_RE = re.compile(
    r"\b(?:create|write|save|add|update|edit)\s+(?:a\s+|the\s+)?"
    r"(?:file\s+)?[`'\"]?[\w./\\-]+\.[A-Za-z0-9_]{1,12}",
    re.IGNORECASE,
)


def _proposes_additional_file_write(content: str) -> bool:
    return bool(_ADDITIONAL_FILE_WRITE_RE.search(content or ""))


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


_TOOL_RESULT_TRUNCATION_HINT = (
    " ... [tool result truncated to fit ~{budget} tokens. Re-run this tool with a "
    "narrower scope to see more: read_file with start_line/end_line, grep_files with "
    "a more specific query or an extensions filter, or a more specific path.]"
)


def _budget_tool_result_json(text: str, max_tokens: int) -> str:
    """Cap a serialized tool result to ``max_tokens`` BEFORE it enters history, so
    one oversized read/grep can't crowd the window mid-loop. Under budget passes
    through unchanged; over budget is trimmed with a hint on how to see more."""
    if max_tokens <= 0:
        return text
    total = count_tokens(text)
    if total <= max_tokens:
        return text
    hint = _TOOL_RESULT_TRUNCATION_HINT.format(budget=max_tokens)
    # Proportional first cut (chars-per-token), then shrink until it fits, leaving
    # room for the hint.
    budget_chars = max(200, len(text) * max_tokens // max(total, 1))
    truncated = text[:budget_chars]
    while truncated and count_tokens(truncated + hint) > max_tokens:
        truncated = truncated[: max(100, int(len(truncated) * 0.85))]
        if len(truncated) <= 100:
            break
    return truncated + hint


def _budget_tool_result_json_with_meta(text: str, max_tokens: int) -> tuple[str, dict[str, Any]]:
    original_tokens = count_tokens(text)
    budgeted = _budget_tool_result_json(text, max_tokens)
    returned_tokens = count_tokens(budgeted)
    return (
        budgeted,
        {
            "original_tokens": original_tokens,
            "returned_tokens": returned_tokens,
            "max_tokens": max_tokens,
            "truncated": budgeted != text,
        },
    )


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)
