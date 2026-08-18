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
import difflib
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

# A single tool result must not be able to crowd out the conversation. 8000
# tokens (~32k chars) against a ~24k-token prompt budget: a source file the
# model has to EDIT is worth a large share of it, and 2000 silently cut
# ordinary files down to a fragment.
MAX_TOOL_RESULT_TOKENS = 8000

# Files named in the always-fresh workspace listing, and the noise excluded from
# it. Small enough to be nearly free; the point is grounding, not a project dump.
MAX_LISTED_FILES = 80
_IGNORED_DIRS = frozenset(
    {".shamsu", ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".idea"}
)

# How many times one run may tell the model "you described it, now do it".
MAX_PROSE_NUDGES = 2

# How many empty replies one run tolerates before stopping and saying so.
# Unbounded, this was a 24-round hang.
MAX_EMPTY_NUDGES = 2

# Consecutive mutations that change nothing before the run gives up. Failed
# patches and no-op patches both count: either way the file is untouched and
# repeating is not going to help.
MAX_UNPRODUCTIVE_EDITS = 4

# Successful edits to ONE file in a single turn before the loop says so, and
# before it gives up. These edits DO change the file, so the no-change counter
# never sees them - but a fix that needs a seventh attempt is not a fix, it is
# guessing. Live 2026-08-18: 7 successful patches to one file in one turn, and
# not once did the model say it could not verify any of them.
EDITS_PER_FILE_BEFORE_WARNING = 3
EDITS_PER_FILE_BEFORE_STOPPING = 5

# Identical read-only calls in one turn before the loop points it out. Reads
# never change anything, so the no-change counter above cannot see them
# spinning: live 2026-08-18 a turn issued `list_files {path: "."}` three times
# in a row, each returning the same listing it already had.
REPEATED_READS_BEFORE_WARNING = 3

# Diff lines fed back after an edit. Enough to see the change, not so many
# that rewriting a file replays the whole file into the conversation.
MAX_DIFF_LINES = 40

# How often the live status line reports that a model call is still running.
HEARTBEAT_SECONDS = 5.0

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

    32768. The window is a VRAM cost, not a free capability - Ollama reserves
    the KV cache for the WHOLE num_ctx up front - but the cost is set by the
    cache's precision, not by the window alone. Measured on an 8GB card with
    qwen3.5:9b, 2026-08-18:

        f16 KV (default):  16384 -> 47.5s, spilled to CPU; 32768 -> OOM
        q8_0 KV + flash :   8192 -> 10.3s | 16384 -> 7.4s | 32768 -> 7.5s
                            and 32768 sits at 6891 MiB, 100% on GPU

    So 32k is only ~385 MiB more than 8k with a quantized cache, and free in
    time. That needs, on the Ollama SERVER (not per request):

        OLLAMA_FLASH_ATTENTION=1
        OLLAMA_KV_CACHE_TYPE=q8_0

    Without them a 32k request spills to CPU or fails outright - which is what
    "the harness got extremely slow" was. `_shrink_for_oom` walks the window
    back down if the GPU refuses, so an unconfigured server degrades instead of
    hanging, but it degrades to SLOW. Check those two vars first.

    SHAMSU_CHAT_MAX_CTX overrides.
    """
    raw = os.environ.get("SHAMSU_CHAT_MAX_CTX", "").strip()
    if raw.isdigit() and int(raw) >= 4096:
        return int(raw)
    return 32768


def output_reserve(ceiling: int) -> int:
    """Tokens held back for the model's reply, as a SHARE of the window.

    A fixed 4096 reserve is what starved simple mode: at num_ctx 32768 the
    prompt was allowed to grow to 28160 and the reply - thinking AND answer
    together - got the same 4096 it would have had at 8k. A reasoning model
    spends that thinking and emits nothing, which the loop then read as an
    empty reply and nudged, forever.

    A quarter of the window scales with it: 8k -> 4096, 32k -> 8192.
    """
    return max(RESERVE_OUTPUT_TOKENS, ceiling // 4)


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
            "description": (
                "Read a file from the workspace. Omit start_line/end_line to read "
                "the whole file; pass them to read one part of a large file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path relative to the workspace."},
                    "start_line": {
                        "type": "integer",
                        "description": "First line to read (1-based). Optional.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to read, inclusive. Optional.",
                    },
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
            "description": (
                "Create a NEW file, or replace a small one completely. To change part "
                "of an existing file, prefer patch_file: it is far faster and cannot "
                "lose the parts you did not mean to touch."
            ),
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

# Every `name` simple mode itself writes into a transcript. This is the set
# history is filtered against on rehydration, so it must include names the loop
# APPENDS as well as the ones the model calls - `verify` is written by
# `_append_verification`, and filtering it out would silently drop "your file
# failed to compile" from the conversation the next turn sees.
SIMPLE_TRANSCRIPT_TOOLS = frozenset(SIMPLE_TOOLS) | {"verify"}

# Legacy LOGICAL tool names, mapped to the six simple ones.
#
# Simple mode never offers these, so `_execute` used to refuse them outright -
# correct, but it costs a round each time and the model rarely takes the hint.
# Two things put them in front of the model anyway:
#
#   1. A legacy-routed SHAMSU (`SHAMSU_LEGACY_ROUTING=1`, or an older build)
#      sharing the workspace appends ITS calls to the same session transcript.
#      Live 2026-08-18 that happened: `project.inspect`, `file.read`,
#      `code.search` and `test.run` landed in the history of a simple-mode
#      session, and the model - reasonably - kept calling what it could see
#      itself having called.
#   2. The names are close enough to other agents' conventions to be guessed.
#
# Accepting them is strictly better than refusing: same sandbox, same ledger,
# same six implementations - just a name the model already believes in.
_TOOL_NAME_ALIASES: dict[str, str] = {
    "file.read": "read_file",
    "file.write": "write_file",
    "file.patch": "patch_file",
    "file.edit": "patch_file",
    "code.search": "search_files",
    "file.search": "search_files",
    "project.inspect": "list_files",
    "file.list": "list_files",
    "shell.run": "run_command",
    "test.run": "run_command",
    "command.run": "run_command",
}


def canonical_tool_name(name: str) -> str:
    """The simple-mode tool *name* refers to, accepting legacy/near-miss spellings."""
    raw = (name or "").strip()
    if raw in SIMPLE_TOOLS:
        return raw
    lowered = raw.lower()
    if lowered in _TOOL_NAME_ALIASES:
        return _TOOL_NAME_ALIASES[lowered]
    # `functions.read_file`, `tools.read_file` - a prefix some models emit.
    tail = lowered.rsplit(".", 1)[-1]
    if tail in SIMPLE_TOOLS:
        return tail
    return raw

# Argument aliases a small model reaches for. Accepting them costs nothing and
# saves a whole failed round each time, which on a 6-round budget is expensive.
_ARG_ALIASES = {
    "path": "filepath",
    "file": "filepath",
    "file_path": "filepath",
    "filename": "filepath",
    "text": "content",
    "old": "old_string",
    "new": "new_string",
    "cmd": "command",
}

# Tools whose real implementation names an argument differently from the schema
# the model is shown. `search_files` is `grep_files`, which reads `query` - so
# the schema's own `pattern` normalised to nothing and EVERY search failed with
# "Missing or placeholder query" (measured 2026-08-18, 100% of calls). A rename
# in one table is the whole fix; per-tool entries win over the global aliases.
_TOOL_ARG_ALIASES: dict[str, dict[str, str]] = {
    "list_files": {"path": "path", "dir": "path", "directory": "path", "folder": "path"},
    "search_files": {
        "pattern": "query",
        "query": "query",
        "text": "query",
        "search": "query",
        "regex": "query",
        "string": "query",
        "path": "path",
        "dir": "path",
        "directory": "path",
    },
}


def normalize_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Accept the near-miss argument names small models produce.

    Two layers: a per-tool table for tools whose implementation genuinely names
    an argument differently (`list_files`/`search_files` take a `path`, not a
    `filepath`; `search_files` takes a `query`, not a `pattern`), then the
    global near-miss aliases for everything else.
    """
    per_tool = _TOOL_ARG_ALIASES.get(tool, {})
    normalized: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        target = per_tool.get(key) or _ARG_ALIASES.get(key, key)
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
    repeated_edit: int = 0
    repeated_path: str = ""
    repeated_read: str = ""


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
        on_status: Any | None = None,
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
        # Replaces a LIVE status line rather than printing a new one. Without
        # it a model call is completely silent for as long as it takes, and at
        # a 600s timeout that is ten minutes indistinguishable from a hang -
        # which is exactly how it was reported.
        self.on_status = on_status
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
            # Replay only calls this loop could have made. The legacy router
            # shares the same transcript and speaks a different vocabulary; a
            # model shown `project.inspect` in its own history will call it.
            known_tools=SIMPLE_TRANSCRIPT_TOOLS,
        )
        self._num_ctx_floor = 0
        # Lowered from what the GPU will actually accept, once it has refused.
        self._num_ctx_ceiling = 0
        self._evicted_others = False
        # Files the model has seen only part of - writing them whole loses data.
        self._partial_reads: set[str] = set()
        # Line ranges seen per file, so reading a big file IN PIECES still
        # adds up to having seen it.
        self._seen_ranges: dict[str, list[tuple[int, int]]] = {}
        # Consecutive mutations that changed nothing.
        self._unproductive = 0
        # Successful edits per file this turn - repeated blind fixes.
        self._edits_per_file: dict[str, int] = {}
        # How many times each identical read-only call has been made this turn.
        self._read_signatures: dict[str, int] = {}
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
        # Refresh the ownership heartbeat every turn. Claiming only at resume
        # left it stale after 5 minutes, so a second window would decide the
        # thread was free and attach to it - the interleaving this is meant to
        # prevent, just delayed by one long turn.
        claim = getattr(self.session_logger, "claim", None)
        if callable(claim):
            try:
                claim()
            except Exception:
                pass
        self.state.append_user(user_input)
        await self._compact_if_needed()
        if self.log_turns:
            try:
                # Scope the log to the SESSION, so a thread is one readable file
                # instead of a pile of turn-NNN files nobody can tell apart.
                session_id = getattr(self.session_logger, "session_id", "") or ""
                title = getattr(getattr(self.session_logger, "metadata", None), "title", "") or ""
                self.turn_log = SimpleTurnLog(
                    self.workspace,
                    next_turn_number(self.workspace, session_id),
                    self.model_name,
                    session_id=session_id,
                    session_title=title,
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
        empty_nudges = 0
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
                    # An empty turn is not an answer - but nudging forever is
                    # worse. Live 2026-08-18 this branch had no counter and did
                    # not append the assistant turn, so a starved model produced
                    # a run of consecutive `user` messages and the loop span all
                    # 24 rounds: half an hour of "Thinking..." and no reply.
                    if empty_nudges < MAX_EMPTY_NUDGES:
                        empty_nudges += 1
                        # Keep the transcript alternating: an assistant turn,
                        # then the nudge. Stacking user messages is what broke
                        # it. And nudge BEFORE salvaging - a reasoning model's
                        # first empty turn usually means it is about to call a
                        # tool, so ending the turn here does no work at all.
                        # (Salvaging first cost the probe turns 8-10: 0 tools,
                        # main.py never written.)
                        self.state.append_assistant("")
                        self.state.append_user(
                            "That reply was empty. Answer the question, or call one tool."
                        )
                        continue
                    if turn.thinking:
                        # Out of retries but it did reason - that beats an error.
                        self._activity("model only reasoned; using its thinking as the answer")
                        self.state.append_assistant(turn.thinking)
                        if self.turn_log:
                            self.turn_log.close_turn(turn.thinking, round_index + 1, stopped=False)
                        return SimpleChatResult(
                            final=turn.thinking,
                            rounds=round_index + 1,
                            tool_calls=tool_calls,
                            changed_files=tuple(dict.fromkeys(changed)),
                        )
                    return self._stop(
                        f"The model returned an empty reply {empty_nudges + 1} times. "
                        "It is most likely out of room to answer in - try a shorter "
                        "request, or `/new` to start a fresh conversation.",
                        round_index,
                        tool_calls,
                        changed,
                    )
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
            if self._unproductive >= MAX_UNPRODUCTIVE_EDITS:
                # Spinning. Live 2026-08-18 a turn ran 12 no-op patches and 5
                # failed ones across 24 rounds and ~25 minutes, changing nothing
                # - and the only thing that stopped it was max_rounds. Say what
                # happened instead of burning the rest of the budget.
                return self._stop(
                    f"I tried {self._unproductive} edits in a row that changed nothing - "
                    "either the snippet I was matching is not in the file, or my "
                    "replacement was identical to what is already there. I have stopped "
                    "rather than keep guessing.\n\n"
                    "It would help to tell me the exact text to look for, or to paste "
                    "the few lines around the problem.",
                    round_index,
                    tool_calls,
                    changed,
                )
            if outcome.repeated_read:
                # A read that repeats verbatim is the model having lost track of
                # what it already has, not new information. Say so once and name
                # the result it is re-fetching, rather than letting it spend the
                # round budget re-reading the same listing.
                self.state.append_user(
                    f"You have already called {outcome.repeated_read} this turn and the "
                    "result has not changed. Use the result you already have, and either "
                    "answer now or make a DIFFERENT call."
                )
                self._read_signatures[outcome.repeated_read] = 0
                self._activity(f"repeated {outcome.repeated_read}; asked it to move on")
            if outcome.repeated_edit >= EDITS_PER_FILE_BEFORE_STOPPING:
                # Repeatedly "fixing" one file without ever confirming a fix is
                # guessing, and each guess can damage what was already right.
                # Live 2026-08-18: 7 successful patches to one file in a single
                # turn, chasing an error that had already been fixed - the
                # console message was a stale browser cache.
                return self._stop(
                    f"I have now changed {outcome.repeated_path} "
                    f"{outcome.repeated_edit} times in this turn without being able to "
                    "confirm any of them worked, so I have stopped rather than keep "
                    "guessing.\n\n"
                    "Check `git diff` on that file - repeated blind edits can undo "
                    "things that were already correct. To go further I need either "
                    "permission to run something that reproduces the problem, or the "
                    "exact current error after a hard refresh.",
                    round_index,
                    tool_calls,
                    changed,
                )
            if outcome.repeated_edit == EDITS_PER_FILE_BEFORE_WARNING:
                self._activity(
                    f"changed {outcome.repeated_path} {outcome.repeated_edit} times "
                    "this turn without confirming a fix"
                )
                self.state.append_user(
                    f"You have changed {outcome.repeated_path} "
                    f"{outcome.repeated_edit} times this turn and cannot confirm any of "
                    "them worked. Do not edit it again on a guess. Either say precisely "
                    "what you changed and what the user should check, or ask for the "
                    "exact error or the lines around the problem."
                )
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
        beat = asyncio.ensure_future(self._heartbeat("thinking..."))
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
            beat.cancel()
            self._activity(f"model responded in {time.perf_counter() - started:.0f}s")
        if self.turn_log:
            self.turn_log.log_response(raw, time.perf_counter() - started)
        return raw

    async def _compact_if_needed(self) -> None:
        """Once per user turn, ask the model to summarise what no longer fits.

        The deterministic digest records what was ASKED and which files were
        TOUCHED - exact, free, and it cannot record a DECISION. It remembers
        "you asked to slow the ship" and never "we set maxSpeed to 4.5", which
        is the half a later turn actually needs.

        So: facts stay deterministic, and a model call adds the reasoning. Done
        here rather than inside `_messages` because that is sync and runs per
        round; compaction belongs once per turn, and at a 32k window it fires
        rarely enough that the round-trip is cheap. If the call fails the
        deterministic digest still stands - this only ever adds.
        """
        ceiling = min(ctx_window_for_model(self.model_name), max_ctx())
        if self._num_ctx_ceiling:
            ceiling = min(ceiling, self._num_ctx_ceiling)
        usable = max(1024, ceiling - output_reserve(ceiling) - SAFETY_MARGIN_TOKENS)
        _tail, start_abs = self.state.select_for_budget(usable, count_tokens)
        if start_abs <= 1:
            return
        evicted = self.state.newly_evicted(start_abs)
        if not evicted:
            return

        facts = _digest(self.state.rolling_summary, evicted)
        # Use the bucket the REAL call is about to use. `self._num_ctx_floor` is
        # 0 here - a fresh loop is built per user turn - so reading it fell back
        # to the smallest bucket and every compaction cost a model reload.
        kept_tokens = sum(count_tokens(str(getattr(m, "content", "") or "")) for m in _tail)
        narrative = await self._narrate(
            evicted, self._bucket_for(kept_tokens + count_tokens(self.state.system_prompt))
        )
        # Decisions FIRST: `_bounded_summary` keeps both ends, so putting them
        # at the head guarantees they outlive the raw "you asked" lines. Live
        # 2026-08-18 eight lines of routine asks nearly buried the two lines
        # that actually mattered.
        combined = f"{narrative}\n{facts}".strip() if narrative else facts
        if combined:
            self.state.update_rolling_summary(
                _bounded_summary(
                    [line for line in combined.splitlines() if line.strip()],
                    summary_budget(ceiling),
                ),
                start_abs,
            )

    async def _narrate(self, evicted: list[Any], num_ctx: int | None = None) -> str:
        """One short model call: what was DECIDED in the turns being dropped."""
        transcript = "\n".join(
            f"{getattr(m, 'role', '?')}: {str(getattr(m, 'content', '') or '')[:600]}"
            for m in evicted
            if str(getattr(m, "content", "") or "").strip()
        )
        if not transcript.strip():
            return ""
        ask = (
            "Below is part of a conversation that no longer fits in context. "
            "List only the DECISIONS and facts a later turn would need - names, "
            "numbers, file paths, choices made. Up to 6 lines, each starting "
            "'- '. No preamble, no commentary.\n\n" + transcript[:12000]
        )
        try:
            raw = await asyncio.wait_for(
                self.client.chat(
                    model=self.model_name,
                    messages=[{"role": "user", "content": ask}],
                    stream=False,
                    options={
                        "temperature": 0.0,
                        "num_ctx": num_ctx or self._num_ctx([]),
                    },
                ),
                timeout=min(self.request_timeout, 120.0),
            )
        except Exception:
            return ""  # the deterministic digest still stands
        turn = parse_model_turn(raw, set())
        lines = [
            line.strip()
            for line in (turn.text or "").splitlines()
            if line.strip().startswith("-")
        ]
        if lines:
            self._activity(f"compacted {len(evicted)} older messages")
        return "\n".join(lines[:6])

    def _messages(self) -> list[dict[str, Any]]:
        """System prompt + workspace listing + older summary + the conversation.

        The listing is the one piece of injected context, and it earns its ~60
        tokens: it is the difference between working on the project and guessing
        at it. Everything else is the policy `_messages_within_budget` already
        uses - keep the largest recent suffix that fits, older turns survive as
        the rolling summary.
        """
        ceiling = min(ctx_window_for_model(self.model_name), max_ctx())
        if self._num_ctx_ceiling:
            ceiling = min(ceiling, self._num_ctx_ceiling)
        usable = max(1024, ceiling - output_reserve(ceiling) - SAFETY_MARGIN_TOKENS)
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
        prompt = count_tokens("\n".join(str(m.get("content", "")) for m in messages))
        chosen = self._bucket_for(prompt)
        self._num_ctx_floor = chosen
        return chosen

    def _bucket_for(self, prompt_tokens: int) -> int:
        """One window for the whole session: the ceiling. No side effects.

        Sizing num_ctx to the prompt was a false economy once the KV cache was
        quantized. Measured on an 8GB card 2026-08-18:

            8192 -> 6506 MiB | 16384 -> 6702 MiB | 32768 -> 6891 MiB

        The ENTIRE range costs 385 MiB, while changing num_ctx makes Ollama
        reload the model - live, a compaction call at 8192 followed by chat at
        16384 produced 290s and 282s rounds. Worse, the smaller bucket halves
        the reply reserve (`output_reserve`): 4096 at 16k against 8192 at 32k,
        and a reasoning model that runs out of room returns EMPTY, which is what
        ended that turn after 15 minutes.

        So: pay the 385 MiB once and never reload. `_shrink_for_oom` still steps
        down if the GPU refuses, which is the case this used to be guessing at.
        Prefill is charged on the actual prompt, not the window, so a big window
        costs nothing in time.
        """
        ceiling = min(ctx_window_for_model(self.model_name), max_ctx())
        if self._num_ctx_ceiling:
            ceiling = min(ceiling, self._num_ctx_ceiling)
        return max(ceiling, self._num_ctx_floor) if self._num_ctx_floor else ceiling

    # -- tools -----------------------------------------------------------

    async def _run_tools(self, calls: list[Any]) -> _Round:
        outcome = _Round()
        for call in calls:
            name = canonical_tool_name(_call_name(call))
            arguments = normalize_arguments(name, _call_arguments(call))
            outcome.tool_names.append(name)
            self._activity(f"{name} {_argument_summary(arguments)}")
            self._trace("simple.tool", f"{name} {_argument_summary(arguments)}", {"tool": name})
            # A tool can block for as long as its timeout allows - `run_command`
            # defaults to 120s, and a server started in the foreground will use
            # every second of it. Without a tick that is two minutes of silence
            # immediately AFTER an approval prompt, which reads as a hang.
            beat = asyncio.ensure_future(self._heartbeat(f"running {name}..."))
            try:
                result = await asyncio.to_thread(self._execute, name, arguments)
            finally:
                beat.cancel()
            if self.turn_log:
                self.turn_log.log_tool_result(name, arguments, result.ok, result.message)
            payload = _budgeted(result.to_json())
            if name == "read_file" and '"content_truncated": true' in payload.lower():
                # `_budgeted` trims AFTER `_execute` has run, so the ToolResult
                # the partial-read guard inspected looked complete. Without this
                # the model could be handed a clipped file and still be allowed
                # to rewrite it whole - losing everything it never saw.
                target = str(
                    (result.data or {}).get("resolved_filepath")
                    or arguments.get("filepath")
                    or ""
                ).strip()
                if target:
                    self._partial_reads.add(target.lower())
            self.state.append_tool(_call_id(call), name, payload)
            if name in MUTATING_TOOLS:
                if _changed_nothing(result):
                    self._unproductive += 1
                else:
                    self._unproductive = 0
            else:
                signature = f"{name}({_argument_summary(arguments)})"
                seen = self._read_signatures.get(signature, 0) + 1
                self._read_signatures[signature] = seen
                if seen >= REPEATED_READS_BEFORE_WARNING:
                    outcome.repeated_read = signature
            if result.ok and name in MUTATING_TOOLS:
                path = str(arguments.get("filepath") or "").strip()
                if path:
                    outcome.written.append(path)
                    count = self._edits_per_file.get(path.lower(), 0) + 1
                    self._edits_per_file[path.lower()] = count
                    outcome.repeated_edit = max(outcome.repeated_edit, count)
                    outcome.repeated_path = path if count >= EDITS_PER_FILE_BEFORE_WARNING else outcome.repeated_path
        return outcome

    def _execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        name = canonical_tool_name(name)
        target = SIMPLE_TOOLS.get(name)
        if target is None:
            return ToolResult(
                False,
                f"There is no tool called {name}. Available: "
                + ", ".join(sorted(SIMPLE_TOOLS)),
                {"unknown_tool": name},
            )
        if name == "write_file":
            blocked = self._refuse_blind_overwrite(arguments)
            if blocked is not None:
                return blocked
            # A model asking to write a file means "make this the content",
            # whether or not it already exists. Refusing without overwrite=True
            # burns a round to teach a flag nobody wants to think about.
            arguments = {**arguments, "overwrite": True}
        before = self._snapshot(arguments) if name in MUTATING_TOOLS else None
        try:
            result = self.tools.execute(target, arguments)
        except Exception as exc:  # noqa: BLE001 - the model can act on the message
            return ToolResult(False, f"{type(exc).__name__}: {exc}", {"tool": name})
        if name == "read_file":
            self._note_partial_read(arguments, result)
        if before is not None and result.ok:
            return self._with_diff(arguments, before, result)
        return result

    def _snapshot(self, arguments: dict[str, Any]) -> str:
        path = str(arguments.get("filepath") or "").strip()
        if not path:
            return ""
        try:
            return (self.workspace / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _with_diff(
        self, arguments: dict[str, Any], before: str, result: ToolResult
    ) -> ToolResult:
        """Show the model what its edit ACTUALLY did.

        A mutation used to report "Edited game.js: +3 -1 lines" - a count, not a
        change. So the model could not tell a fix from a no-op from an edit that
        removed the wrong thing, and live 2026-08-18 it patched one file seven
        times in a turn without ever seeing the result of any of them.

        Deliberately NOT git. `git diff` shows everything since the last commit
        - the user's work mixed with the agent's - and says nothing at all in a
        workspace that is not a repo, which is the common case. Diffing the file
        against its own state one call ago is exact, always available, and
        scoped to precisely this edit.
        """
        path = str(arguments.get("filepath") or "").strip()
        if not path:
            return result
        after = self._snapshot(arguments)
        if after == before:
            return result
        diff = list(
            difflib.unified_diff(
                before.splitlines(), after.splitlines(),
                fromfile=f"{path} (before)", tofile=f"{path} (after)",
                lineterm="", n=2,
            )
        )
        if not diff:
            return result
        shown = diff[:MAX_DIFF_LINES]
        if len(diff) > MAX_DIFF_LINES:
            shown.append(f"... [{len(diff) - MAX_DIFF_LINES} more diff lines]")
        data = dict(result.data) if isinstance(result.data, dict) else {}
        data["diff_lines"] = len(diff)
        return ToolResult(
            result.ok,
            f"{result.message}\n\nWhat changed:\n" + "\n".join(shown),
            data,
        )

    def _note_partial_read(self, arguments: dict[str, Any], result: ToolResult) -> None:
        """Track how much of each file the model has actually seen.

        Ranges have to ACCUMULATE. Marking a file partial on any range read and
        only clearing it on a whole-file read made the guard a dead end: told to
        read a large file with start_line/end_line, the model would read it all
        in pieces and still be refused a write, forever. Live 2026-08-18 that
        left one turn alternating read and patch for 24 rounds.
        """
        data = result.data if isinstance(result.data, dict) else {}
        if not result.ok:
            return
        path = str(data.get("resolved_filepath") or arguments.get("filepath") or "").strip()
        if not path:
            return
        key = path.lower()
        total = data.get("total_lines")
        start = data.get("start_line")
        end = data.get("end_line")
        ranged = isinstance(start, int) and isinstance(end, int)
        # Three different things can shorten a read, and they mean different
        # things. `content_truncated` is the conversation budget clipping it.
        # `truncated` means "you did not get every line" - but on a RANGE read
        # that is just the range doing its job, so it only signals clipping when
        # no range was asked for. Conflating them cleared the flag on a file the
        # model had only seen the head of.
        clipped = bool(data.get("content_truncated")) or (
            not ranged and bool(data.get("truncated"))
        )

        if not clipped and not ranged:
            # A complete, unclipped read: the model has seen all of it.
            self._seen_ranges.pop(key, None)
            self._partial_reads.discard(key)
            return

        if ranged and not clipped:
            seen = self._seen_ranges.setdefault(key, [])
            seen.append((start, end))
            if isinstance(total, int) and _covers(seen, total):
                # Read in pieces, but all of it - that is not a partial read.
                self._seen_ranges.pop(key, None)
                self._partial_reads.discard(key)
                return
        self._partial_reads.add(key)

    def _refuse_blind_overwrite(self, arguments: dict[str, Any]) -> ToolResult | None:
        """Stop a whole-file write of a file the model has only partly read.

        The failure this prevents: a truncated read leaves the model holding a
        fragment, it "rewrites" the file from that fragment, and everything it
        never saw is gone. The existing gutting guard does not catch it - that
        one needs a shrink to under 25% AND the loss of every declaration, built
        for a file replaced by the three bytes `}`. Losing the last third of a
        file passes it cleanly.
        """
        path = str(arguments.get("filepath") or "").strip()
        if not path:
            return None
        if path.lower() in self._partial_reads:
            return ToolResult(
                False,
                f"You have only seen part of {path}, so writing it whole would delete "
                "the rest. Either call patch_file to change just the part you need, or "
                "read the remainder first with read_file(start_line=..., end_line=...).",
                {"filepath": path, "partial_read": True},
            )
        return self._refuse_unwritable_rewrite(path)

    def _refuse_unwritable_rewrite(self, path: str) -> ToolResult | None:
        """Stop a whole-file rewrite that physically cannot fit in one reply.

        Not a style preference - a hard limit. The reply reserve is
        `output_reserve(num_ctx)` tokens, so a file larger than that cannot be
        emitted whole however well the model reads it: generation simply stops
        mid-file and the tail is lost. `patch_file` costs the same regardless of
        file size, which is why it is the answer rather than a bigger window.

        Measured 2026-08-18: whole-file rewrites drove one turn to 18 minutes
        over 18 rounds at ~100s per write.
        """
        try:
            existing = (self.workspace / path).stat().st_size
        except OSError:
            return None  # new file - nothing to lose, and nothing to compare
        ceiling = min(ctx_window_for_model(self.model_name), max_ctx())
        if self._num_ctx_ceiling:
            ceiling = min(ceiling, self._num_ctx_ceiling)
        writable_chars = output_reserve(ceiling) * 4
        if existing <= writable_chars:
            return None
        return ToolResult(
            False,
            f"{path} is {existing:,} bytes and one reply can hold about "
            f"{writable_chars:,}, so rewriting it whole would be cut off partway "
            "and lose the rest. Use patch_file for the change you actually want - "
            "its cost does not grow with the file.",
            {"filepath": path, "size": existing, "writable_chars": writable_chars},
        )
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

    def _status(self, message: str) -> None:
        if self.on_status:
            try:
                self.on_status(message)
            except Exception:
                pass

    async def _heartbeat(self, label: str) -> None:
        """Tick a live status while a model call is in flight, until cancelled."""
        started = time.perf_counter()
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                self._status(f"{label} {time.perf_counter() - started:.0f}s")
        except asyncio.CancelledError:
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
    # Eight. Trimming these to make room for the model-written decisions looked
    # tempting after a synthetic run where they were all filler, but in a real
    # conversation they carry facts nothing else records - dropping to four lost
    # "the port is 8080" from an early turn. Decisions are protected by ORDER
    # instead: they lead, so the head of a bounded summary always keeps them.
    lines.extend(f"- you asked: {item}" for item in asked[-8:])
    if touched:
        lines.append("- files changed earlier: " + ", ".join(dict.fromkeys(touched))[:300])
    if not lines:
        return ""
    return _bounded_summary(lines, summary_budget(max_ctx()))




def _changed_nothing(result: ToolResult) -> bool:
    """Did this mutation leave the file exactly as it was?

    Both shapes count, because both are the model spinning: a patch that FAILED
    (old_string not found) and a patch that "succeeded" with old_string ==
    new_string. Live 2026-08-18 one turn ran 12 no-op patches and 5 failed ones
    - 17 mutations that changed nothing - and the harness ran every one without
    noticing.
    """
    if not result.ok:
        return True
    message = (result.message or "").lower()
    return "nothing to change" in message or "identical" in message


def _covers(ranges: list[tuple[int, int]], total_lines: int) -> bool:
    """Do these (start, end) line ranges together cover 1..total_lines?"""
    if total_lines <= 0:
        return False
    reach = 0
    for start, end in sorted(ranges):
        if start > reach + 1:
            return False          # a gap the model has not read
        reach = max(reach, end)
    return reach >= total_lines


def summary_budget(ceiling: int) -> int:
    """Tokens the rolling summary may occupy: ~6% of the window.

    It has to be bounded, or the thing protecting the window eventually eats it
    - the summary now PERSISTS and accumulates across every turn of a thread
    that may run for weeks.
    """
    return max(256, ceiling // 16)


def _bounded_summary(lines: list[str], budget_tokens: int) -> str:
    """Fit the summary to *budget_tokens*, dropping the MIDDLE.

    The old rule was `lines[-14:]` - keep the newest fourteen - which discarded
    exactly the founding decisions a long thread depends on ("the window is
    900x700", "max speed is 4.5") while keeping incidental recent chatter. Both
    ends are what matter: what the project IS, and what just happened.
    """
    joined = chr(10).join(lines)
    if count_tokens(joined) <= budget_tokens:
        return joined
    head, tail = 4, 10
    while head + tail > 2:
        kept = lines[:head] + ["- (older detail compacted)"] + lines[-tail:]
        text = chr(10).join(kept)
        if count_tokens(text) <= budget_tokens:
            return text
        if tail > head:
            tail -= 1
        else:
            head -= 1
    return chr(10).join(lines[:1] + ["- (older detail compacted)"] + lines[-1:])

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
    """Cap one tool result so it cannot crowd the conversation out of the window.

    Truncates the CONTENT field, not the JSON string. Slicing raw JSON left an
    unterminated string and unclosed braces - a payload the model can only read
    as garbage. Whatever survives the cap must still be shaped like a result.
    """
    if count_tokens(payload) <= MAX_TOOL_RESULT_TOKENS:
        return payload
    keep = MAX_TOOL_RESULT_TOKENS * 4
    try:
        parsed = json.loads(payload)
        data = parsed.get("data")
        if isinstance(data, dict) and isinstance(data.get("content"), str):
            body = data["content"]
            overflow = len(payload) - keep
            if 0 < overflow < len(body):
                kept = body[: len(body) - overflow]
                shown = kept.count("\n") + 1
                total = data.get("total_lines")
                # Name the exact next call. "Read the rest with start_line" was
                # too vague to act on: live 2026-08-18 the model re-read the
                # same file twice, got the same head both times, and gave up
                # without ever trying a range or patch_file.
                if isinstance(total, int) and total > shown:
                    guidance = (
                        f"... [showing lines 1-{shown} of {total}. "
                        f"For the rest call read_file(start_line={shown + 1}, "
                        f"end_line={total}). To CHANGE this file use patch_file - "
                        "it is too large to rewrite whole.]"
                    )
                else:
                    guidance = (
                        f"... [truncated {overflow} chars - call read_file with "
                        "start_line/end_line for the rest, and patch_file to change it]"
                    )
                data["content"] = kept + "\n" + guidance
                data["content_truncated"] = True
                data["shown_lines"] = shown
                return json.dumps(parsed, ensure_ascii=True)
    except (ValueError, AttributeError, TypeError):
        pass
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
    "canonical_tool_name",
    "DEFAULT_MAX_ROUNDS",
    "MAX_EMPTY_NUDGES",
    "MAX_PROSE_NUDGES",
    "REPEATED_READS_BEFORE_WARNING",
    "SIMPLE_TOOLS",
    "SIMPLE_TOOL_SCHEMAS",
    "SIMPLE_TRANSCRIPT_TOOLS",
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
