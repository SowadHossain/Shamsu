"""Simple mode: Ollama chat with coding tools attached.

The default path SHAMSU should have had. One conversation, a small system
prompt, six tools, and a loop that does exactly this:

    call the model -> it asks for a tool -> run it -> put the REAL result back
    into the same conversation -> call the model again -> it answers.

No router, no planner, no execution phases, no task objects, no synthetic state
frame rebuilt per call. If the model does not need a tool, this behaves like
plain Ollama chat; if it does, the same model reads, edits, runs and continues
without changing lanes.

What the harness still does, invisibly: the path sandbox, approvals, the action
ledger, conversation persistence, and a verification pass after code changes -
whose result is appended as an ordinary tool message, so a failure reaches the
model as information it can act on rather than as a verdict panel.

Deliberately separate from :class:`~shamsu.agents.chat_loop.AgentChatLoop`.
Adding a "simple" flag to a 5,000-line class with 30 constructor parameters is
how this codebase ended up with two orchestrators in the first place.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.agents.chat_state import ChatState
from shamsu.agents.simple_log import SimpleTurnLog, next_turn_number
from shamsu.agents.simple_prompt import simple_system_prompt
from shamsu.context.budget import (
    RESERVE_OUTPUT_TOKENS,
    SAFETY_MARGIN_TOKENS,
    count_tokens,
    ctx_window_for_model,
)
from shamsu.llm.manager import OLLAMA_BASE_URL
from shamsu.llm.output import parse_model_turn
from shamsu.runtime.models import model_for_role
from shamsu.session.manager import SessionLogger
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import CommandRisk, ToolResult

# Bounded so a confused model cannot loop forever. Generous, because each round
# here is one tool call rather than one milestone.
DEFAULT_MAX_ROUNDS = 24

# Ollama reserves the KV cache for the WHOLE num_ctx up front, so requesting the
# ceiling costs the ceiling in VRAM even for a short conversation. Live
# 2026-08-17: an 8.3k prompt asked for 32768, the cache no longer fit beside the
# weights on an 8GB card, prefill spilled to system RAM and first token took 83s
# - which tripped the 90s timeout. Buckets are coarse because Ollama reloads the
# model whenever num_ctx changes.
CTX_BUCKETS = (8192, 16384, 32768, 49152, 65536)

# A single tool result must not be able to crowd out the conversation.
MAX_TOOL_RESULT_TOKENS = 2000

# Files named in the always-fresh workspace listing, and the noise excluded from
# it. Small enough to be nearly free; the point is grounding, not a project dump.
MAX_LISTED_FILES = 80
_IGNORED_DIRS = frozenset(
    {".shamsu", ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".idea"}
)

# How many times one run may tell the model "you described it, now do it".
MAX_PROSE_NUDGES = 2

_FENCE_RE = re.compile(r"```[^\n]*\n(?P<body>.*?)```", re.DOTALL)

# Messages pulled back from the session transcript per turn. Deliberately large:
# the token budget in `_messages` is the real limiter, and it can see the whole
# conversation to choose from. A small cap here is invisible and unrecoverable -
# it silently truncates history BEFORE anything gets to weigh it.
HYDRATE_MAX_MESSAGES = 400


def simple_mode_enabled() -> bool:
    """Whether the simple path is the default. Legacy routing is opt-in."""
    return not os.environ.get("SHAMSU_LEGACY_ROUTING", "").strip()


def max_ctx() -> int:
    """Ceiling for a chat call's context window.

    16384, not 32768, because this is a VRAM budget and not a capability claim.
    A 7-9B q4 model is ~5GB resident and its KV cache is ~144 KiB/token, so on
    an 8GB card 16k costs ~2.25GB (fits) and 32k costs ~4.5GB (does not). Live
    2026-08-17 the 32k bucket produced `cudaMalloc failed: out of memory` the
    moment anything else shared the GPU - and something usually does.

    Raise it with SHAMSU_CHAT_MAX_CTX on a bigger card; `_shrink_for_oom` will
    walk it back down automatically if the hardware disagrees.
    """
    raw = os.environ.get("SHAMSU_CHAT_MAX_CTX", "").strip()
    if raw.isdigit() and int(raw) >= 4096:
        return int(raw)
    return 16384


# Substrings Ollama/llama.cpp use when the GPU cannot fit what was asked for.
_OOM_MARKERS = (
    "out of memory",
    "cudamalloc failed",
    "failed to allocate",
    "failed to initialize the context",
    "unable to allocate",
)


def looks_like_out_of_memory(error: str) -> bool:
    lowered = (error or "").lower()
    return any(marker in lowered for marker in _OOM_MARKERS)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
# Six, named the way a model expects them to be named. Each maps to a method the
# registry already implements, with its sandbox and ledger intact.

SIMPLE_TOOLS: dict[str, str] = {
    "read_file": "read_file",
    "list_files": "list_files",
    "search_files": "grep_files",
    "write_file": "write_file",
    "patch_file": "edit_file",
    "run_command": "run_command",
}

SIMPLE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path relative to the workspace."}
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and folders in a workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory, relative to the workspace. Defaults to '.'."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search the workspace for text or a pattern and return matching lines.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Text or regular expression to find."},
                    "path": {"type": "string", "description": "Directory to search. Defaults to the whole workspace."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a file, or replace one completely, with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path relative to the workspace."},
                    "content": {"type": "string", "description": "The complete file content."},
                },
                "required": ["filepath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": "Replace an exact snippet in an existing file, leaving the rest untouched.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path relative to the workspace."},
                    "old_string": {"type": "string", "description": "The exact text to replace."},
                    "new_string": {"type": "string", "description": "The text to put in its place."},
                },
                "required": ["filepath", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the workspace, e.g. to run the code or its tests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to run."}
                },
                "required": ["command"],
            },
        },
    },
]

MUTATING_TOOLS = frozenset({"write_file", "patch_file"})

# Argument aliases a small model reaches for. Accepting them costs nothing and
# saves a whole failed round each time, which on a 6-round budget is expensive.
_ARG_ALIASES = {
    "path": "filepath",
    "file": "filepath",
    "file_path": "filepath",
    "filename": "filepath",
    "query": "pattern",
    "text": "content",
    "old": "old_string",
    "new": "new_string",
    "cmd": "command",
}


def normalize_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Accept the near-miss argument names small models produce.

    `list_files` genuinely takes `path`, so it is exempt from the path->filepath
    rewrite; everything else means the file it is about to touch.
    """
    normalized: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        target = _ARG_ALIASES.get(key, key)
        if tool == "list_files" and key == "path":
            target = "path"
        if tool == "search_files" and key == "path":
            target = "path"
        normalized.setdefault(target, value)
    return normalized


# --------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------
# Reads and writes inside the workspace run silently: the sandbox has already
# decided they are in-bounds, and a prompt per write is what made the old path
# feel like paperwork. Shell stays gated, minus commands that only look.

_READ_ONLY_GIT = re.compile(
    r"^git\s+(?:-C\s+\S+\s+)?"
    r"(?:status|diff|log|show|rev-parse|ls-files|describe|shortlog|blame"
    r"|branch\s+--show-current|branch\s+--list|remote\s+-v)\b",
    re.IGNORECASE,
)
_READ_ONLY_COMMANDS = re.compile(
    r"^(?:ls|dir|pwd|cat|type|head|tail|wc|find|which|where|echo|node\s+--version"
    r"|python\s+--version|npm\s+(?:ls|list|--version)|pip\s+(?:list|show))\b",
    re.IGNORECASE,
)


def command_needs_approval(command: str) -> bool:
    """Whether *command* should stop and ask. Read-only inspection should not.

    The old classifier sent `git branch --show-current` to "unknown -> MEDIUM",
    so describing a workspace raised two approval prompts before any work began.
    """
    text = (command or "").strip()
    if not text:
        return True
    if _READ_ONLY_GIT.match(text) or _READ_ONLY_COMMANDS.match(text):
        return False
    try:
        from shamsu.safety.commands import classify_command

        return classify_command(text) != CommandRisk.SAFE
    except Exception:
        return True


def make_approval_func(console_approval: Any) -> Any:
    """Approval policy for simple mode.

    Only shell commands can reach a prompt. File writes are already constrained
    to the workspace by the sandbox, which refuses an escape outright rather than
    asking about it, so a second question adds friction without adding safety.
    """

    def approve(request: Any) -> bool:
        action = str(getattr(request, "action_type", "") or getattr(request, "action", "") or "")
        if action in {"file_write", "file_edit", "file_read"}:
            return True
        command = str(getattr(request, "command", "") or "")
        if action == "run_command" or command:
            if not command_needs_approval(command):
                return True
            return bool(console_approval(request))
        return bool(console_approval(request))

    return approve


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclass
class SimpleChatResult:
    final: str = ""
    rounds: int = 0
    tool_calls: int = 0
    changed_files: tuple[str, ...] = ()
    stopped: bool = False
    error: str = ""


@dataclass
class _Round:
    tool_names: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)


class SimpleChatLoop:
    """One conversation, six tools, no ceremony."""

    def __init__(
        self,
        workspace: Path,
        *,
        client: Any,
        tools: AgentToolRegistry,
        state: ChatState | None = None,
        session_logger: SessionLogger | None = None,
        action_ledger: ActionLedger | None = None,
        model_name: str | None = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        on_activity: Any | None = None,
        on_trace: Any | None = None,
        verify_changes: bool = True,
        temperature: float = 0.2,
        request_timeout: float = 600.0,
        log_turns: bool = True,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.client = client
        self.tools = tools
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self.model_name = model_name or model_for_role("agent-chat")
        self.max_rounds = max(1, int(max_rounds))
        self.on_activity = on_activity
        self.on_trace = on_trace
        self.verify_changes = verify_changes
        self.temperature = temperature
        self.request_timeout = request_timeout
        # Off only for tests and embedders; a real session always wants the
        # transcript, since that is the only record of what the model SAW.
        self.log_turns = log_turns and not os.environ.get("SHAMSU_NO_CHAT_LOG", "").strip()
        self.state = state or ChatState(
            simple_system_prompt(self.workspace),
            session_logger=session_logger,
            # Pull far more than the 24-message default and let the TOKEN budget
            # decide what fits. A fresh loop is built per user message, so the
            # hydration cap - not the context window - was the real memory
            # horizon: ~5 messages per turn meant a 32k window remembered five
            # turns, and the agent started guessing at a project it had built.
            hydrate_max_messages=HYDRATE_MAX_MESSAGES,
        )
        self._num_ctx_floor = 0
        # Lowered from what the GPU will actually accept, once it has refused.
        self._num_ctx_ceiling = 0
        self._evicted_others = False
        # Readable per-turn transcript of prompt + raw response.
        self.turn_log: SimpleTurnLog | None = None
        self._files: list[str] = []
        self._brief = ""
        # Simple mode owns the whole toolbox: no phase policy, no per-step
        # allowlist, no logical-alias indirection between the model and the tool.
        self.tools.clear_phase()
        self.tools.set_allowed_tools(None)
        self.tools.use_logical_tools(False)

    # -- public ----------------------------------------------------------

    async def run(self, user_input: str) -> SimpleChatResult:
        self.state.append_user(user_input)
        if self.log_turns:
            try:
                self.turn_log = SimpleTurnLog(
                    self.workspace, next_turn_number(self.workspace), self.model_name
                )
                self.turn_log.open_turn(user_input)
            except OSError:
                self.turn_log = None
        # Once per user message, not per round: the graph lookup costs ~2s and
        # what a file exports does not change between rounds of the same turn.
        self._brief = await asyncio.to_thread(codebase_brief, self.workspace, user_input)
        changed: list[str] = []
        tool_calls = 0
        prose_nudges = 0
        for round_index in range(self.max_rounds):
            self._files = await asyncio.to_thread(workspace_files, self.workspace)
            try:
                response = await self._call_model()
            except (TimeoutError, asyncio.TimeoutError):
                return self._stop(
                    f"The model did not respond within {self.request_timeout:.0f}s.",
                    round_index,
                    tool_calls,
                    changed,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced verbatim, not swallowed
                if looks_like_out_of_memory(str(exc)) and self._shrink_for_oom():
                    continue
                message = f"{type(exc).__name__}: {exc}"
                if looks_like_out_of_memory(str(exc)):
                    message += (
                        "\n\nThe GPU ran out of memory even at the smallest context. "
                        "Another model is probably resident - `ollama ps` will show it, "
                        "`ollama stop <name>` frees it."
                    )
                return self._stop(message, round_index, tool_calls, changed, error=str(exc))

            turn = parse_model_turn(response, set(SIMPLE_TOOLS))
            if not turn.tool_calls:
                text = (turn.text or "").strip()
                if not text:
                    # An empty turn is not an answer. Nudge once rather than
                    # ending on nothing, which is what "No response returned."
                    # used to be.
                    self.state.append_user(
                        "That reply was empty. Answer the question, or call one tool."
                    )
                    continue
                described = describes_an_unmade_edit(text, self._files)
                if described and prose_nudges < MAX_PROSE_NUDGES:
                    # It showed the code instead of writing it. Say so once,
                    # naming the file, and let it act - the alternative is what
                    # the user saw: a perfect answer and an unchanged file.
                    prose_nudges += 1
                    self.state.append_assistant(text)
                    self.state.append_user(
                        f"You showed the new contents of {described} but did not change the file. "
                        f"Apply it now: call write_file for the complete new {described}, "
                        "or patch_file for one exact replacement. Do not repeat the code in prose."
                    )
                    self._activity(f"described a change to {described} without making it; asked it to apply")
                    continue
                self.state.append_assistant(text)
                if self.turn_log:
                    self.turn_log.close_turn(text, round_index + 1, stopped=False)
                return SimpleChatResult(
                    final=text,
                    rounds=round_index + 1,
                    tool_calls=tool_calls,
                    changed_files=tuple(dict.fromkeys(changed)),
                )

            self.state.append_assistant(
                turn.text or "",
                tool_calls=[_call_to_message(call) for call in turn.tool_calls],
            )
            outcome = await self._run_tools(turn.tool_calls)
            tool_calls += len(turn.tool_calls)
            changed.extend(outcome.written)
            if outcome.written and self.verify_changes:
                await self._append_verification(outcome.written)

        return self._stop(
            "I stopped after "
            f"{self.max_rounds} steps without finishing. Say `continue` to keep going.",
            self.max_rounds,
            tool_calls,
            changed,
        )

    # -- model -----------------------------------------------------------

    async def _call_model(self) -> Any:
        messages = self._messages()
        num_ctx = self._num_ctx(messages)
        kwargs = {
            "model": self.model_name,
            "messages": messages,
            "tools": SIMPLE_TOOL_SCHEMAS,
            "stream": False,
            "options": {"temperature": self.temperature, "num_ctx": num_ctx},
        }
        self._trace(
            "simple.model_call",
            f"{len(messages)} messages, num_ctx {num_ctx}",
            {"messages": len(messages), "num_ctx": num_ctx},
        )
        if self.turn_log:
            approx = sum(count_tokens(str(m.get("content") or "")) for m in messages)
            self.turn_log.log_call(messages, num_ctx, approx)
        started = time.perf_counter()
        try:
            try:
                raw = await asyncio.wait_for(
                    self.client.chat(**kwargs), timeout=self.request_timeout
                )
            except TypeError:
                # Older/simpler clients may not accept every keyword.
                kwargs.pop("tools", None)
                raw = await asyncio.wait_for(
                    self.client.chat(**kwargs), timeout=self.request_timeout
                )
        except Exception as exc:
            if self.turn_log:
                self.turn_log.log_error(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._activity(f"model responded in {time.perf_counter() - started:.0f}s")
        if self.turn_log:
            self.turn_log.log_response(raw, time.perf_counter() - started)
        return raw

    def _messages(self) -> list[dict[str, Any]]:
        """System prompt + workspace listing + older summary + the conversation.

        The listing is the one piece of injected context, and it earns its ~60
        tokens: it is the difference between working on the project and guessing
        at it. Everything else is the policy `_messages_within_budget` already
        uses - keep the largest recent suffix that fits, older turns survive as
        the rolling summary.
        """
        ceiling = min(ctx_window_for_model(self.model_name), max_ctx())
        usable = max(1024, ceiling - RESERVE_OUTPUT_TOKENS - SAFETY_MARGIN_TOKENS)
        tail, start_abs = self.state.select_for_budget(usable, count_tokens)
        if start_abs > 1:
            # Fold what no longer fits into a digest instead of dropping it.
            # Without this the evicted turns simply vanished and `include_summary`
            # inserted an empty string - so a long session lost its early
            # decisions entirely rather than keeping a trace of them.
            evicted = self.state.newly_evicted(start_abs)
            if evicted:
                digest = _digest(self.state.rolling_summary, evicted)
                if digest:
                    self.state.update_rolling_summary(digest, start_abs)
        messages = self.state.build_ollama_messages(tail, include_summary=start_abs > 1)
        # Placed AFTER the system prompt and before the conversation, and rebuilt
        # every call, so it can never be the stale thing the model reasons from.
        # Two halves of one question: the listing says which files exist, the
        # brief says what is inside the ones this turn is about.
        grounding = render_workspace_files(self._files)
        if self._brief:
            grounding = f"{grounding}\n\n{self._brief}"
        messages.insert(1, {"role": "system", "content": grounding})
        return messages

    def _shrink_for_oom(self) -> bool:
        """Drop one context bucket after the GPU refused the last request.

        Any static default is a guess about hardware AND about what else is
        resident - live 2026-08-17 a second 5.1GB model was sharing an 8GB card,
        leaving 1.25GB, and every chat turn died on `cudaMalloc failed`. Rather
        than guess better, take the GPU's answer as the measurement: step down,
        remember the ceiling for the rest of the session, and carry on.
        """
        current = self._num_ctx_ceiling or self._num_ctx_floor or max_ctx()
        smaller = [bucket for bucket in CTX_BUCKETS if bucket < current]
        if smaller:
            self._num_ctx_ceiling = smaller[-1]
            self._num_ctx_floor = 0  # the floor was set under the old, larger ceiling
            self._activity(
                f"GPU could not fit that context; retrying at num_ctx {self._num_ctx_ceiling}."
            )
            return True
        # Already at the smallest window and still refused: the problem is not
        # our context, it is that someone else owns the card. Live 2026-08-17,
        # `qwen2.5-coder` held 5.1GB of an 8GB GPU and left 1.25GB, so every
        # single turn died no matter how small the request.
        return self._evict_other_models()

    def _evict_other_models(self) -> bool:
        """Ask Ollama to drop models we are not using, once per run.

        Safe: Ollama reloads any of them on demand, so the cost is one cold
        start for whoever wanted it, against a session that otherwise cannot
        run at all.
        """
        if self._evicted_others:
            return False
        self._evicted_others = True
        try:
            from shamsu.runtime.ollama import list_loaded_models, unload_model

            others = [name for name in list_loaded_models() if name != self.model_name]
            if not others:
                return False
            for name in others:
                unload_model(name)
            self._activity(
                "GPU was full; unloaded " + ", ".join(others) + " and retrying."
            )
            return True
        except Exception:
            return False

    def _num_ctx(self, messages: list[dict[str, Any]]) -> int:
        ceiling = min(ctx_window_for_model(self.model_name), max_ctx())
        if self._num_ctx_ceiling:
            ceiling = min(ceiling, self._num_ctx_ceiling)
        prompt = count_tokens("\n".join(str(m.get("content", "")) for m in messages))
        needed = prompt + RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
        chosen = ceiling
        for bucket in CTX_BUCKETS:
            if bucket >= needed:
                chosen = min(bucket, ceiling)
                break
        chosen = max(chosen, self._num_ctx_floor)
        self._num_ctx_floor = chosen
        return chosen

    # -- tools -----------------------------------------------------------

    async def _run_tools(self, calls: list[Any]) -> _Round:
        outcome = _Round()
        for call in calls:
            name = _call_name(call)
            arguments = normalize_arguments(name, _call_arguments(call))
            outcome.tool_names.append(name)
            self._activity(f"{name} {_argument_summary(arguments)}")
            self._trace("simple.tool", f"{name} {_argument_summary(arguments)}", {"tool": name})
            result = await asyncio.to_thread(self._execute, name, arguments)
            if self.turn_log:
                self.turn_log.log_tool_result(name, arguments, result.ok, result.message)
            self.state.append_tool(
                _call_id(call), name, _budgeted(result.to_json())
            )
            if result.ok and name in MUTATING_TOOLS:
                path = str(arguments.get("filepath") or "").strip()
                if path:
                    outcome.written.append(path)
        return outcome

    def _execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        target = SIMPLE_TOOLS.get(name)
        if target is None:
            return ToolResult(
                False,
                f"There is no tool called {name}. Available: "
                + ", ".join(sorted(SIMPLE_TOOLS)),
                {"unknown_tool": name},
            )
        if name == "write_file":
            # A model asking to write a file means "make this the content",
            # whether or not it already exists. Refusing without overwrite=True
            # burns a round to teach a flag nobody wants to think about.
            arguments = {**arguments, "overwrite": True}
        try:
            return self.tools.execute(target, arguments)
        except Exception as exc:  # noqa: BLE001 - the model can act on the message
            return ToolResult(False, f"{type(exc).__name__}: {exc}", {"tool": name})

    # -- verification ----------------------------------------------------

    async def _append_verification(self, written: list[str]) -> None:
        """Check what was just written, and put the answer in the conversation.

        Appended as a tool message on purpose. A failure then arrives as
        ordinary information the model can fix on its next turn, instead of a
        separate repair loop, a phase transition, and an UNVERIFIED verdict on a
        run that never wrote anything.
        """
        report = await asyncio.to_thread(self._verify, written)
        if not report:
            return
        self.state.append_tool("", "verify", report)
        self._trace("simple.verify", report.splitlines()[0] if report else "", {})

    def _verify(self, written: list[str]) -> str:
        problems: list[str] = []
        checked: list[str] = []
        for relative in dict.fromkeys(written):
            path = (self.workspace / relative).resolve()
            if not path.is_file():
                problems.append(f"{relative}: file was not created")
                continue
            checked.append(relative)
            if path.suffix != ".py":
                continue
            try:
                compile(path.read_text(encoding="utf-8", errors="replace"), str(path), "exec")
            except SyntaxError as exc:
                problems.append(f"{relative}: line {exc.lineno}: {exc.msg}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{relative}: {type(exc).__name__}: {exc}")
        if problems:
            return json.dumps(
                {"ok": False, "message": "Problems in the files just written.",
                 "data": {"problems": problems}},
                ensure_ascii=True,
            )
        if not checked:
            return ""
        return json.dumps(
            {"ok": True, "message": f"Checked {', '.join(checked)}: no syntax errors.",
             "data": {"checked": checked}},
            ensure_ascii=True,
        )

    # -- helpers ---------------------------------------------------------

    def _stop(
        self,
        message: str,
        rounds: int,
        tool_calls: int,
        changed: list[str],
        *,
        error: str = "",
    ) -> SimpleChatResult:
        self.state.append_assistant(message)
        if self.turn_log:
            self.turn_log.close_turn(message, rounds, stopped=True)
        return SimpleChatResult(
            final=message,
            rounds=rounds,
            tool_calls=tool_calls,
            changed_files=tuple(dict.fromkeys(changed)),
            stopped=True,
            error=error,
        )

    def _activity(self, message: str) -> None:
        if self.on_activity:
            try:
                self.on_activity(message)
            except Exception:
                pass

    def _trace(self, event: str, message: str, data: dict[str, Any]) -> None:
        if self.on_trace:
            try:
                self.on_trace(event, message, data)
            except Exception:
                pass


# --------------------------------------------------------------------------
# Tool-call shape helpers (native dicts and salvaged ToolCall objects both)
# --------------------------------------------------------------------------


def _call_name(call: Any) -> str:
    name = getattr(call, "name", None)
    if name:
        return str(name)
    if isinstance(call, dict):
        function = call.get("function") or {}
        return str(function.get("name") or call.get("name") or "")
    return ""


def _call_arguments(call: Any) -> dict[str, Any]:
    arguments = getattr(call, "arguments", None)
    if arguments is None and isinstance(call, dict):
        function = call.get("function") or {}
        arguments = function.get("arguments", call.get("arguments"))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            return {}
    return dict(arguments) if isinstance(arguments, dict) else {}


def _call_id(call: Any) -> str:
    return str(getattr(call, "call_id", "") or (call.get("id", "") if isinstance(call, dict) else ""))


def _call_to_message(call: Any) -> dict[str, Any]:
    return {
        "function": {
            "name": _call_name(call),
            "arguments": _call_arguments(call),
        }
    }


def _argument_summary(arguments: dict[str, Any]) -> str:
    for key in ("filepath", "path", "pattern", "command"):
        value = arguments.get(key)
        if value:
            return str(value)[:80]
    return ""


# Files a single turn will look up in the code graph, and how many briefs to
# remember. The lookup costs ~2s and does NOT cache internally (measured
# 2026-08-17: three identical calls, ~2.0s each), so it is fetched once per user
# message rather than per round, and memoised on the targets' mtimes.
MAX_BRIEF_TARGETS = 3
_BRIEF_CACHE: dict[tuple[Any, ...], str] = {}
_BRIEF_CACHE_LIMIT = 64


def codebase_brief(workspace: Path, text: str, limit: int = MAX_BRIEF_TARGETS) -> str:
    """What the code graph knows about the existing files *text* names.

    The workspace listing says which files exist; this says what is IN them -
    the half the model was guessing at. Live 2026-08-17 it re-derived
    `frontend/game.js` from its own prose for a dozen turns while a maintained
    graph (106 nodes) sat unread, and invented names that were never there.

    Guarded hard, because the lookup is not free: only files that ALREADY exist
    are considered, at most three, and a turn naming none pays ~0.002s and
    returns immediately. Best-effort - an unindexed or unreachable workspace
    returns "" and the turn proceeds unchanged.
    """
    try:
        from shamsu.abstract.context import build_codebase_memory_brief
        from shamsu.agents.rewrite_fallback import mentioned_workspace_files

        targets = mentioned_workspace_files(workspace, text, limit=limit)
        if not targets:
            return ""
        stamps: list[float] = []
        for relative in targets:
            try:
                stamps.append((Path(workspace) / relative).stat().st_mtime)
            except OSError:
                stamps.append(0.0)
        key = (str(workspace), tuple(targets), tuple(stamps))
        cached = _BRIEF_CACHE.get(key)
        if cached is not None:
            return cached
        brief = build_codebase_memory_brief(workspace, targets) or ""
        if len(_BRIEF_CACHE) >= _BRIEF_CACHE_LIMIT:
            _BRIEF_CACHE.clear()
        _BRIEF_CACHE[key] = brief
        return brief
    except Exception:
        return ""


def _digest(previous: str, evicted: list[Any]) -> str:
    """A deterministic `asked -> did` trace of turns that no longer fit.

    No model call: a digest that costs a round-trip gets skipped under pressure,
    which is exactly when it is needed. What must survive is what was ASKED and
    which files were TOUCHED - that is what a later "continue" depends on.
    """
    asked: list[str] = []
    touched: list[str] = []
    for message in evicted:
        role = getattr(message, "role", "")
        content = str(getattr(message, "content", "") or "").strip()
        if role == "user" and content:
            asked.append(" ".join(content.split())[:120])
        for call in getattr(message, "tool_calls", None) or []:
            function = call.get("function", {}) if isinstance(call, dict) else {}
            arguments = function.get("arguments") or {}
            if not isinstance(arguments, dict):
                continue
            path = str(arguments.get("filepath") or "").strip()
            if path and function.get("name") in MUTATING_TOOLS:
                touched.append(path)
    lines = [line for line in (previous or "").splitlines() if line.strip()]
    lines.extend(f"- you asked: {item}" for item in asked[-8:])
    if touched:
        lines.append("- files changed earlier: " + ", ".join(dict.fromkeys(touched))[:300])
    if not lines:
        return ""
    return "\n".join(lines[-14:])


def workspace_files(workspace: Path, limit: int = MAX_LISTED_FILES) -> list[str]:
    """Every file in the workspace right now, as posix-relative paths.

    Cheap, and recomputed per call rather than remembered: what the model knows
    about the project must not be able to go stale. Live 2026-08-17 it did -
    after a dozen turns the early `list_files` result had been evicted into the
    rolling summary, so the agent started GUESSING the layout, kept re-printing
    "ensure your files are structured like this", and created `scripts.js` and
    `game.js` for the same job without noticing.
    """
    found: list[str] = []
    root = Path(workspace)
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _IGNORED_DIRS and not d.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            try:
                found.append((Path(base) / name).relative_to(root).as_posix())
            except ValueError:
                continue
            if len(found) >= limit:
                return found
    return found


def render_workspace_files(files: list[str]) -> str:
    if not files:
        return "The workspace is empty - nothing has been created yet."
    listing = "\n".join(f"  {path}" for path in files)
    return f"Files in the workspace right now:\n{listing}"


def names_a_workspace_file(text: str, files: list[str]) -> str:
    """The first workspace file this text names, or ``""``."""
    lowered = (text or "").lower()
    for path in files:
        if path.lower() in lowered or Path(path).name.lower() in lowered:
            return path
    return ""


def describes_an_unmade_edit(text: str, files: list[str]) -> str:
    """The file this answer shows instead of writing, or ``""``.

    A reply that prints a substantial code block AND names a real workspace file,
    while calling no tool, has answered the question and skipped the job. Live
    2026-08-17, asked to "implement the fix you just stated", the model spent
    147s producing a complete `game.js` in a fence and wrote nothing.
    """
    body_lines = 0
    for match in _FENCE_RE.finditer(text or ""):
        body_lines = max(body_lines, len(match.group("body").strip().splitlines()))
    if body_lines < 4:
        return ""
    return names_a_workspace_file(text, files)


def _budgeted(payload: str) -> str:
    """Cap one tool result so it cannot crowd the conversation out of the window."""
    if count_tokens(payload) <= MAX_TOOL_RESULT_TOKENS:
        return payload
    keep = MAX_TOOL_RESULT_TOKENS * 4
    return payload[:keep] + "\n...[result truncated by SHAMSU]"


def build_simple_tools(
    workspace: Path,
    *,
    console_approval: Any,
    session_logger: SessionLogger | None = None,
    action_ledger: ActionLedger | None = None,
) -> AgentToolRegistry:
    """An AgentToolRegistry wired for simple mode's approval policy."""
    return AgentToolRegistry(
        workspace,
        approval_func=make_approval_func(console_approval),
        session_logger=session_logger,
        action_ledger=action_ledger,
    )


__all__ = [
    "CTX_BUCKETS",
    "DEFAULT_MAX_ROUNDS",
    "MAX_PROSE_NUDGES",
    "SIMPLE_TOOLS",
    "SIMPLE_TOOL_SCHEMAS",
    "SimpleChatLoop",
    "SimpleChatResult",
    "build_simple_tools",
    "codebase_brief",
    "command_needs_approval",
    "describes_an_unmade_edit",
    "make_approval_func",
    "names_a_workspace_file",
    "normalize_arguments",
    "render_workspace_files",
    "simple_mode_enabled",
    "workspace_files",
]
