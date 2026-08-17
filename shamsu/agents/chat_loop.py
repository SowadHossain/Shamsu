"""Stateful ReAct chat loop using Ollama's native tool calling."""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os as _os
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from collections.abc import Sequence
from typing import Any

import httpx
import ollama

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.artifacts.code import (
    hash_source_text,
    invalidate_artifacts_if_hash_mismatch,
    mark_artifacts_stale_for_paths,
    refresh_artifacts_for_paths,
)
from shamsu.audit import SessionAuditLog
from shamsu.agents.chat_state import ChatState
from shamsu.agents.clarification import format_question
from shamsu.agents.executor import (
    AgentExecutor,
    StepExecutionController,
    StepExecutionDecision,
    StepExecutionLimits,
    StepExecutionOutcome,
)
from shamsu.agents.markdown_fallback import MarkdownWriteFallback
from shamsu.agents.planner import AgentPlanner, create_plan
from shamsu.agents.prompting import PromptProfile, compose_agent_prompt, prompt_profile_for_model
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
from shamsu.agents.project_instructions import load_project_instructions
from shamsu.agents.repair import RepairRecorder
from shamsu.llm.output import ParseFailure, parse_model_turn, tool_call_to_message_dict
from shamsu.memory.service import MemoryService
from shamsu.routing.operations import file_targets
from shamsu.runtime.models import (
    model_for_role,
    model_supports_native_tools,
    role_should_think,
)
from shamsu.context.compiler import ContextCompiler
from shamsu.runtime.engine import RuntimeEngine
from shamsu.runtime.failures import FailureTracker, FailureType, RecoveryAction, failure_type_for_timeout
from shamsu.runtime.run_control import ControlledRun, time_remaining, timed_out, wait_if_paused
from shamsu.runtime.timeouts import ShamsuTimeoutError, TimeoutCategory, TimeoutConfig, timeout_failure_detail
from shamsu.runtime.phase_contracts import ExecutionPhase, normalize_phase, phase_for_step
from shamsu.runtime.task_state import (
    PlanStepStatus,
    RuntimeStateStore,
    TaskStepStatus,
    TaskState,
)
from shamsu.session.manager import SessionLogger
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.tools.dispatcher import ToolDispatcher
from shamsu.ui.progress import ProgressReporter, summarize_tool_args, summarize_tool_result
from shamsu.verification.verifier import ChangeVerifier
from shamsu.types import RunStatus

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
_RUN_TIMEOUT_SECONDS: int = int(_os.environ.get("SHAMSU_RUN_TIMEOUT_SECONDS", "300"))
_REPAIR_MODEL_TIMEOUT_SECONDS: int = int(
    _os.environ.get("SHAMSU_REPAIR_MODEL_TIMEOUT_SECONDS", "120")
)
_REPAIR_MODEL_MAX_OUTPUT_TOKENS: int = int(
    _os.environ.get("SHAMSU_REPAIR_MODEL_MAX_OUTPUT_TOKENS", "2048")
)

# How often to emit a "still waiting for the model" heartbeat during a long model
# call, so a slow local model reads as working rather than a frozen prompt.
_HEARTBEAT_INTERVAL_SECONDS: int = int(_os.environ.get("SHAMSU_HEARTBEAT_SECONDS", "15"))

def _env_int_at_least(name: str, default: int, minimum: int) -> int:
    raw = _os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


# Interactive-chat context window. Local Ollama latency is dominated by prompt
# prefill; filling a 32k window before every edit made small models appear
# frozen. Keep ordinary tool-loop turns tighter by default. Override with
# SHAMSU_CHAT_MAX_CTX on machines that can comfortably prefill larger prompts.
_CHAT_MAX_CTX: int = _env_int_at_least("SHAMSU_CHAT_MAX_CTX", 12288, 6144)
_CHAT_SUMMARY_BUDGET_TOKENS = 512
_CHAT_PROMPT_TARGET_FRACTION = float(_os.environ.get("SHAMSU_CHAT_PROMPT_TARGET_FRACTION", "0.70"))
_CHAT_HARD_TRIM_MARKER = "\n\n[...context hard-trimmed by SHAMSU to keep the local model responsive...]\n\n"

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
# Bounded strict-repair iterations when an autonomous run's verify FAILS (E1).
# The repair machinery existed (freeform/full_pipeline) but the chat loop only
# ever reported failure. SHAMSU_AUTO_REPAIR=0 restores report-only.
_AUTO_REPAIR_ENABLED = _os.environ.get("SHAMSU_AUTO_REPAIR", "1").strip().lower() not in {
    "0",
    "false",
    "off",
    "no",
}

# Repair iterations allowed per failed verify. `RepairLoop` stops as soon as no
# actionable error remains, so a first-attempt fix still costs exactly one pass
# and this ceiling is only ever paid on the sad path. It must stay small: the
# point is to rescue a one-line syntax error at the end of a long autonomous
# run, not to let a model grind at a problem it cannot solve.
_AUTO_REPAIR_MAX_ATTEMPTS = 3

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
- PREFER write_file with the COMPLETE file content. This is the default for creating a file AND
  for changing one. Read the file first when it exists, then re-emit all of it with your change
  applied. Whole-file writes are far more reliable than patches; a mismatched old_string wastes
  the whole turn.
- Send file content as a RAW block, never as a JSON string. Never escape quotes, backslashes, or
  newlines in code you are writing - the block is copied to disk exactly as you type it. See
  "File writes" at the end of this prompt for the exact form.
- Use edit_file ONLY when the file is too large to re-emit, or the change is a single unique
  line. Pass the exact old_string and new_string, matching exactly once (or set replace_all=true).
  If an edit_file call fails to match, do not retry it - switch to write_file with the full content.
- Use append_file when adding content at the end of an existing file. Do not fake append by
  passing an empty old_string to edit_file.
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
- Dangerous commands, sudo/admin commands, destructive deletes, and commands outside the workspace are not allowed.
- Run Python installs through run_command. SHAMSU resolves bare pip/python commands to the
  project environment and bootstraps a local .venv before installs when needed.
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
- A path the request already names is NOT ambiguous. Never ask "what is the full path to X",
  "where should X go", or "may I create X" when the request said to create X: write X exactly as
  named, relative to the workspace, creating parent directories as needed. An empty workspace is
  the normal starting state, not a reason to ask.
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
# Env-tunable because 2 is tight for a 7B: it stalls on prose more often than a
# larger model, and each correction is one cheap round, while giving up ends the
# whole milestone. Note this is an ATTEMPT budget, not a clock - the
# `tool_call_missing_after_promise` category it sets makes runs read as
# "timed_out" when nothing actually timed out.
def _os_env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(_os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


_MAX_PROSE_CORRECTIONS = _os_env_int("SHAMSU_MAX_PROSE_CORRECTIONS", 2, 1)
# How often a "which file / may I create it" non-question is answered for the
# model before its next one is forwarded to the user as a real question.
_MAX_STALL_ANSWERS = _os_env_int("SHAMSU_MAX_STALL_ANSWERS", 2, 1)
# How many empty model replies (no content, no tool call) to nudge past before
# giving up with a clear message instead of a blank "No response returned".
_MAX_EMPTY_RESPONSES = 2
# One retry is not enough: a 7B reliably spends its first response asking which
# path to use, which consumed the only recovery and ended the turn unwritten
# (observed repeatedly 2026-08-02). The correction now names the target, so the
# second attempt is the one that usually lands.
_MAX_MISSING_MUTATION_RECOVERIES = _os_env_int("SHAMSU_MAX_MUTATION_RECOVERIES", 2, 1)

# Post-eviction re-grounding. Scaled for a 7B's window: Codex re-reads 5 files on a
# 50k budget, which would be most of this model's context.
_REGROUND_MAX_FILES = _os_env_int("SHAMSU_REGROUND_MAX_FILES", 3, 0)
_REGROUND_MAX_CHARS_PER_FILE = _os_env_int("SHAMSU_REGROUND_MAX_CHARS", 2400, 200)

# Above this share of assistant PROSE in an evicted span, the structured digest
# would drop real reasoning and the LLM summary tier runs instead.
_STRUCTURED_COMPACT_PROSE_RATIO = 0.25


def _load_arguments_or_empty(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}

_EMPTY_RESPONSE_CORRECTION = (
    "You returned an empty response. Do not return nothing. Either call a tool to make "
    "progress, or write the answer/code directly, or state plainly what input you need."
)

# Tools whose failed parse means a MUTATION was attempted and lost, as opposed to
# a turn that never tried to change anything.
_MUTATION_TOOL_NAMES = frozenset({"write_file", "append_file", "edit_file", "file.patch"})
# Failure kinds that are a mutation attempt even when `tool` was not recoverable.
_MUTATION_FAILURE_KINDS = frozenset(
    {
        "raw_envelope_bad_path",
        "raw_envelope_empty_body",
        "raw_envelope_fence_collision",
        "edit_envelope_without_search_replace",
        "fence_body_not_json",
        "json_decode",
        "json_truncated",
    }
)


class _RetryEscalation:
    """Break the determinism trap.

    At temperature 0.1 an unchanged prompt reproduces byte-identical output, so a
    retry that only re-nags is a guaranteed wasted round: 2026-08-03 burned all
    three mutation rounds on the same broken call, byte for byte, then spent the
    remaining circuit-breaker budget getting nowhere. Every identical response
    escalates the STRATEGY instead of repeating it.
    """

    def __init__(self) -> None:
        self.last_hash = ""
        self.level = 0

    def observe(self, raw: str) -> int:
        digest = hashlib.sha256((raw or "").encode("utf-8", "replace")).hexdigest()
        if digest == self.last_hash:
            self.level += 1
        else:
            self.level = 0
        self.last_hash = digest
        return self.level

    def sampling_override(self, round_index: int) -> dict[str, Any] | None:
        """Sampling that will actually produce a DIFFERENT response.

        Only fires once a repeat is proven, so ordinary runs stay deterministic
        and evals stay reproducible.
        """
        if self.level < 1:
            return None
        return {
            "temperature": 0.7,
            "top_p": 0.95,
            "seed": 7919 * (round_index + 1),
        }


def _first_mutation_parse_failure(
    failures: Iterable[ParseFailure],
) -> ParseFailure | None:
    """The first failure that represents a LOST mutation, not a repaired one.

    ``repaired`` failures are informational - the call went through - so they
    must never trigger a correction.
    """
    for failure in failures:
        if failure.repaired:
            continue
        if failure.tool in _MUTATION_TOOL_NAMES or failure.kind in _MUTATION_FAILURE_KINDS:
            return failure
    return None


def _unparseable_tool_call_correction(failure: ParseFailure, target: str) -> str:
    """The correction for a turn that DID emit a mutation call SHAMSU could not read.

    Deliberately distinct from the missing-mutation nag. Telling a model that
    emitted a complete, correct write_file call to "stop returning prose" is a
    lie it cannot act on, and at temperature 0.1 it answers by re-emitting the
    identical broken call (2026-08-03: three rounds, byte-identical, zero files).
    """
    tool = failure.tool or "write_file"
    path = failure.path or target or "<path>"
    reason = (
        "Your call ended mid-payload - it was cut off before the JSON closed."
        if failure.kind == "json_truncated"
        else (
            "File content inside a JSON string has to escape every \" and every "
            "\\, and yours escaped some but not all of them. There is no reliable "
            "way to do that by hand, so do not retry the JSON form."
        )
    )
    detail = f"\n\nThe exact parser error was: {failure.error}" if failure.error else ""
    return (
        f"Your last response DID contain a {tool} call, but SHAMSU could not parse it, "
        "so nothing was written. This is an encoding failure, not a planning failure - "
        f"your plan was fine.{detail}\n\n{reason}\n\n"
        "Send the file as a raw block instead. No JSON, no escaping:\n\n"
        f"```\n# {tool}: {path}\n<the complete file content, exactly as it must appear on disk>\n```\n\n"
        "Everything between the header line and the closing fence is written verbatim. "
        "Reply with that block and nothing else."
    )


def _unparseable_mutation_final(failure: ParseFailure, artifact_path: str = "") -> str:
    """The user-facing result. Says what actually broke, not "returned prose"."""
    tool = failure.tool or "mutation"
    path = failure.path or "the requested file"
    reason = (
        f" ({failure.error})" if failure.error else ""
    )
    evidence = (
        f" The full raw response was saved to {artifact_path}." if artifact_path else ""
    )
    return (
        f"I could not complete the change. The model produced a {tool} call for {path} "
        f"that SHAMSU could not parse{reason}, so no file was written. This is a "
        f"tool-call encoding failure, not a missing plan.{evidence}"
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
    {"write_file", "edit_file", "append_file", "move_file", "delete_file", "file.patch"}
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
TIMEOUT_CONNECT = TimeoutCategory.CONNECT_TIMEOUT.value
TIMEOUT_FIRST_TOKEN = TimeoutCategory.FIRST_TOKEN_TIMEOUT.value
TIMEOUT_TOKEN_IDLE = TimeoutCategory.TOKEN_IDLE_TIMEOUT.value
TIMEOUT_TOTAL_GENERATION = TimeoutCategory.TOTAL_GENERATION_TIMEOUT.value
TIMEOUT_TOOL = TimeoutCategory.TOOL_TIMEOUT.value
TIMEOUT_STEP = TimeoutCategory.STEP_TIMEOUT.value
TIMEOUT_TASK = TimeoutCategory.TASK_TIMEOUT.value
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
    run_id: str = ""
    task_id: str = ""
    status: RunStatus = RunStatus.COMPLETED
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
        hydrate_history: bool = True,
        verify_changes: bool = True,
        use_model_compaction: bool = True,
        original_user_request: str = "",
        run_id: str | None = None,
        max_runtime_seconds: float | None = None,
        runtime_state_store: RuntimeStateStore | None = None,
    ) -> None:
        _validate_local_llm_url(base_url)
        self.workspace_root = Path(workspace_root).resolve()
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self.timeout_config = TimeoutConfig.from_env()
        self.run_id = run_id or (
            action_ledger.run_id if action_ledger is not None else f"agentrun-{uuid.uuid4().hex[:12]}"
        )
        self.max_runtime_seconds = (
            float(max_runtime_seconds)
            if max_runtime_seconds is not None
            else float(self.timeout_config.task_timeout)
        )
        self.runtime_state_store = runtime_state_store or RuntimeStateStore(self.workspace_root)
        self.runtime_task_id = f"task-{self.run_id}"
        self.model_name = model_name or model_for_role(_CHAT_EXECUTOR_ROLE)
        self.prompt_profile = prompt_profile_for_model(self.model_name)
        # Capability flags drive the model I/O boundary: whether to hand this
        # model a native tools schema (vs. an in-prompt protocol + salvager) and
        # whether to ask it to `think` so reasoning stays out of the answer.
        self._supports_native_tools = model_supports_native_tools(self.model_name)
        # Per-role, not per-model: the executor role is real work, so it keeps its
        # thinking channel, but the gate is now the same one the mechanical roles
        # (router/classifier) go through in llm/manager.
        self._is_reasoning = role_should_think(_CHAT_EXECUTOR_ROLE, self.model_name)
        self.llm = llm or LLMManager(session_logger=session_logger, action_ledger=action_ledger)
        self.context_builder = context_builder or ContextBuilder()
        self.client = client or _default_ollama_client(base_url, self.timeout_config)
        self.tools = tools or AgentToolRegistry(self.workspace_root, session_logger=session_logger)
        use_logical_tools = getattr(self.tools, "use_logical_tools", None)
        if callable(use_logical_tools):
            use_logical_tools(True)
        self.tool_dispatcher = ToolDispatcher(self.tools)
        self.context_compiler = ContextCompiler(
            self._messages_within_budget,
            store=self.runtime_state_store,
            task_id_getter=lambda: self.runtime_task_id,
            workspace_root=self.workspace_root,
            system_prompt_getter=lambda: self.state.system_prompt,
            allowed_tools_getter=self.tools.tool_schemas,
            recent_messages_getter=lambda: [
                message.to_ollama() for message in self.state.all_messages[-8:]
            ],
            trace=lambda event, message, data: self._emit_trace(
                event,
                message,
                data or {},
                level="verbose",
            ),
        )
        self.change_verifier = ChangeVerifier(
            self.workspace_root,
            command_runner=self.tools.command_runner,
            session_logger=self.session_logger,
        )
        self.failure_tracker = FailureTracker(
            self.runtime_state_store,
            self.runtime_task_id,
            step_id_getter=self._current_failure_step_id,
        )
        self.step_execution_limits = StepExecutionLimits()
        self.executor = AgentExecutor(
            self._run_inner,
            step_runner=self.run_step,
            limits=self.step_execution_limits,
        )
        self.repair_recorder = RepairRecorder(self.runtime_state_store, self.runtime_task_id)
        # Names the salvager is allowed to recover calls for (a JSON blob naming
        # an unregistered "tool" is treated as prose, not a call).
        model_tool_names = getattr(self.tools, "model_tool_names", None)
        if callable(model_tool_names):
            self._registered_tool_names = {name for name in model_tool_names() if name}
        else:
            self._registered_tool_names = {
                str((schema.get("function") or {}).get("name") or "")
                for schema in self.tools.tool_schemas()
            }
        self.agent_planner = AgentPlanner(
            store=self.runtime_state_store,
            tool_policy=self.tools,
            registered_tool_names=self._registered_tool_names,
            run_id=self.run_id,
            task_id=self.runtime_task_id,
        )
        # Optional hook to surface live tool activity (e.g. "Writing game.js")
        # to the REPL while the loop runs. None keeps the loop silent (tests).
        self.on_activity = on_activity
        self.on_trace = on_trace
        self.progress = progress
        self.budget_manager = budget_manager
        self.audit = audit
        # The prompt WITHOUT project instructions. Those are appended fresh each
        # turn by _refresh_system_prompt so an edit to SHAMSU.md mid-session takes
        # effect, and so they can never be paraphrased away by the rolling summary.
        self._base_system_prompt = _system_prompt(
            self.workspace_root,
            include_tool_protocol=not self._supports_native_tools,
            # A read-only turn must not be taught to write, and skipping it
            # returns ~250 tokens to a 7B's window.
            include_raw_write_protocol=not read_only,
            profile=self.prompt_profile,
            phase=ExecutionPhase.EXPLORE,
            available_tools=(),
        )
        self.state = state or ChatState(
            self._base_system_prompt + load_project_instructions(self.workspace_root),
            session_logger=session_logger,
            hydrate=hydrate_history,
        )
        self.long_running = long_running
        self.max_tool_rounds = LONG_RUNNING_MAX_TOOL_ROUNDS if long_running else max_tool_rounds
        # The user explicitly forbade file changes ("do not modify files").
        # Propagated to the tool registry, which denies mutating tools outright
        # regardless of approval mode - an instruction, not a preference.
        self.read_only = read_only
        self.use_long_term_memory = use_long_term_memory
        self.use_planner = use_planner
        self.verify_changes = verify_changes
        self.use_model_compaction = use_model_compaction
        self._last_context_evicted = False
        # The clean CURRENT user request, when the caller wraps it in an
        # internal contract (composite step, PRD repair). A pending ask_user
        # must resume from this - resuming from the internal wrapper re-routes
        # contract text as if the user typed it (observed live 2026-08-01).
        self.original_user_request = str(original_user_request or "")
        if read_only:
            self.tools.set_read_only(True)
        self.markdown_fallback = MarkdownWriteFallback(self.tools)

    def _refresh_system_prompt(self) -> None:
        """Re-read the workspace's standing instructions before each model call.

        Per turn, not per session, on purpose: a rolling summary is a lossy
        paraphrase that degrades every time it is folded, whereas re-reading the
        file keeps the rules byte-exact for the whole run no matter how much
        history gets evicted.
        """
        phase, current_step, available_tools = self._prompt_runtime_context()
        self._base_system_prompt = _system_prompt(
            self.workspace_root,
            include_tool_protocol=not self._supports_native_tools,
            include_raw_write_protocol=not self.read_only,
            profile=self.prompt_profile,
            phase=phase,
            current_step=current_step,
            available_tools=available_tools,
        )
        self.state.set_system_prompt(
            self._base_system_prompt + load_project_instructions(self.workspace_root)
        )

    def _prompt_runtime_context(self) -> tuple[ExecutionPhase, Any, tuple[str, ...]]:
        phase = ExecutionPhase.EXPLORE
        current_step = None
        try:
            task = self.runtime_state_store.load_task(self.runtime_task_id)
            if task is not None:
                phase = normalize_phase(task.current_phase)
                current_step = self.runtime_state_store.current_active_step(task.task_id)
        except Exception:
            current_step = None
        try:
            available_tools = tuple(
                sorted(
                    str((schema.get("function") or {}).get("name") or "")
                    for schema in self.tools.tool_schemas()
                    if str((schema.get("function") or {}).get("name") or "")
                )
            )
        except Exception:
            available_tools = ()
        return phase, current_step, available_tools

    def _regrounding_block(self, mutated_paths: Sequence[str]) -> str:
        """Current on-disk contents of the files this run most recently changed.

        Injected only after eviction. Once the tool results that carried a write
        are trimmed away, the model's only remaining knowledge of its own work is
        the rolling summary - a paraphrase - so it starts re-deriving file state
        and re-editing from memory. Codex re-reads recently edited files after
        every compaction for exactly this reason; the counts here are scaled for a
        7B window rather than a 200k one.

        Reads from disk, not from the transcript, so the model sees what the file
        ACTUALLY contains after every write, revert, and external edit.
        """
        if not mutated_paths:
            return ""
        sections: list[str] = []
        for relative in list(mutated_paths)[-_REGROUND_MAX_FILES:]:
            try:
                target = (self.workspace_root / relative).resolve()
                target.relative_to(self.workspace_root)
                if not target.is_file():
                    continue
                body = target.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue
            if len(body) > _REGROUND_MAX_CHARS_PER_FILE:
                body = (
                    body[:_REGROUND_MAX_CHARS_PER_FILE].rstrip()
                    + f"\n[...truncated at {_REGROUND_MAX_CHARS_PER_FILE} chars]"
                )
            sections.append(f"--- {relative} ---\n{body.rstrip()}")
        if not sections:
            return ""
        return (
            "Current on-disk contents of the files you have changed this run. "
            "Earlier turns were trimmed from this conversation, so trust THIS over "
            "anything you remember writing:\n\n" + "\n\n".join(sections)
        )

    async def _messages_within_budget(
        self, num_ctx: int, mutated_paths: Sequence[str] = ()
    ) -> list[dict[str, Any]]:
        """Budget-aware replacement for the flat 30-message cap: keep the system
        prompt plus the largest recent suffix of the conversation that fits the
        model's window, folding older evicted turns into a compact rolling
        summary instead of silently dropping them.

        When eviction happens, the summary is followed by the current on-disk
        contents of recently changed files, so the model is grounded in real bytes
        rather than in a paraphrase of its own earlier writes.
        """
        reserve = RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
        usable = max(1, num_ctx - reserve)
        history_budget = max(1, usable - _CHAT_SUMMARY_BUDGET_TOKENS)
        target_budget = max(1, int(usable * max(0.25, min(1.0, _CHAT_PROMPT_TARGET_FRACTION))))
        history_budget = min(history_budget, target_budget)
        evicted_anything = False
        tail, start_abs = self.state.select_for_budget(history_budget, count_tokens)
        if start_abs > 1:
            evicted_anything = True
            pending = self.state.newly_evicted(start_abs)
            if pending:
                # Two-tier: try the deterministic digest before spending a model
                # round-trip, and only summarize when real reasoning would be lost.
                summary = self._structured_compact(self.state.rolling_summary, pending)
                if summary:
                    self._emit_trace(
                        "context.compacted",
                        "Compacted evicted turns from structured state, without a model call.",
                        {"messages": len(pending)},
                        level="verbose",
                    )
                elif self.use_model_compaction:
                    summary = await self._summarize_evicted(
                        self.state.rolling_summary, pending, _CHAT_SUMMARY_BUDGET_TOKENS
                    )
                else:
                    summary = self._deterministic_compaction_fallback(
                        self.state.rolling_summary, pending
                    )
                    self._emit_trace(
                        "context.compacted",
                        "Compacted evicted turns deterministically; model summary disabled.",
                        {"messages": len(pending)},
                        level="verbose",
                    )
                self.state.update_rolling_summary(summary, start_abs)
        messages = self.state.build_ollama_messages(tail, include_summary=start_abs > 1)
        if evicted_anything:
            grounding = self._regrounding_block(mutated_paths)
            if grounding:
                # After the summary, before the tail: it is reference material for
                # the recent turns, not part of the conversation.
                messages.insert(
                    len(messages) - len(tail), {"role": "system", "content": grounding}
                )
                self._emit_trace(
                    "context.regrounded",
                    f"Re-read {min(len(mutated_paths), _REGROUND_MAX_FILES)} changed "
                    "file(s) from disk after context eviction.",
                    {"files": list(mutated_paths)[-_REGROUND_MAX_FILES:]},
                        level="verbose",
                )
        messages, hard_trimmed = self._hard_trim_messages(messages, num_ctx)
        self._last_context_evicted = evicted_anything or hard_trimmed
        return messages

    def _hard_trim_messages(
        self, messages: list[dict[str, Any]], num_ctx: int
    ) -> tuple[list[dict[str, Any]], bool]:
        reserve = RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
        target_tokens = max(1024, int(max(1, num_ctx - reserve) * max(0.25, min(1.0, _CHAT_PROMPT_TARGET_FRACTION))))
        trimmed = [dict(message) for message in messages]

        def total_tokens() -> int:
            return count_tokens("\n".join(str(message.get("content", "")) for message in trimmed))

        changed = False
        # Drop oldest non-system, non-final messages first. The final user turn
        # carries the current task; the system prompt carries the tool protocol.
        while len(trimmed) > 2 and total_tokens() > target_tokens:
            del trimmed[1]
            changed = True

        for _ in range(8):
            current_total = total_tokens()
            if current_total <= target_tokens:
                break
            candidates = [
                (index, count_tokens(str(message.get("content", ""))))
                for index, message in enumerate(trimmed)
                if str(message.get("content", ""))
            ]
            if not candidates:
                break
            # Prefer trimming non-system context before the system prompt.
            candidates.sort(key=lambda item: (item[0] != 0, item[1]), reverse=True)
            index, token_count = candidates[0]
            if token_count <= 256:
                break
            excess = max(1, current_total - target_tokens)
            new_budget = max(256, token_count - excess - 256)
            content = str(trimmed[index].get("content", ""))
            new_content = self._truncate_content_to_token_budget(content, new_budget)
            if new_content == content:
                break
            trimmed[index]["content"] = new_content
            changed = True

        if changed:
            self._emit_trace(
                "context.compacted",
                "Hard-trimmed chat context before the model call.",
                {"messages": len(messages), "target_tokens": target_tokens},
                level="verbose",
            )
        return trimmed, changed

    def _truncate_content_to_token_budget(self, content: str, budget_tokens: int) -> str:
        if count_tokens(content) <= budget_tokens:
            return content
        char_budget = max(800, budget_tokens * 4)
        if len(content) <= char_budget:
            return content
        marker = _CHAT_HARD_TRIM_MARKER
        keep = max(200, char_budget - len(marker))
        head = max(100, int(keep * 0.7))
        tail = max(100, keep - head)
        return content[:head].rstrip() + marker + content[-tail:].lstrip()

    def _deterministic_compaction_fallback(
        self, prior_summary: str, evicted: list[Any]
    ) -> str:
        lines: list[str] = []
        if prior_summary.strip():
            lines.append(prior_summary.strip())
        user_requests: list[str] = []
        assistant_notes = 0
        tool_results = 0
        for message in evicted:
            role = str(getattr(message, "role", "") or "")
            content = " ".join(str(getattr(message, "content", "") or "").split())
            if role == "user" and content and not content.startswith("("):
                user_requests.append(content[:200])
            elif role == "assistant" and content and not getattr(message, "tool_calls", None):
                assistant_notes += 1
            elif role == "tool":
                tool_results += 1
        if user_requests:
            lines.append(f"- earlier user requests: {'; '.join(dict.fromkeys(user_requests))}")
        details: list[str] = []
        if assistant_notes:
            details.append(f"{assistant_notes} assistant note(s)")
        if tool_results:
            details.append(f"{tool_results} tool result(s)")
        details_text = ", ".join(details) if details else f"{len(evicted)} message(s)"
        lines.append(f"- compacted deterministically: {details_text}; model summary was disabled.")
        return "\n".join(lines)

    def _structured_compact(self, prior_summary: str, evicted: list[Any]) -> str:
        """Tier 1 compaction: a deterministic digest of what the evicted turns DID.

        Most evicted content in a build run is mechanical tool traffic - reads,
        writes, commands and their results - and summarizing that with a model is
        both a wasted round-trip on a local 7B and lossy: each fold paraphrases the
        previous paraphrase, so paths and command names drift. Structured facts do
        not drift.

        Returns ``""`` when the evicted span carries enough genuine prose that a
        digest would lose reasoning, and the LLM tier should run instead.
        """
        wrote: list[str] = []
        read: list[str] = []
        ran: list[str] = []
        asked: list[str] = []
        prose_chars = 0
        total_chars = 0
        for message in evicted:
            content = str(getattr(message, "content", "") or "")
            role = str(getattr(message, "role", ""))
            total_chars += len(content)
            if role == "user":
                if not content.startswith("("):
                    asked.append(" ".join(content.split())[:200])
                continue
            for call in getattr(message, "tool_calls", None) or []:
                function = (call or {}).get("function") or {}
                name = str(function.get("name") or "")
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    arguments = _load_arguments_or_empty(arguments)
                arguments = arguments if isinstance(arguments, dict) else {}
                target = str(arguments.get("filepath") or arguments.get("command") or "")
                if name in _MUTATION_TOOL_NAMES and target:
                    wrote.append(target)
                elif name == "read_file" and target:
                    read.append(target)
                elif name == "run_command" and target:
                    ran.append(target[:120])
            if role == "assistant" and not getattr(message, "tool_calls", None):
                prose_chars += len(content)
        if total_chars and prose_chars / total_chars > _STRUCTURED_COMPACT_PROSE_RATIO:
            return ""
        lines: list[str] = []
        if prior_summary.strip():
            lines.append(prior_summary.strip())
        if asked:
            lines.append(f"- asked: {'; '.join(dict.fromkeys(asked))}")
        if wrote:
            lines.append(f"- wrote: {', '.join(dict.fromkeys(wrote))}")
        if read:
            lines.append(f"- read: {', '.join(dict.fromkeys(read))}")
        if ran:
            lines.append(f"- ran: {'; '.join(dict.fromkeys(ran))}")
        if not lines:
            return ""
        return "\n".join(lines)

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
        *,
        options_override: dict[str, Any] | None = None,
        control: ControlledRun | None = None,
    ) -> Any:
        """Call the model with layer-specific timeout diagnostics.

        ``options_override`` raises sampling for a retry that would otherwise be
        byte-identical (see :class:`_RetryEscalation`).
        """

        async def _beat() -> None:
            elapsed = 0
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
                elapsed += _HEARTBEAT_INTERVAL_SECONDS
                if self.on_activity:
                    self.on_activity(
                        f"still waiting for the model... {elapsed}s "
                        "(Ollama may be loading, queued, or generating; "
                        f"first-token timeout {self.timeout_config.first_token_timeout:.0f}s, "
                        f"token-idle timeout {self.timeout_config.token_idle_timeout:.0f}s)"
                    )

        chat_kwargs: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": 0.1,
                "num_ctx": num_ctx,
                **(options_override or {}),
            },
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
        model_task: asyncio.Task[Any] | None = None
        try:
            coro = self._chat_with_layered_timeouts(chat_kwargs, control, round_index)
            model_task = asyncio.create_task(coro)
            if control is not None:
                control.current_model_task = model_task
                control.record_event("model_call_started", round=round_index, model=self.model_name)
            response = await model_task
            if control is not None:
                control.record_event("model_call_finished", round=round_index, model=self.model_name)
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
        except asyncio.CancelledError:
            if control is not None:
                control.record_event(
                    "model_call_cancelled",
                    round=round_index,
                    cancel_requested=control.cancel_event.is_set(),
                    feedback_pending=control.feedback_queue.qsize(),
                )
            raise
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
            if control is not None and control.current_model_task is model_task:
                control.current_model_task = None
            beat.cancel()

    async def _chat_with_layered_timeouts(
        self,
        chat_kwargs: dict[str, Any],
        control: ControlledRun | None,
        round_index: int,
    ) -> Any:
        streaming_kwargs = {**chat_kwargs, "stream": True}
        try:
            raw_stream = self.client.chat(**streaming_kwargs)
        except TypeError:
            streaming_kwargs.pop("think", None)
            try:
                raw_stream = self.client.chat(**streaming_kwargs)
            except TypeError:
                return await self._chat_non_streaming_with_timeout(chat_kwargs, control, round_index)
        try:
            stream = await self._await_generation_boundary(
                raw_stream,
                TimeoutCategory.FIRST_TOKEN_TIMEOUT,
                control,
                round_index,
            )
        except TypeError:
            return await self._chat_non_streaming_with_timeout(chat_kwargs, control, round_index)
        if isinstance(stream, dict) or not hasattr(stream, "__aiter__"):
            return stream
        return await self._collect_streaming_chat(stream, control, round_index)

    async def _chat_non_streaming_with_timeout(
        self,
        chat_kwargs: dict[str, Any],
        control: ControlledRun | None,
        round_index: int,
    ) -> Any:
        fallback_kwargs = {**chat_kwargs, "stream": False}
        try:
            raw_response = self.client.chat(**fallback_kwargs)
        except TypeError:
            fallback_kwargs.pop("think", None)
            raw_response = self.client.chat(**fallback_kwargs)
        category = (
            TimeoutCategory.TOTAL_GENERATION_TIMEOUT
            if self.timeout_config.total_generation_timeout > 0
            else TimeoutCategory.FIRST_TOKEN_TIMEOUT
        )
        return await self._await_generation_boundary(raw_response, category, control, round_index)

    async def _collect_streaming_chat(
        self,
        stream: Any,
        control: ControlledRun | None,
        round_index: int,
    ) -> dict[str, Any]:
        chunks: list[str] = []
        thinking_chunks: list[str] = []
        tool_calls: list[Any] = []
        started = False
        total_deadline = (
            time.monotonic() + self.timeout_config.total_generation_timeout
            if self.timeout_config.total_generation_timeout > 0
            else None
        )
        iterator = stream.__aiter__()
        while True:
            category = TimeoutCategory.TOKEN_IDLE_TIMEOUT if started else TimeoutCategory.FIRST_TOKEN_TIMEOUT
            wait_category, seconds = self._timeout_for_generation_wait(category, control, total_deadline)
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=seconds)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError as exc:
                raise ShamsuTimeoutError(
                    wait_category,
                    seconds,
                    self._timeout_message_for_category(wait_category, seconds),
                ) from exc
            message = _message_from_response(chunk)
            content = str(_get(message, "content", "") or "")
            thinking = str(_get(message, "thinking", "") or "")
            current_tool_calls = _get(message, "tool_calls", []) or []
            if content or thinking or current_tool_calls:
                started = True
            if content:
                chunks.append(content)
            if thinking:
                thinking_chunks.append(thinking)
            if current_tool_calls:
                tool_calls.extend(current_tool_calls)
            if control is not None:
                control.record_event(
                    "model_stream_chunk",
                    round=round_index,
                    has_content=bool(content),
                    has_tool_calls=bool(current_tool_calls),
                )
            if bool(_get(chunk, "done", False)):
                break
        response: dict[str, Any] = {
            "message": {
                "role": "assistant",
                "content": "".join(chunks),
                "tool_calls": tool_calls,
            }
        }
        if thinking_chunks:
            response["message"]["thinking"] = "".join(thinking_chunks)
        return response

    async def _await_generation_boundary(
        self,
        value: Any,
        category: TimeoutCategory,
        control: ControlledRun | None,
        round_index: int,
    ) -> Any:
        wait_category, seconds = self._timeout_for_generation_wait(category, control, None)
        try:
            if hasattr(value, "__await__"):
                return await asyncio.wait_for(value, timeout=seconds)
            return value
        except asyncio.TimeoutError as exc:
            raise ShamsuTimeoutError(
                wait_category,
                seconds,
                self._timeout_message_for_category(wait_category, seconds),
            ) from exc
        except Exception as exc:
            lowered = f"{type(exc).__name__}: {exc}".lower()
            if "connect" in lowered or "connection" in lowered:
                raise ShamsuTimeoutError(
                    TimeoutCategory.CONNECT_TIMEOUT,
                    self.timeout_config.connect_timeout,
                    self._timeout_message_for_category(
                        TimeoutCategory.CONNECT_TIMEOUT,
                        self.timeout_config.connect_timeout,
                    ),
                ) from exc
            raise
        finally:
            if control is not None:
                control.record_event(
                    "model_timeout_boundary_checked",
                    round=round_index,
                    category=wait_category.value,
                    seconds=seconds,
                )

    def _timeout_for_generation_wait(
        self,
        category: TimeoutCategory,
        control: ControlledRun | None,
        total_deadline: float | None,
    ) -> tuple[TimeoutCategory, float]:
        candidates: list[tuple[TimeoutCategory, float]] = []
        if category == TimeoutCategory.FIRST_TOKEN_TIMEOUT:
            candidates.append((category, self.timeout_config.first_token_timeout))
        elif category == TimeoutCategory.TOKEN_IDLE_TIMEOUT:
            candidates.append((category, self.timeout_config.token_idle_timeout))
        elif category == TimeoutCategory.TOTAL_GENERATION_TIMEOUT:
            candidates.append((category, self.timeout_config.total_generation_timeout))
        else:
            candidates.append((category, self.timeout_config.connect_timeout))
        if total_deadline is not None:
            candidates.append((TimeoutCategory.TOTAL_GENERATION_TIMEOUT, max(0.0, total_deadline - time.monotonic())))
        if control is not None:
            remaining = time_remaining(control)
            if remaining is not None:
                candidates.append((TimeoutCategory.TASK_TIMEOUT, remaining))
        chosen_category, seconds = min(candidates, key=lambda item: item[1])
        if seconds <= 0:
            raise ShamsuTimeoutError(
                chosen_category,
                0.0,
                self._timeout_message_for_category(chosen_category, 0.0),
            )
        return chosen_category, max(0.001, seconds)

    def _timeout_message_for_category(self, category: TimeoutCategory, seconds: float) -> str:
        if category == TimeoutCategory.CONNECT_TIMEOUT:
            return f"Could not connect to the local model transport within {seconds:.0f}s."
        if category == TimeoutCategory.FIRST_TOKEN_TIMEOUT:
            return f"The model did not start generating within {seconds:.0f}s."
        if category == TimeoutCategory.TOKEN_IDLE_TIMEOUT:
            return f"The model started generating, then produced no token for {seconds:.0f}s."
        if category == TimeoutCategory.TOTAL_GENERATION_TIMEOUT:
            return f"The optional total generation cap of {seconds:.0f}s was reached."
        if category == TimeoutCategory.TASK_TIMEOUT:
            return "The overall task deadline was reached."
        if category == TimeoutCategory.STEP_TIMEOUT:
            return "The current plan step deadline was reached."
        return f"The tool deadline of {seconds:.0f}s was reached."

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

    def _cancelled_result(self, control: ControlledRun, round_index: int = 0) -> AgentLoopResult:
        final = "Run cancelled."
        self.state.append_assistant(final)
        control.record_event("run_cancelled_checkpoint", round=round_index)
        self._audit_final(final)
        return AgentLoopResult(
            final=final,
            tool_rounds=round_index,
            stopped=True,
            run_id=self.run_id,
            status=RunStatus.CANCELLED,
        )

    def _timeout_result(
        self,
        control: ControlledRun,
        round_index: int,
        category: str = TIMEOUT_TASK,
        *,
        seconds: float = 0.0,
    ) -> AgentLoopResult:
        try:
            timeout_category = TimeoutCategory(category)
        except ValueError:
            timeout_category = TimeoutCategory.TASK_TIMEOUT
        final = _timeout_message(timeout_category.value, int(seconds))
        layer = "step" if timeout_category == TimeoutCategory.STEP_TIMEOUT else "task"
        action = "plan.step" if timeout_category == TimeoutCategory.STEP_TIMEOUT else "task.run"
        self._record_failure(
            failure_type_for_timeout(timeout_category),
            action=action,
            evidence=[timeout_category.value],
            detail=timeout_failure_detail(
                timeout_category,
                seconds=seconds,
                layer=layer,
                round=round_index,
            ),
        )
        self.state.append_assistant(final)
        control.record_event("run_deadline_reached", round=round_index, timeout_category=timeout_category.value)
        self._audit_final(final)
        return AgentLoopResult(
            final=final,
            tool_rounds=round_index,
            stopped=True,
            timeout_category=timeout_category.value,
            run_id=self.run_id,
            status=RunStatus.TIMED_OUT,
        )

    def _inject_feedback(self, control: ControlledRun) -> bool:
        injected = False
        while True:
            try:
                text = control.feedback_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.state.append_user(
                "High-priority user feedback for this active run:\n"
                f"{text}\n\n"
                "Incorporate this feedback into the current task state before continuing."
            )
            control.record_event("feedback_injected", text_preview=text[:500])
            injected = True
        if injected:
            control.feedback_event.clear()
        return injected

    def _initialize_runtime_task(self, user_input: str, control: ControlledRun) -> TaskState:
        self.runtime_state_store.create_run(
            self.run_id,
            status=RunStatus.RUNNING,
            deadline_at=control.deadline_at,
        )
        state = self.runtime_state_store.create_task(
            run_id=self.run_id,
            task_id=self.runtime_task_id,
            user_request=user_input,
            project_id=self.workspace_root.name,
        )
        state.status = RunStatus.RUNNING
        state.current_phase = ExecutionPhase.EXPLORE.value
        self.tools.set_phase(ExecutionPhase.EXPLORE, task_risk="low")
        return self.runtime_state_store.save_task(state, checkpoint_kind="task_started")

    def _checkpoint_task_status(
        self,
        status: RunStatus,
        phase: str,
        checkpoint_kind: str,
    ) -> None:
        try:
            self.runtime_state_store.update_task_status(
                self.runtime_task_id,
                status,
                phase=phase,
                checkpoint_kind=checkpoint_kind,
            )
        except Exception:
            # Runtime state must be durable, but checkpointing must not turn a
            # successful user-visible task into data loss. Tests cover the store
            # itself; production keeps going and the action ledger/session logs
            # still record the failure path.
            pass

    def _checkpoint_successful_tool(
        self,
        *,
        round_index: int,
        name: str,
        arguments: dict[str, Any],
        result: Any,
        changed_files: list[str],
    ) -> None:
        try:
            result_data = result.data if isinstance(result.data, dict) else {}
            existing = self.runtime_state_store.load_task(self.runtime_task_id)
            action_number = (existing.action_count + 1) if existing is not None else 1
            active = self.runtime_state_store.current_active_step(self.runtime_task_id)
            step_id = active.step_id if active is not None else f"round-{round_index + 1}-action-{action_number}"
            required_evidence = active.required_evidence if active is not None else []
            self.runtime_state_store.record_successful_step(
                self.runtime_task_id,
                step_id=step_id,
                tool_call={"name": name, "arguments": arguments},
                tool_result={
                    "tool": name,
                    "ok": bool(getattr(result, "ok", False)),
                    "message": str(getattr(result, "message", "")),
                    "data": result_data,
                },
                changed_files=changed_files,
                required_evidence=required_evidence,
            )
        except Exception:
            pass

    def _checkpoint_latest_observation(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> None:
        try:
            state = self.runtime_state_store.require_task(self.runtime_task_id)
            state.last_tool_call = {"name": name, "arguments": arguments}
            state.last_tool_result = self._compress_observation(
                name=name,
                arguments=arguments,
                result=result,
            )
            self.runtime_state_store.save_task(state, checkpoint_kind="latest_observation")
        except Exception:
            pass

    def _sync_artifacts_after_tool_result(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> None:
        if not bool(getattr(result, "ok", False)):
            return
        data = result.data if isinstance(result.data, dict) else {}
        try:
            if name in {"read_file", "file.read"} and not bool(data.get("truncated", False)):
                path = str(
                    data.get("resolved_filepath")
                    or data.get("filepath")
                    or arguments.get("filepath")
                    or ""
                )
                content = data.get("content")
                if path and isinstance(content, str):
                    invalidate_artifacts_if_hash_mismatch(
                        self.workspace_root,
                        path,
                        hash_source_text(content),
                        source=name,
                    )
                return

            changed = self._artifact_touched_paths(name=name, arguments=arguments, data=data)
            if not changed:
                return
            mark_artifacts_stale_for_paths(
                self.workspace_root,
                changed,
                reason=f"successful tool mutation: {name}",
            )
            refresh_artifacts_for_paths(self.workspace_root, changed)
        except Exception:
            self._emit_trace(
                "artifacts.refresh_failed",
                f"Could not refresh code artifacts after {name}.",
                {"tool": name},
            )

    def _artifact_touched_paths(
        self,
        *,
        name: str,
        arguments: dict[str, Any],
        data: dict[str, Any],
    ) -> list[str]:
        mutating = name in _MUTATION_TOOL_NAMES or name in {"file.patch"}
        command_mutation = name in {"run_command", "test.run"} and bool(data.get("touched_files"))
        mcp_mutation = (
            name.startswith("mcp__")
            and not bool(data.get("read_only", False))
            and bool(data.get("touched_files"))
        )
        if not (mutating or command_mutation or mcp_mutation):
            return []
        touched = [str(path) for path in data.get("touched_files", []) if str(path)]
        path = str(data.get("resolved_filepath") or data.get("filepath") or arguments.get("filepath") or "")
        if path:
            touched.append(path)
        deleted = {str(path) for path in data.get("deleted_files", [])}
        return sorted(dict.fromkeys(path for path in touched if path and path not in deleted))

    def _current_failure_step_id(self) -> str:
        try:
            active = self.runtime_state_store.current_active_step(self.runtime_task_id)
        except Exception:
            active = None
        return active.step_id if active is not None else ""

    def _start_step_controller(self, control: ControlledRun) -> StepExecutionController:
        try:
            active = self.runtime_state_store.current_active_step(self.runtime_task_id)
        except Exception:
            active = None
        step_id = active.step_id if active is not None else "unplanned-step"
        controller = StepExecutionController(
            step_id=step_id,
            limits=self.step_execution_limits,
        )
        control.record_event(
            "step_execution_started",
            step_id=step_id,
            max_actions_per_step=controller.limits.max_actions_per_step,
            max_repairs_per_step=controller.limits.max_repairs_per_step,
            max_replans_per_task=controller.limits.max_replans_per_task,
            max_consecutive_failures=controller.limits.max_consecutive_failures,
        )
        return controller

    async def run_step(
        self,
        step: Any,
        user_input: str,
        control: ControlledRun,
        limits: StepExecutionLimits | None = None,
    ) -> AgentLoopResult:
        previous_limits = self.step_execution_limits
        if limits is not None:
            self.step_execution_limits = limits
        try:
            control.record_event(
                "run_step_entered",
                step_id=str(getattr(step, "step_id", "") or "unplanned-step"),
            )
            return await self._run_inner(user_input, control)
        finally:
            self.step_execution_limits = previous_limits

    def _step_blocked_result(
        self,
        outcome: StepExecutionOutcome,
        control: ControlledRun,
        round_index: int,
        written_files: list[str],
    ) -> AgentLoopResult:
        self._mark_active_step_blocked(outcome.reason)
        final = (
            "I stopped this step because it hit the bounded step executor limit. "
            f"Reason: {outcome.reason or 'no progress'}."
        )
        self.state.append_assistant(final)
        control.record_event(
            "step_execution_blocked",
            round=round_index,
            reason=outcome.reason,
        )
        self._record_failure(
            FailureType.UNKNOWN_FAILURE,
            action="step.execute",
            evidence=[outcome.reason or "step execution blocked"],
            detail={"round": round_index, "step_limit": True},
        )
        self._audit_final(final)
        return AgentLoopResult(
            final=final,
            tool_rounds=round_index,
            stopped=True,
            changed_files=tuple(written_files),
        )

    def _mark_active_step_blocked(self, reason: str = "") -> None:
        try:
            active = self.runtime_state_store.current_active_step(self.runtime_task_id)
            if active is None:
                self._checkpoint_task_status(RunStatus.FAILED, "blocked", "step_blocked")
                return
            self.runtime_state_store.update_plan_step_status(
                self.runtime_task_id,
                active.step_id,
                PlanStepStatus.BLOCKED,
                checkpoint_kind="step_blocked",
            )
            self._checkpoint_task_status(RunStatus.RUNNING, "blocked", "step_blocked")
            self._emit_trace(
                "step.blocked",
                f"Active step blocked: {reason}",
                {"step_id": active.step_id, "reason": reason},
            )
        except Exception:
            pass

    def _apply_step_outcome(
        self,
        controller: StepExecutionController,
        outcome: StepExecutionOutcome,
        *,
        round_index: int,
        failure_policy: Any = None,
    ) -> StepExecutionOutcome:
        if outcome.decision == StepExecutionDecision.CONTINUE:
            return outcome
        if outcome.decision == StepExecutionDecision.VERIFY:
            self._checkpoint_task_status(RunStatus.RUNNING, ExecutionPhase.VERIFY.value, "step_verify")
            return outcome
        if outcome.decision == StepExecutionDecision.REPAIR:
            self._checkpoint_task_status(RunStatus.RUNNING, ExecutionPhase.REPAIR.value, "step_repair")
            self.state.append_user(
                "The last action failed but is repairable. Use the latest observation, "
                "change only related files or arguments, and do not repeat the same action unchanged."
            )
            return outcome
        if outcome.decision == StepExecutionDecision.REPLAN:
            try:
                state = self.runtime_state_store.require_task(self.runtime_task_id)
                if state.replan_count >= controller.limits.max_replans_per_task:
                    return StepExecutionOutcome(
                        StepExecutionDecision.BLOCK,
                        "replan budget exhausted",
                    )
                self.runtime_state_store.record_replan(self.runtime_task_id)
            except Exception:
                pass
            self._checkpoint_task_status(RunStatus.RUNNING, ExecutionPhase.PLAN.value, "step_replan")
            self.state.append_user(
                "The current plan appears wrong. Stop authoring and produce a corrected "
                "next-step plan using only the current task evidence."
            )
            return outcome
        if failure_policy is not None:
            self._emit_trace(
                "step.failure_policy",
                f"{outcome.decision.value}: {getattr(failure_policy.action, 'value', '')}",
                {"round": round_index, "step_id": controller.step_id},
                level="verbose",
            )
        return outcome

    def _compress_observation(self, *, name: str, arguments: dict[str, Any], result: Any) -> dict[str, Any]:
        data = result.data if isinstance(result.data, dict) else {}
        compact_data = _compact_value(data, limit=2200)
        return {
            "tool": name,
            "ok": bool(getattr(result, "ok", False)),
            "message": str(getattr(result, "message", ""))[:1000],
            "data": compact_data,
            "arguments": _compact_value(arguments, limit=1200),
        }

    def _record_failure(
        self,
        failure_type: FailureType,
        *,
        action: str = "",
        evidence: list[str] | None = None,
        detail: Any = None,
    ):
        try:
            failure, policy = self.failure_tracker.decision(
                failure_type,
                action=action,
                evidence=evidence or [],
                detail=detail,
            )
        except Exception:
            return None, None
        self._emit_trace(
            "failure.recorded",
            f"{failure.failure_type.value}: {policy.action.value}",
            {
                "failure_type": failure.failure_type.value,
                "action": failure.action,
                "retry_count": failure.retry_count,
                "recovery_action": policy.action.value,
            },
            level="verbose",
        )
        return failure, policy

    def _apply_active_step_phase(self) -> None:
        try:
            active = self.runtime_state_store.current_active_step(self.runtime_task_id)
            if active is None:
                return
            risk = _enum_value(active.risk_level)
            phase = phase_for_step(
                allowed_tools=active.allowed_tools,
                required_evidence=active.required_evidence,
                approval_required=active.approval_required,
                risk_level=risk,
            )
            self.tools.set_phase(phase, task_risk=risk)
            self._checkpoint_task_status(RunStatus.RUNNING, phase.value, "active_step_selected")
        except Exception:
            pass

    def _active_step_elapsed_seconds(self) -> float | None:
        if self.timeout_config.step_timeout <= 0:
            return None
        try:
            task = self.runtime_state_store.load_task(self.runtime_task_id)
            if task is None or not task.current_step_id:
                return None
            step = self.runtime_state_store.load_step(task.task_id, task.current_step_id)
            if step is None or not step.started_at:
                return None
            started = datetime.fromisoformat(step.started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        except Exception:
            return None

    def _active_step_timed_out(self) -> bool:
        elapsed = self._active_step_elapsed_seconds()
        return elapsed is not None and elapsed >= self.timeout_config.step_timeout

    def _mark_active_step_timed_out(self) -> None:
        try:
            task = self.runtime_state_store.require_task(self.runtime_task_id)
            if not task.current_step_id:
                return
            try:
                self.runtime_state_store.update_plan_step_status(
                    task.task_id,
                    task.current_step_id,
                    PlanStepStatus.FAILED,
                    checkpoint_kind="step_timeout",
                )
            except Exception:
                pass
            step = self.runtime_state_store.load_step(task.task_id, task.current_step_id)
            if step is not None:
                step.status = TaskStepStatus.FAILED
                self.runtime_state_store.record_step(step)
        except Exception:
            pass

    async def run(self, user_input: str) -> AgentLoopResult:
        return await RuntimeEngine(self).run(user_input)

    def _make_terminal_result(
        self,
        final: str,
        status: RunStatus,
        *,
        stopped: bool = True,
    ) -> AgentLoopResult:
        return AgentLoopResult(
            final=final,
            stopped=stopped,
            run_id=self.run_id,
            task_id=self.runtime_task_id,
            status=status,
        )

    async def _run_inner(self, user_input: str, control: ControlledRun) -> AgentLoopResult:
        original_input = user_input
        if control.cancel_event.is_set():
            return self._cancelled_result(control, 0)
        if timed_out(control):
            return self._timeout_result(control, 0)
        if self.audit:
            self.audit.log_prompt(original_input)
        self._refresh_system_prompt()
        if self.use_long_term_memory:
            user_input = self._append_long_term_memory(user_input)
        # Structural facts about the files this turn names, from the code graph.
        # Independent of Graphiti (cross-session recall): the two answer
        # different questions and one being unavailable must not mute the other.
        user_input = self._append_codebase_memory(user_input)
        self._produced_plan = False
        self._pending_upfront_question: dict[str, Any] | None = None
        if self.use_planner and (self.long_running or _CHAT_PLANNER_ENABLED):
            user_input = await self._append_plan(user_input)
            self.runtime_state_store.record_plan_created(self.runtime_task_id)
            self._apply_active_step_phase()
            if self._active_step_timed_out():
                self._mark_active_step_timed_out()
                return self._timeout_result(
                    control,
                    0,
                    TIMEOUT_STEP,
                    seconds=self.timeout_config.step_timeout,
                )
        else:
            phase = (
                ExecutionPhase.AUTHOR
                if _request_requires_workspace_change(user_input)
                else ExecutionPhase.EXPLORE
            )
            risk = "medium" if phase == ExecutionPhase.AUTHOR else "low"
            self.tools.set_phase(phase, task_risk=risk)
            self._checkpoint_task_status(RunStatus.RUNNING, phase.value, "phase_selected")
        if control.cancel_event.is_set():
            return self._cancelled_result(control, 0)
        if timed_out(control):
            return self._timeout_result(control, 0)
        self._inject_feedback(control)
        self.state.append_user(user_input, persisted_content=original_input)
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
        successful_call_signatures: set[tuple[str, str]] = set()
        successful_read_paths: set[str] = set()
        unconfirmed_failed_writes: dict[str, str] = {}
        mutation_recovery_attempts = 0
        missing_mutation_recovery_attempts = 0
        strict_repair_recovery_attempted = False
        # Files this run actually wrote (confirmed ok), for the end-of-run verify gate.
        written_files: list[str] = []
        # Times a "which file / may I" non-question was answered on the model's
        # behalf; bounded so a model that only ever asks still hands back.
        stall_answers = 0
        # The most recent read_file failure that has not yet been recovered from,
        # plus a cap on prose-only "I'll read X next" stalls after such a failure.
        last_failed_read: dict[str, Any] | None = None
        read_recovery_attempts = 0
        prose_corrections = 0
        truncation_recoveries = 0
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
        # Raised sampling for the NEXT call once a byte-identical repeat is proven.
        escalation = _RetryEscalation()
        pending_options: dict[str, Any] | None = None
        step_controller = self._start_step_controller(control)
        step_round_limit = min(self.max_tool_rounds, step_controller.limits.max_actions_per_step)
        for round_index in range(step_round_limit):
            control.iterations = round_index
            await wait_if_paused(control)
            decision = step_controller.before_model_decision()
            if decision.should_stop:
                return self._step_blocked_result(decision, control, round_index, written_files)
            if control.cancel_event.is_set():
                return self._cancelled_result(control, round_index)
            if timed_out(control):
                return self._timeout_result(control, round_index)
            if self._active_step_timed_out():
                self._mark_active_step_timed_out()
                return self._timeout_result(
                    control,
                    round_index,
                    TIMEOUT_STEP,
                    seconds=self.timeout_config.step_timeout,
                )
            self._inject_feedback(control)
            num_ctx = min(ctx_window_for_model(self.model_name), _CHAT_MAX_CTX)
            self._refresh_system_prompt()
            messages = await self.context_compiler.compile(num_ctx, written_files)
            # Show context-window usage before each model call.
            if self.budget_manager:
                _msg_text = "\n".join(str(m.get("content", "")) for m in messages)
                _budget = self.budget_manager.compute(self.model_name, "chat", _msg_text)
                _budget.compacted = self._last_context_evicted
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
            if self.progress:
                self.progress.step(f"Thinking... choosing action {round_index + 1}/{step_round_limit}")
            try:
                response = await self._chat_with_heartbeat(
                    messages,
                    num_ctx,
                    round_index,
                    options_override=pending_options,
                    control=control,
                )
                pending_options = None
            except asyncio.CancelledError:
                if control.cancel_event.is_set():
                    return self._cancelled_result(control, round_index)
                if self._inject_feedback(control):
                    pending_options = None
                    continue
                raise
            except ShamsuTimeoutError as exc:
                category = exc.category.value
                self._record_failure(
                    failure_type_for_timeout(exc.category),
                    action="model.chat",
                    evidence=[category],
                    detail=timeout_failure_detail(
                        exc.category,
                        seconds=exc.seconds,
                        layer="model",
                        round=round_index,
                        produced_plan=self._produced_plan,
                        ran_any_tool=ran_any_tool,
                    ),
                )
                final = _timeout_message(category, int(exc.seconds or 0))
                self.state.append_assistant(final)
                self._emit_trace(
                    "llm.timeout",
                    f"Model call timed out (category: {category}).",
                    {"category": category, "round": round_index, "seconds": exc.seconds},
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
            except asyncio.TimeoutError:
                if timed_out(control):
                    return self._timeout_result(control, round_index)
                category = self._timeout_category(round_index, ran_any_tool)
                final = _timeout_message(category, _MODEL_CALL_TIMEOUT_SECONDS)
                self.state.append_assistant(final)
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
            # The PRE-strip text. _strip_tool_artifacts has already removed the
            # very spans needed to diagnose a failed parse, so turn.text is
            # useless as evidence here.
            raw_response = str(_get(_message_from_response(response), "content", "") or "")
            repeat_level = escalation.observe(raw_response)
            content = turn.text
            tool_calls = [tool_call_to_message_dict(call) for call in turn.tool_calls]
            if not tool_calls:
                promised_read = _promised_read_tool_call(content)
                if promised_read is not None:
                    tool_calls = [promised_read]
                    self._emit_trace(
                        "tool.salvaged",
                        "Converted an explicit prose-only file-read promise into a read_file call.",
                        {"round": round_index, "tools": ["read_file"]},
                    )
                elif "write_file" in self._registered_tool_names and not self.read_only:
                    promised_write = _promised_write_tool_call(
                        content,
                        self.workspace_root,
                        self.original_user_request or original_input,
                    )
                    if promised_write is not None:
                        tool_calls = [promised_write]
                        self._emit_trace(
                            "tool.salvaged",
                            "Converted a prose-only mutation promise with a code fence into a write_file call.",
                            {"round": round_index, "tools": ["write_file"]},
                        )
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
            if len(tool_calls) > 1:
                tool_calls, single_action_outcome = step_controller.enforce_single_action(tool_calls)
                self._record_failure(
                    FailureType.WRONG_TOOL,
                    action="model.multiple_actions",
                    evidence=[single_action_outcome.reason],
                    detail={
                        "round": round_index,
                        "accepted_tool": _tool_call_name(tool_calls[0]) if tool_calls else "",
                    },
                )
                self._emit_trace(
                    "step.action_limited",
                    single_action_outcome.reason,
                    {"round": round_index, "accepted": _tool_call_name(tool_calls[0]) if tool_calls else ""},
                )
            self.state.append_assistant(content, tool_calls=tool_calls)
            repair_targets = self.tools.allowed_write_paths()
            should_handoff_to_strict_repair = (
                self.long_running
                and not strict_repair_recovery_attempted
                and not successful_mutation
                and len(successful_read_paths) >= 1
                and bool(repair_targets)
                and _request_is_verification_repair(original_input)
                and not tool_calls
            )
            if should_handoff_to_strict_repair:
                strict_repair_recovery_attempted = True
                handoff = await self._run_scoped_repair_handoff(repair_targets, round_index)
                if handoff is not None:
                    return handoff
            if not tool_calls and not content.strip():
                # An empty model reply (no content, no tool call) is the "No
                # response returned" the user saw. Retry with a nudge rather than
                # ending the turn on nothing.
                empty_outcome = step_controller.note_no_progress("empty model response")
                if empty_outcome.should_stop:
                    return self._step_blocked_result(empty_outcome, control, round_index, written_files)
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
                # A failed write/edit is stronger evidence than generic prose
                # that merely promises another action. Handle it first so the
                # model receives the exact failed path and a concrete recovery
                # contract instead of exhausting generic prose corrections.
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
                if _looks_like_deferred_action(content):
                    # The model said it would take a tool action ("I will read X
                    # next") but did not call a tool. Do not end the turn on an
                    # empty promise: demand a real tool call, an ask_user, or an
                    # explicit "I am blocked" and give it one more round.
                    _failure, policy = self._record_failure(
                        FailureType.TOOL_NOT_CALLED,
                        action="assistant.prose",
                        evidence=[content[:500]],
                        detail={"round": round_index},
                    )
                    if prose_corrections < _MAX_PROSE_CORRECTIONS:
                        prose_corrections += 1
                        self.state.append_user(_PROSE_ONLY_CORRECTION)
                        self._emit_trace(
                            "workflow.blocked",
                            "Assistant promised a tool action without calling one; asking it to act or ask.",
                            {
                                "attempt": prose_corrections,
                                "category": "tool_call_missing_after_promise",
                                "failure_type": FailureType.TOOL_NOT_CALLED.value,
                                "recovery_action": (
                                    policy.action.value if policy is not None else ""
                                ),
                            },
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
                if _request_requires_workspace_change(original_input) and not successful_mutation:
                    max_missing_mutation_recoveries = (
                        3 if self.long_running else _MAX_MISSING_MUTATION_RECOVERIES
                    )
                    if missing_mutation_recovery_attempts < max_missing_mutation_recoveries:
                        missing_mutation_recovery_attempts += 1
                        # Naming the target here matters: the model's prose is
                        # often a question about *which path* to write ("could
                        # you provide the full path to manage.py?"), and a
                        # correction that says "use the exact workspace target"
                        # without stating it leaves the question standing, so
                        # the next turn asks again and the run ends unwritten.
                        targets = sorted(
                            file_targets(self.original_user_request or original_input)
                        )
                        target_hint = ""
                        if targets:
                            target_hint = (
                                " The request already states the path: "
                                + ", ".join(targets)
                                + ". Paths are relative to the workspace root, so write "
                                "exactly that - do not ask which path to use."
                            )
                        # An unparseable ATTEMPT and no attempt at all need
                        # opposite corrections. Conflating them is what kept the
                        # 2026-08-03 run telling a model that had emitted a
                        # correct write_file call to stop writing prose.
                        mutation_failure = _first_mutation_parse_failure(turn.parse_failures)
                        if mutation_failure is not None:
                            # A proven byte-identical repeat cannot be argued out
                            # of at temperature 0.1, so change BOTH the sampling
                            # and the prefix: raise temperature for the next call
                            # and take the broken payload out of the prompt so the
                            # model stops copying it. The on-disk transcript keeps
                            # the original.
                            #
                            # Both fire at the first proven repeat rather than
                            # laddering: _MAX_MISSING_MUTATION_RECOVERIES is 2, so
                            # there are only three rounds here and a longer ladder
                            # would never reach its later steps.
                            pending_options = escalation.sampling_override(round_index)
                            if repeat_level >= 1:
                                self.state.replace_last_assistant(
                                    "(unparseable tool call omitted by the harness)"
                                )
                            self.state.append_user(
                                _unparseable_tool_call_correction(
                                    mutation_failure,
                                    targets[0] if targets else "",
                                )
                            )
                            self._emit_trace(
                                "workflow.blocked",
                                "A mutation call was emitted but could not be parsed; "
                                "asking for a raw block.",
                                {
                                    "attempt": missing_mutation_recovery_attempts,
                                    "category": "unparseable_tool_call",
                                    "kind": mutation_failure.kind,
                                    "parse_error": mutation_failure.error,
                                    "repeat_level": repeat_level,
                                },
                            )
                            continue
                        self.state.append_user(
                            "No workspace mutation has succeeded yet. Your diagnosis or proposed "
                            "code is not an edit. In your NEXT response, call edit_file, "
                            "append_file, or write_file on the exact workspace target using the "
                            "file evidence already returned by tools. Do not provide prose, a "
                            "code fence, or a success summary before a mutation tool returns ok."
                            + target_hint
                        )
                        self._emit_trace(
                            "workflow.blocked",
                            "Mutation was required but the model returned prose; requiring a tool call.",
                            {
                                "attempt": missing_mutation_recovery_attempts,
                                "category": "required_mutation_tool_missing",
                            },
                        )
                        continue
                    mutation_failure = _first_mutation_parse_failure(turn.parse_failures)
                    # Always keep the raw response for a failed mutation round.
                    # Without it every failure of this class looks like "returned
                    # prose", which is exactly how this went undiagnosed.
                    artifact_path = ""
                    if self.action_ledger:
                        artifact_path = self.action_ledger.record_unparsed_response(
                            "agent-executor",
                            self.model_name,
                            raw_response,
                            reason=(
                                "mutation_round_unparseable"
                                if mutation_failure is not None
                                else "mutation_round_no_attempt"
                            ),
                            round_index=round_index,
                            parse_error=(
                                mutation_failure.error if mutation_failure is not None else ""
                            ),
                        )
                    final = (
                        _unparseable_mutation_final(mutation_failure, artifact_path)
                        if mutation_failure is not None
                        else _missing_mutation_final(content, artifact_path)
                    )
                    self.state.append_assistant(final)
                    self._emit_trace(
                        "workflow.failed",
                        "The request required a workspace change, but no mutation tool succeeded.",
                        {"category": "required_mutation_missing"},
                    )
                    if self.action_ledger:
                        # Both events: the existing one keeps repl.py, the
                        # reliability report, and test_action_ledger_integration
                        # working; the new one lets telemetry stop filing an
                        # encoding failure as a planning failure.
                        self.action_ledger.log_event(
                            "mutation_required_but_missing",
                            model_response=content,
                        )
                        if mutation_failure is not None:
                            self.action_ledger.log_event(
                                "mutation_tool_call_unparseable",
                                model_response=content,
                                kind=mutation_failure.kind,
                                parse_error=mutation_failure.error,
                                tool=mutation_failure.tool,
                                path=mutation_failure.path,
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
                if control.cancel_event.is_set():
                    return self._cancelled_result(control, round_index)
                if timed_out(control):
                    return self._timeout_result(control, round_index)
                self._inject_feedback(control)
                name = _tool_call_name(call)
                arguments = _tool_call_arguments(call)
                signature = (name, json.dumps(arguments, sort_keys=True, default=str))
                requested_read = str(arguments.get("filepath") or "").replace("\\", "/").lower()
                if (
                    name in {"read_file", "file.read"}
                    and requested_read in successful_read_paths
                    and (
                        len(successful_read_paths) >= 2
                        or (
                            _request_is_verification_repair(original_input)
                            and bool(self.tools.allowed_write_paths())
                        )
                    )
                    and _request_requires_workspace_change(original_input)
                    and not successful_mutation
                ):
                    self.state.append_user(
                        "The relevant files have already been read successfully, including "
                        f"{requested_read}. Do not read them again. Your NEXT response must call "
                        "file.patch on the allowed mutation target using "
                        "the source evidence already in context."
                    )
                    self._emit_trace(
                        "workflow.recovering",
                        f"Skipped redundant read_file for {requested_read}; requiring mutation.",
                        {"category": "read_saturation", "filepath": requested_read},
                    )
                    if not strict_repair_recovery_attempted:
                        strict_repair_recovery_attempted = True
                        handoff = await self._run_scoped_repair_handoff(
                            self.tools.allowed_write_paths(), round_index
                        )
                        if handoff is not None:
                            return handoff
                    continue
                repeated_calls[signature] += 1
                if repeated_calls[signature] >= _MAX_REPEATED_CALLS:
                    failure, policy = self._record_failure(
                        FailureType.REPEATED_ACTION,
                        action=name,
                        evidence=[json.dumps(arguments, sort_keys=True, default=str)],
                        detail={"round": round_index, "successful_before": signature in successful_call_signatures},
                    )
                    if signature in successful_call_signatures:
                        failed_targets = sorted(unconfirmed_failed_writes)
                        target_hint = (
                            f" The failed mutation target is {failed_targets[-1]}."
                            if failed_targets
                            else ""
                        )
                        if policy is None or policy.action == RecoveryAction.BLOCK_IDENTICAL_CALL:
                            self.state.append_user(
                                _repetition_correction(name) + target_hint
                            )
                        self._emit_trace(
                            "workflow.recovering",
                            f"Skipped a repeated successful {name} call and required a different action.",
                            {
                                "category": "successful_tool_repetition",
                                "failure_type": FailureType.REPEATED_ACTION.value,
                                "tool": name,
                                "target": failed_targets[-1] if failed_targets else "",
                                "retry_count": failure.retry_count if failure is not None else 0,
                            },
                        )
                        break
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
                result = self.tool_dispatcher.dispatch(name, arguments)
                self._checkpoint_latest_observation(name=name, arguments=arguments, result=result)
                self._sync_artifacts_after_tool_result(name=name, arguments=arguments, result=result)
                if result.ok:
                    successful_call_signatures.add(signature)
                    if name in {"read_file", "file.read"}:
                        result_data = result.data if isinstance(result.data, dict) else {}
                        read_path = str(
                            result_data.get("resolved_filepath")
                            or result_data.get("filepath")
                            or arguments.get("filepath")
                            or ""
                        ).replace("\\", "/").lower()
                        if read_path:
                            successful_read_paths.add(read_path)
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
                command_mutation = (
                    name in {"run_command", "test.run"}
                    and result.ok
                    and bool(result_data.get("touched_files"))
                )
                if (name in _MUTATION_TOOL_NAMES and result.ok) or mcp_mutation or command_mutation:
                    successful_mutation = True
                elif result.ok:
                    nonwrite_tool_succeeded = True
                self._log_tool_result(name, result)
                if self.audit:
                    self.audit.log_tool_result(
                        name, bool(result.ok), result.message, _compact_value(result.data, limit=4000)
                    )
                    self._audit_file_change(name, arguments, result)
                    if name in {"run_command", "test.run"}:
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
                tool_failure = None
                tool_failure_policy = None
                if not result.ok:
                    try:
                        tool_failure = self.failure_tracker.record_tool_result(name, arguments, result)
                        tool_failure_policy = self.failure_tracker.policy_for(tool_failure)
                    except Exception:
                        tool_failure = None
                        tool_failure_policy = None
                    if tool_failure is not None and tool_failure_policy is not None:
                        self._emit_trace(
                            "failure.recorded",
                            f"{tool_failure.failure_type.value}: {tool_failure_policy.action.value}",
                            {
                                "failure_type": tool_failure.failure_type.value,
                                "tool": name,
                                "retry_count": tool_failure.retry_count,
                                "recovery_action": tool_failure_policy.action.value,
                            },
                            level="verbose",
                        )
                if not result.ok:
                    repairable = bool(
                        tool_failure_policy is not None
                        and tool_failure_policy.action
                        in {
                            RecoveryAction.ENTER_REPAIR,
                            RecoveryAction.SAFE_ARGUMENT_REPAIR,
                            RecoveryAction.RETRY_WITH_TOOL_CONTRACT,
                        }
                    )
                    replan_needed = bool(
                        tool_failure_policy is not None
                        and tool_failure_policy.action == RecoveryAction.REPLAN
                    )
                    step_outcome = self._apply_step_outcome(
                        step_controller,
                        step_controller.note_failure(
                            repairable=repairable,
                            replan_needed=replan_needed,
                        ),
                        round_index=round_index,
                        failure_policy=tool_failure_policy,
                    )
                    control.record_event(
                        "step_action_failed",
                        round=round_index,
                        step_id=step_controller.step_id,
                        tool=name,
                        decision=step_outcome.decision.value,
                    )
                    if step_outcome.decision == StepExecutionDecision.BLOCK:
                        return self._step_blocked_result(
                            step_outcome,
                            control,
                            round_index,
                            written_files,
                        )
                if name.startswith("mcp__") and not result.ok:
                    self.state.append_user(
                        f"The MCP call {name} failed: {result.message} "
                        "The user already authorized the requested operation. Choose the matching "
                        "registered mcp__ tool, correct its arguments, and continue. Do not replace "
                        "it with a shell command or ask for confirmation that was already given."
                    )
                if not result.ok and "denied by user" in result.message.lower():
                    self._record_failure(
                        FailureType.PERMISSION_DENIED,
                        action=name,
                        evidence=[result.message],
                        detail={"arguments": arguments},
                    )
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
                if name in {"write_file", "edit_file", "append_file", "file.patch"} and result.ok:
                    _data = result.data if isinstance(result.data, dict) else {}
                    written = str(
                        _data.get("resolved_filepath")
                        or _data.get("filepath")
                        or arguments.get("filepath")
                        or ""
                    )
                    if written and written not in written_files:
                        written_files.append(written)
                    truncation = _truncated_write_correction(self.workspace_root, written)
                    if truncation:
                        # The write SUCCEEDED but the content stops mid-construct:
                        # a 7B routinely runs out of room part-way through a long
                        # file (live 2026-08-02: a 35-byte settings.py stub, and a
                        # manage.py with an unterminated string literal). Treat it
                        # as unfinished rather than done, and ask for the rest
                        # instead of letting the turn end on a broken file.
                        truncation_recoveries += 1
                        if truncation_recoveries <= _MAX_TRUNCATION_RECOVERIES:
                            self.state.append_user(truncation)
                            self._emit_trace(
                                "workflow.blocked",
                                f"{written} was written truncated; asking for the remainder.",
                                {"attempt": truncation_recoveries, "category": "truncated_write"},
                            )
                            continue
                if mcp_mutation:
                    for written in result_data.get("touched_files", []):
                        written = str(written)
                        if written and written not in written_files:
                            written_files.append(written)
                if command_mutation:
                    deleted = {str(path) for path in result_data.get("deleted_files", [])}
                    for written in result_data.get("touched_files", []):
                        written = str(written)
                        if written and written not in deleted and written not in written_files:
                            written_files.append(written)
                if result.ok:
                    self._checkpoint_successful_tool(
                        round_index=round_index,
                        name=name,
                        arguments=arguments,
                        result=result,
                        changed_files=list(written_files),
                    )
                    step_outcome = self._apply_step_outcome(
                        step_controller,
                        step_controller.note_success(),
                        round_index=round_index,
                    )
                    control.record_event(
                        "step_action_succeeded",
                        round=round_index,
                        step_id=step_controller.step_id,
                        tool=name,
                        decision=step_outcome.decision.value,
                    )
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
                    pending = result.data.get("pending_question", {})
                    question = str((pending or {}).get("question", ""))
                    stalls_named_write = (
                        stall_answers < _MAX_STALL_ANSWERS
                        and _question_stalls_a_named_write(
                            question,
                            self.original_user_request or original_input,
                            bool(written_files),
                            (pending or {}).get("options") or (),
                        )
                    )
                    if stalls_named_write:
                        stall_answers += 1
                    if stalls_named_write or _asks_permission_already_granted(
                        question, original_input
                    ):
                        # Asking "the file does not exist, should I create it?"
                        # for a request that SAYS "create the file" spends the
                        # turn re-requesting consent the user already gave
                        # (observed live 2026-08-02: a one-file build ended in
                        # 2.8s having written nothing). Answer it and continue.
                        self.state.append_user(
                            "Yes - that is exactly what was requested. Do not ask for "
                            "permission to do what the request already states. Perform "
                            "the write now with write_file and the complete file content."
                        )
                        self._emit_trace(
                            "clarification.skipped",
                            "Declined a permission question the request already answered.",
                            {"round": round_index},
                        )
                        continue
                    # ask_user ends the turn: store the pending question and hand
                    # control back to the user (resolved on their next reply).
                    return self._handle_ask_user(pending, original_input, round_index)
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
                if name in {"find_file", "grep_files"} and result.ok:
                    # Searching for a file the request says to CREATE returns
                    # "no files matched", which the model reads as "I do not
                    # know the path" and answers with "could you provide the
                    # full path to X?" - then repeats that sentence for the
                    # rest of the session (observed live 2026-08-02: ten
                    # identical assistant turns, zero files). Absence is the
                    # expected result here, so say so.
                    missing = self._creation_target_not_found(
                        arguments, result, original_input
                    )
                    if missing:
                        self.state.append_user(
                            f"{missing} does not exist yet - that is expected, the "
                            "request is to create it. Do not search for it again and do "
                            "not ask where it should go. Call write_file with filepath "
                            f"'{missing}' and the complete file content now."
                        )
                        self._emit_trace(
                            "workflow.blocked",
                            f"Discovery found no {missing}; it is the file to create.",
                            {"category": "search_for_creation_target"},
                        )
                if name in {"write_file", "append_file"}:
                    filepath = str(arguments.get("filepath", "the file"))
                    if result.ok:
                        unconfirmed_failed_writes.pop(filepath, None)
                        # A small model often follows the failed tool's candidate
                        # path on its next turn. Reconcile the stale failure for
                        # the same basename once that corrected write succeeds.
                        for failed_path in list(unconfirmed_failed_writes):
                            if _basename(failed_path) == _basename(filepath):
                                unconfirmed_failed_writes.pop(failed_path, None)
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
                            append_available="append_file" in self._registered_tool_names,
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
        final = (
            f"I stopped after {step_round_limit} bounded step action(s) to avoid looping."
        )
        self.state.append_assistant(final)
        self._mark_active_step_blocked("step action budget exhausted")
        self._audit_final(final)
        return AgentLoopResult(
            final=final, tool_rounds=step_round_limit, stopped=True,
            changed_files=tuple(written_files),
        )

    def _append_codebase_memory(self, user_input: str) -> str:
        """Attach Codebase-Memory MCP facts about the files this turn names.

        `CodeEditWorkflow` and `BugfixWorkflow` have always done this, but the
        route table has no `edit` entry and `file.write` sits above everything
        that would reach them - so "edit core/views.py to ..." is dispatched
        here, and the code graph, however healthy, was never consulted. The
        symptom is the expensive one: the model re-derives what a module
        exports and imports by guessing, and invents names that do not exist.

        Only files that ALREADY exist are looked up; a request to create a new
        file has nothing to say. Best-effort throughout - an unavailable or
        unindexed workspace returns "" and the turn proceeds unchanged.
        """
        try:
            from shamsu.abstract.context import build_codebase_memory_brief
            from shamsu.agents.rewrite_fallback import mentioned_workspace_files

            # Capped tightly: the brief renders at most three paths, and by the
            # time a composite turn reaches here the text can carry plan and
            # workspace context naming files the user never asked about. Tokens
            # come back in order of appearance, so the file the request actually
            # names leads and incidental mentions cannot crowd it out.
            targets = mentioned_workspace_files(self.workspace_root, user_input, limit=3)
            if not targets:
                # Nothing named that exists - skip the lookup rather than pay a
                # healthcheck round-trip on every conversational turn.
                return user_input
            brief = build_codebase_memory_brief(self.workspace_root, targets)
        except Exception:
            return user_input
        if not brief:
            return user_input
        if self.session_logger:
            self.session_logger.log(
                "codebase_memory.retrieved",
                {"specialist": "agent-chat", "targets": targets[:3]},
                "Retrieved Codebase-Memory facts for named files",
                workflow_id="agent-chat",
            )
        return f"{user_input}\n\n{brief}"

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
                "Retrieved relevant local project memories",
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
        if (
            plan.needs_input
            and not _question_is_answerable_by_reading(
                plan.question, self.workspace_root, user_input
            )
            and not _planner_question_answered_by_request(
                plan.question, plan.options, user_input
            )
        ):
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
        try:
            contract = self.agent_planner.persist_contract(user_input, plan.text)
            if contract.phase is not None:
                self._checkpoint_task_status(
                    RunStatus.RUNNING,
                    contract.phase.value,
                    "active_step_selected",
                )
            contract_text = contract.text
        except Exception:
            contract_text = f"Plan from planner model:\n{plan.text}"
        return f"{user_input}\n\n{contract_text}"

    async def _maybe_verify(self, content: str, written_files: list[str]) -> str:
        """After writes, run a deterministic lightweight verifier once.

        Never claims success it did not check: a build failure turns into a loud
        'UNCONFIRMED' note (the model may have claimed success), a pass adds a
        short confirmation, and an unverifiable change is left untouched. Safe
        lightweight checks run interactively; automatic repair is autonomous-only.
        A verifier error never breaks the turn."""
        if not self.verify_changes or not _VERIFY_GATE_ENABLED or not written_files:
            return content
        try:
            outcome = await self.change_verifier.verify(list(written_files))
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
            _failure, policy = self._record_failure(
                FailureType.VERIFICATION_FAILURE,
                action=outcome.command or "verify",
                evidence=[outcome.summary],
                detail={"files": list(written_files), "exit_code": outcome.exit_code},
            )
            # One bounded repair pass before giving up (gap E1), itself capped
            # at _AUTO_REPAIR_MAX_ATTEMPTS iterations. The machinery to fix
            # a one-line syntax error after a 30-minute run existed all along
            # (RepairLoop, used by freeform/full_pipeline) and simply was never
            # invited here - the loop verified, reported failure, and left the
            # user to start over. Best-effort and capped: a repair error or a
            # still-failing repair falls through to the honest UNCONFIRMED note.
            repaired = (
                await self._attempt_repair(written_files)
                if self.long_running
                and (policy is None or policy.action == RecoveryAction.ENTER_REPAIR)
                else None
            )
            if repaired is not None and repaired.verified:
                self._emit_trace(
                    "verify.result",
                    f"repaired: {repaired.summary}",
                    {"status": "verified_after_repair", "command": repaired.command},
                )
                return (
                    f"{content}\n\n[verified after repair] The first check failed "
                    f"({outcome.summary}), so I ran bounded repair iterations; "
                    f"it now passes: {repaired.summary}"
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
        """Run bounded strict repair iterations over scoped editable files.

        Returns the repair's VerifyOutcome, or None when repair is disabled,
        unavailable (no schema-capable LLM), or errored - the caller then keeps
        the plain UNCONFIRMED behavior. The verifier stays lightweight so a
        mid-chat repair can never be the thing that runs pip/npm installs."""
        if not _AUTO_REPAIR_ENABLED:
            return None
        try:
            self.repair_recorder.record_attempt(list(written_files))
        except Exception:
            pass
        generate_async = getattr(self.llm, "generate_structured", None)
        if not callable(generate_async):
            return None
        session_logger = self.session_logger

        def _generate_sync(system: str, user: str, schema: dict) -> str:
            # Fresh manager + asyncio.run: this runs inside a worker thread with
            # no event loop, the same shape as repl._pipeline_generate.
            from shamsu.llm.manager import LLMManager

            async def _bounded_generate() -> str:
                return await asyncio.wait_for(
                    LLMManager(
                        session_logger=session_logger,
                        action_ledger=self.action_ledger,
                    ).generate_structured(
                        "coder",
                        system,
                        user,
                        schema,
                        num_predict=_REPAIR_MODEL_MAX_OUTPUT_TOKENS,
                    ),
                    timeout=_REPAIR_MODEL_TIMEOUT_SECONDS,
                )

            return asyncio.run(_bounded_generate())

        try:
            from shamsu.verify.gate import verify_and_repair

            return await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: verify_and_repair(
                    self.workspace_root,
                    list(written_files),
                    generate=_generate_sync,
                    command_runner=self.tools.command_runner,
                    max_attempts=_AUTO_REPAIR_MAX_ATTEMPTS,
                    lightweight=True,
                    session_logger=session_logger,
                    action_ledger=self.action_ledger,
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

    async def _run_scoped_repair_handoff(
        self, repair_targets: list[str], round_index: int
    ) -> AgentLoopResult | None:
        """Hand grounded repair targets from broad chat to strict verification."""
        self._emit_trace(
            "workflow.recovering",
            "Evidence gathered without a mutation; handing scoped targets to the "
            "verifier-driven repair loop.",
            {"category": "strict_repair_handoff", "targets": repair_targets},
        )
        repaired = await self._attempt_repair(repair_targets)
        if repaired is None:
            return None
        if repaired.verified:
            final = (
                "[verified after repair] The chat loop gathered the relevant evidence but did "
                "not apply an edit, so the scoped strict repair loop took over. "
                f"{repaired.summary}"
            )
            self._audit_final(final)
            return AgentLoopResult(
                final=final,
                tool_rounds=round_index,
                changed_files=tuple(repair_targets),
            )
        final = (
            "The verifier-driven repair loop could not confirm a repair within its bounded "
            f"attempts. {repaired.summary} Treat the target files as UNCONFIRMED."
        )
        self._audit_final(final)
        return AgentLoopResult(
            final=final,
            tool_rounds=round_index,
            stopped=True,
            changed_files=tuple(repair_targets),
        )

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
        pending["created_from_prompt"] = self.original_user_request or original_input
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

    def _creation_target_not_found(
        self, arguments: dict[str, Any], result: Any, original_input: str
    ) -> str:
        """The request's target file, when a search just proved it absent.

        Returns "" unless the request asked to create a file, the search was
        for that file, and it turned up nothing.
        """
        request = self.original_user_request or original_input
        if not _CREATE_INTENT_RE.search(request or ""):
            return ""
        targets = file_targets(request)
        if not targets:
            return ""
        data = getattr(result, "data", None) or {}
        if isinstance(data, dict):
            if data.get("count") or data.get("candidates") or data.get("matches"):
                return ""
        query = str(arguments.get("query") or arguments.get("filepath") or "").strip()
        if not query:
            return ""
        needle = query.replace("\\", "/").lower()
        for target in sorted(targets):
            normalized = target.replace("\\", "/").lower()
            if needle in normalized or normalized.endswith(needle):
                if not (self.workspace_root / target).exists():
                    return target
        return ""

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
        sole_target = str(
            getattr(self.tools, "sole_allowed_write_path", lambda: "")() or ""
        ).replace("\\", "/")
        requested_path = filepath.replace("\\", "/").lstrip("./")
        exact_scoped_target = bool(
            sole_target
            and (
                sole_target == requested_path
                or sole_target.endswith("/" + requested_path)
            )
        )
        if exact_scoped_target or _request_explicitly_creates_path(user_request, filepath):
            return (
                f"read_file {filepath} confirmed that the requested new file does not exist. "
                f"The orchestrated step explicitly targets {sole_target or filepath}; call write_file "
                "now with its complete "
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
        if bool(getattr(self.tools, "has_scoped_reads", lambda: False)()):
            return (
                f"read_file {filepath} failed ({message}) and no matching file exists inside the "
                "active project. Do not inspect or ask about files from other projects. If this "
                "milestone needs the file, call write_file with its complete implementation under "
                "the active project root; otherwise continue with another in-scope project file."
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
    if category == TIMEOUT_CONNECT:
        return (
            f"{base} SHAMSU could not connect to the local model transport. "
            "Start or repair Ollama, then try again."
        )
    if category == TIMEOUT_FIRST_TOKEN:
        return (
            f"{base} The model connection succeeded, but generation did not start. "
            "This usually means model load or prompt prefill is too slow for the current settings."
        )
    if category == TIMEOUT_TOKEN_IDLE:
        return (
            f"{base} Generation started, then no token arrived before the idle deadline. "
            "This is a token-idle stall, not healthy slow generation."
        )
    if category == TIMEOUT_TOTAL_GENERATION:
        return (
            f"{base} The optional total generation cap fired. "
            "Increase SHAMSU_TOTAL_GENERATION_TIMEOUT_SECONDS or leave it unset for slow-but-active generation."
        )
    if category == TIMEOUT_TASK:
        return "The task-level timeout expired before the run finished."
    if category == TIMEOUT_STEP:
        return "The step-level timeout expired before the active plan step finished."
    if category == TIMEOUT_TOOL:
        return f"A tool timed out after {seconds}s."
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


def _default_ollama_client(base_url: str, timeout_config: TimeoutConfig) -> ollama.AsyncClient:
    timeout = httpx.Timeout(
        timeout=None,
        connect=timeout_config.connect_timeout,
        read=None,
        write=None,
        pool=None,
    )
    return ollama.AsyncClient(host=base_url, timeout=timeout)


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


def _request_is_verification_repair(prompt: str) -> bool:
    """Identify scoped fix/debug requests suitable for deterministic repair.

    Feature creation still belongs to the normal ReAct loop. This handoff is
    reserved for requests that explicitly combine repair language with failing
    tests, errors, or a verification command.
    """
    lowered = " ".join((prompt or "").lower().split())
    repair_signal = re.search(r"\b(fix|repair|debug|resolve|correct|failing)\b", lowered)
    verification_signal = re.search(
        r"\b(test|tests|pytest|vitest|error|failure|traceback|verify|verification|"
        r"compile|build)\b|manage\.py\s+test|npm\s+(?:run\s+)?test",
        lowered,
    )
    return bool(repair_signal and verification_signal)


def _missing_mutation_final(model_response: str, artifact_path: str = "") -> str:
    detail = " ".join((model_response or "").strip().split())
    suffix = f" The model's response was: {detail[:300]}" if detail else ""
    # The 300-char clip stays - a terminal message should not dump 5 kB - but it
    # stops being a LOSS now that the full text is on disk and named here.
    evidence = f" The full raw response was saved to {artifact_path}." if artifact_path else ""
    return (
        "I did not complete the requested workspace change because no file mutation "
        f"succeeded. No file was changed.{suffix}{evidence}"
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


_CONTENT_QUESTION_RE = re.compile(
    r"\b(what|purpose|about|contain|contains|describe|explain|summar\w*|say|says|detail\w*)\b",
    re.IGNORECASE,
)
_USER_CHOICE_RE = re.compile(
    r"\b(or|instead|prefer|choose|pick|which one|rather|option [ab12])\b",
    re.IGNORECASE,
)


_CREATE_INTENT_RE = re.compile(
    # Edit verbs count too: "replace the line X with Y in config/urls.py" has
    # named its target just as firmly as "create X", and the same stall came
    # back as "should the routes go before or after?" when the prompt showed
    # the order (observed live 2026-08-02).
    r"\b(creat\w*|writ\w*|add|adds|adding|make|makes|making|generat\w*|"
    r"scaffold\w*|implement\w*|build\w*|replac\w*|rewrit\w*|updat\w*|"
    r"modif\w*|fix\w*|edit\w*|insert\w*|remov\w*|delet\w*)\b",
    re.IGNORECASE,
)


_TARGET_QUESTION_RE = re.compile(
    r"\b(full path|path|paths|director\w*|folder|location|where|"
    r"creat\w*|exist\w*|overwrit\w*|file ?name|which file|this file)\b",
    re.IGNORECASE,
)


def _question_stalls_a_named_write(
    question: str,
    user_request: str,
    wrote_anything: bool,
    options: Sequence[Any] = (),
) -> bool:
    """True when ask_user cannot yet be a real question.

    Matching question *phrasings* is a losing game - the same non-question
    arrived live on 2026-08-02 as "should I create it?", "do you want to
    create the manage.py file in this directory?" and "could you please
    provide the full path to manage.py", each ending the turn having written
    nothing. Gate on the situation instead: a request that names the file to
    create has already answered both "which file" and "may I", so until
    something has been written there is nothing to ask about. A genuine
    either/or choice still reaches the user.

    Only create/write requests qualify. "read the file src/App.tsx" is
    genuinely ambiguous when two such files exist, and a question offering
    concrete alternatives is a real choice whatever the request said.
    """
    if wrote_anything:
        return False
    if len(options or ()) >= 2:
        return False
    if _USER_CHOICE_RE.search(question or ""):
        return False
    if not _CREATE_INTENT_RE.search(user_request or ""):
        return False
    # The question must be about the *target* - which path, may I create it,
    # does it exist. A question about a VALUE ("which port?" for "fix the
    # server port in app.py") is a real one however firmly the file is named,
    # and suppressing it answers the user's decision for them.
    if not _TARGET_QUESTION_RE.search(question or ""):
        return False
    return bool(file_targets(user_request or ""))


def _question_is_answerable_by_reading(
    question: str, workspace_root: Path, user_input: str = ""
) -> bool:
    """True when the planner's question is answerable from a document present.

    Asking "What is the main purpose of canvas lite.pdf?" - or "What is the
    primary purpose of the app?" when the request pointed at a spec - spends
    the whole turn on something one read-only call answers. Observed live
    2026-08-02: two plan attempts ended with ZERO tool calls because this gate
    fired before any reading happened.

    The document may be named in the QUESTION or only in the REQUEST, so both
    are checked. Asking the user to CHOOSE between alternatives is still their
    decision and is left alone, and the loop can always call ask_user later if
    the document turns out not to answer it.
    """
    text = str(question or "").strip()
    if not text or not _CONTENT_QUESTION_RE.search(text) or _USER_CHOICE_RE.search(text):
        return False
    try:
        from shamsu.tools.workspace import WorkspaceTool

        tool = WorkspaceTool(workspace_root)
        return bool(tool.names_in_text(text) or tool.names_in_text(user_input))
    except Exception:
        return False


_GENERIC_OPTION_WORDS = {
    "section",
    "feature",
    "functionality",
    "page",
    "screen",
    "component",
    "view",
    "the",
    "app",
    "frontend",
    "main",
    "jsx",
    "tsx",
    "page",
    "file",
}
_PLANNER_PERMISSION_RE = re.compile(
    r"\b(should|shall|do you want|would you like|may i|can i)\b",
    re.IGNORECASE,
)
_ACTION_WORDS_BY_STEM = {
    "remov": ("remove", "removes", "removed", "removing"),
    "delet": ("delete", "deletes", "deleted", "deleting"),
    "add": ("add", "adds", "added", "adding"),
    "creat": ("create", "creates", "created", "creating"),
    "updat": ("update", "updates", "updated", "updating"),
    "modif": ("modify", "modifies", "modified", "modifying"),
    "fix": ("fix", "fixes", "fixed", "fixing"),
}


def _planner_question_answered_by_request(
    question: str, options: Sequence[Any], user_input: str
) -> bool:
    """Suppress upfront questions whose own options are already named.

    Live 2026-08-04: the planner asked "Which section in App.jsx should be
    removed?" with option "Login Section" after the user had already said
    "remove the login section from App.jsx". That is not a choice; it is the
    request repeated back as a question, and it traps the user in clarification
    instead of editing.
    """
    q = str(question or "").lower()
    request = str(user_input or "").lower()
    if _planner_permission_answered_by_request(q, request):
        return True
    if not re.search(r"\b(which|what)\b.*\b(section|feature|functionality|page|screen)\b", q):
        return False
    request_words = set(re.findall(r"[a-z0-9_]{3,}", request))
    if not request_words:
        return False
    for option in options or ():
        if isinstance(option, dict):
            label = str(option.get("label") or "")
        else:
            label = str(option or "")
        label_words = {
            word
            for word in re.findall(r"[a-z0-9_]{3,}", label.lower())
            if word not in _GENERIC_OPTION_WORDS
        }
        if label_words and label_words <= request_words:
            return True
    return False


def _planner_permission_answered_by_request(question: str, user_input: str) -> bool:
    if not _PLANNER_PERMISSION_RE.search(question):
        return False
    request_words = set(re.findall(r"[a-z0-9_]{3,}", user_input))
    question_words = set(re.findall(r"[a-z0-9_]{3,}", question))
    for stem, variants in _ACTION_WORDS_BY_STEM.items():
        if not any(word.startswith(stem) for word in question_words):
            continue
        if not any(word in request_words for word in variants):
            continue
        # The action matches; require at least one meaningful object word in
        # common so a generic "yes" does not authorize an unrelated action.
        object_words = (
            question_words
            - set(variants)
            - _GENERIC_OPTION_WORDS
            - {"should", "shall", "want", "would", "like", "main", "from"}
        )
        if object_words & request_words:
            return True
    return False


def _looks_like_deferred_action(content: str) -> bool:
    """True when a tool-less reply merely *promises* a tool action."""
    text = " ".join(content.strip().lower().split())
    if not text:
        return False
    return any(re.search(pattern, text) for pattern in _DEFERRED_ACTION_PATTERNS)


def _promised_read_tool_call(content: str) -> dict[str, Any] | None:
    """Turn an explicit, safe read promise into the tool call a small model omitted."""
    matches = list(re.finditer(
        r"\b(?:read(?:ing)?|open(?:ing)?|inspect(?:ing)?)\s+(?:the\s+)?"
        r"[`\"'](?P<path>[^`\"'\r\n]+\.[A-Za-z0-9_]{1,12})[`\"']",
        content or "",
        re.IGNORECASE,
    ))
    if not matches:
        return None
    # Small models often summarize the prior read before promising the next
    # one. The final explicit read phrase is the action they intend now.
    match = matches[-1]
    filepath = match.group("path").strip()
    return {
        "id": f"salvaged_read_{sha256(filepath.encode('utf-8')).hexdigest()[:10]}",
        "type": "function",
        "function": {
            "name": "read_file",
            "arguments": {"filepath": filepath},
        },
    }


_MUTATION_PROMISE_RE = re.compile(
    # The optional `_file` matters: models narrate the call itself ("write_file
    # with filepath 'library/urls.py'"), and `\bwrite\b` cannot match inside
    # `write_file`, so the promise went unrecognised and nothing was salvaged.
    r"\b(?:writ(?:e|ing)|creat(?:e|ing)|sav(?:e|ing)|updat(?:e|ing)|edit(?:ing)?|"
    r"implement(?:ing)?|overwrit(?:e|ing)|add(?:ing)?)(?:_file)?\b"
    r"[^.\r\n]{0,80}?"
    r"[`\"']?[\w][\w./\\-]*\.[A-Za-z0-9_]{1,12}[`\"']?",
    re.IGNORECASE,
)
_PROSE_FILE_TOKEN_RE = re.compile(r"[\w][\w./\\-]*\.[A-Za-z0-9_]{1,12}\b")
_SINGLE_FENCE_RE = re.compile(r"```[^\r\n]*\r?\n(?P<body>.*?)```", re.DOTALL)


# Files at or below this size are always rewritten whole rather than patched.
# Measured 2026-08-02: every `edit_file` attempt failed on an `old_string`
# mismatch (five on one line of urls.py), while every whole-file `write_file`
# landed first try. Aider's benchmarks show the same - edit format alone swung
# GPT-4 Turbo 26% -> 59%, and weak models exceed 50% patch failure.
_WHOLE_FILE_MAX_BYTES = _os_env_int("SHAMSU_WHOLE_FILE_MAX_BYTES", 8000, 200)


def edit_tools_for_target(
    target: str, workspace_root: Path, available: Sequence[str]
) -> tuple[str, ...]:
    """The mutation tools to offer for a single-file turn.

    Small or missing files get `write_file` only: withholding the patch tools
    is what actually changes behaviour. Telling the model to prefer whole-file
    writes did not - it reached for `append_file` anyway and appended a route
    outside `urlpatterns`.
    """
    mutators = {"write_file", "edit_file", "append_file"}
    try:
        size = (Path(workspace_root) / target).stat().st_size
    except OSError:
        size = 0
    whole_file_only = size <= _WHOLE_FILE_MAX_BYTES
    return tuple(
        name
        for name in available
        if name not in mutators or (name == "write_file" or not whole_file_only)
    )


def _is_unparseable_python(path: Path, source: str) -> bool:
    """True for a .py file that does not currently compile."""
    if path.suffix.lower() != ".py":
        return False
    try:
        compile(source, str(path), "exec")
    except SyntaxError:
        return True
    except (ValueError, TypeError):
        return False
    return False


def _promised_write_tool_call(
    content: str, workspace_root: Path, user_request: str = ""
) -> dict[str, Any] | None:
    """Turn a prose-only mutation promise plus its code fence into the
    write_file call a small model omitted.

    The 2026-08-01 dogfood showed 7B coders repeatedly *promising* an
    edit_file call, showing the finished file in a fence, and emitting no tool
    call - reads had a salvager for this, mutations did not. Conservative on
    purpose: exactly one fence, exactly one promised path, and for an existing
    file the fence must read as a full replacement (at least as long as the
    current content and containing its first meaningful line) so a snippet can
    never clobber a file."""
    text = content or ""
    fences = list(_SINGLE_FENCE_RE.finditer(text))
    if len(fences) != 1:
        return None
    body = fences[0].group("body")
    if not body.strip():
        return None
    prose = text[: fences[0].start()]
    # Exactly one distinct file token in the whole promise: a reply naming a
    # second file (another target, or a source doc) is ambiguous - skip it
    # rather than guess which file the fence belongs to.
    paths = {
        match.group(0).strip().replace("\\", "/")
        for match in _PROSE_FILE_TOKEN_RE.finditer(prose)
    }
    if _MUTATION_PROMISE_RE.search(prose) and len(paths) == 1:
        filepath = next(iter(paths))
    else:
        # No usable promise. A model that answers "create library/.../login.html"
        # with the finished document and *no prose at all* leaves prose empty,
        # so there is nothing to match - yet the target is not in doubt, the
        # request named exactly one file (observed live 2026-08-02, the reply
        # opened directly with ```html and the whole turn was discarded).
        request_targets = file_targets(user_request or "")
        if len(request_targets) != 1:
            return None
        filepath = next(iter(request_targets)).replace("\\", "/")
    try:
        target = (Path(workspace_root) / filepath).resolve()
        target.relative_to(Path(workspace_root).resolve())
    except (OSError, ValueError):
        return None
    if target.is_file():
        try:
            existing = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        # These two guards stop a snippet clobbering a good file: the rewrite
        # must be at least as long and must keep the current first line. A
        # file that does not parse has nothing worth protecting, and a repair
        # necessarily breaks both rules - it removes the bad line (so the first
        # line changes) and is therefore shorter. Refusing there blocked the
        # only fix that mattered: a stray "import path from django.urls"
        # survived five rewrite attempts live on 2026-08-02.
        # A template converted to `{% extends %}` legitimately drops the
        # document it used to own - doctype, <html>, <head> and all - so it is
        # both shorter and missing the old first line. The guard refused that
        # rewrite three times running.
        converting_to_extends = body.lstrip().startswith(
            ("{% extends", "{%extends")
        ) and not existing.lstrip().startswith(("{% extends", "{%extends"))
        if not converting_to_extends and not _is_unparseable_python(target, existing):
            existing_lines = [line for line in existing.splitlines() if line.strip()]
            body_line_count = len([line for line in body.splitlines() if line.strip()])
            if existing_lines and (
                body_line_count < len(existing_lines)
                or existing_lines[0].strip() not in body
            ):
                return None
    if not body.endswith("\n"):
        body += "\n"
    return {
        "id": f"salvaged_write_{sha256(filepath.encode('utf-8')).hexdigest()[:10]}",
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": {"filepath": filepath, "content": body},
        },
    }


_MAX_TRUNCATION_RECOVERIES = 3
# Python syntax errors that mean "the file stops mid-construct", as opposed to
# an ordinary typo. These are what a cut-off generation produces.
_TRUNCATION_ERROR_MARKERS = (
    "unterminated string literal",
    "unterminated triple-quoted string literal",
    "was never closed",
    "unexpected eof",
    "expected an indented block",
    "incomplete input",
)


def _truncated_write_correction(workspace_root: Path, relative_path: str) -> str:
    """Ask for the remainder when a just-written file stops mid-construct.

    A successful write is not a finished file: a 7B commonly runs out of output
    room part-way through, leaving an unterminated string or an unclosed
    bracket. Live 2026-08-02 this produced a 35-byte `settings.py` and a
    `manage.py` with `SyntaxError: unterminated string literal`, each of which
    ended the turn looking like success. Only Python is checked, and only for
    errors that specifically mean "cut off".
    """
    path = str(relative_path or "").strip()
    if not path or not path.lower().endswith(".py"):
        return ""
    try:
        target = (Path(workspace_root) / path).resolve()
        target.relative_to(Path(workspace_root).resolve())
        source = target.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeError):
        return ""
    if not source.strip():
        return ""
    try:
        compile(source, path, "exec")
    except SyntaxError as exc:
        message = str(getattr(exc, "msg", "") or exc).lower()
        if not any(marker in message for marker in _TRUNCATION_ERROR_MARKERS):
            return ""
    except ValueError:
        return ""
    else:
        return ""
    tail = "\n".join(source.splitlines()[-12:])
    return (
        f"{path} was written but STOPS PART-WAY THROUGH - it does not parse: the "
        "content is cut off mid-construct, so the file is unusable as written. Do "
        "not re-send the whole file (that is what truncated). Call append_file on "
        f"{path} with ONLY the missing remainder, continuing exactly from where "
        "this current tail ends, and close every open string, bracket and block:\n"
        "--- current end of file ---\n"
        f"{tail}\n"
        "--- end ---"
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
    append_available: bool = True,
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
        # Naming only the ambiguity ("read the file, then decide") left a 7B
        # model repeating the identical empty-anchor call until the run hit its
        # deadlock timeout - observed live 2026-08-01 adding AUTH_USER_MODEL to
        # settings.py. Still refuse to ASSUME which was meant; enumerate both
        # branches concretely so either one is a single next call.
        add_option = (
            "to ADD it as new content, call append_file with the same filepath and this content"
            if append_available
            else (
                "to ADD it as new content, call read_file and then edit_file with old_string "
                "anchored on the exact line it should follow and new_string containing that "
                "line plus your addition"
            )
        )
        how = (
            "An empty old_string is not a valid replacement anchor and does not reveal whether "
            f"you intended to add or replace content. Decide explicitly: {add_option}; to "
            "REPLACE existing content, call read_file and use an exact old_string copied from "
            "it. Do not retry the empty anchor. If a targeted change remains difficult, call "
            "write_file with the complete corrected file."
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
        current_excerpt = str((data or {}).get("current_excerpt") or "").strip()
        exact_source = (
            " The tool already returned this exact current source excerpt; use text from it "
            "as the next old_string:\n--- current source ---\n"
            + current_excerpt
            + "\n--- end current source ---"
            if current_excerpt
            else ""
        )
        how = (
            "That old_string was not found verbatim. Copy EXACT current text (whitespace "
            "included) into old_string, or use write_file with the entire corrected file."
            + exact_source
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


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


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


_PERMISSION_QUESTION_RE = re.compile(
    # "me" is optional: models phrase the same non-question both as "do you
    # want me to create X" and "do you want to create X" (observed live
    # 2026-08-02, the latter form slipped through and wrote nothing).
    r"\b(should i|shall i|shall we|do you want (?:me )?to|would you like (?:me )?to|"
    r"may i|can i|ok to|is it ok|proceed\?)\b",
    re.IGNORECASE,
)
# "Where should settings.py be created?" / "Which directory ...?" - a location
# question whose answer is the path already written in the request.
_LOCATION_QUESTION_RE = re.compile(
    r"\b(where|which director|which folder|what path|which path|what director)\w*\b",
    re.IGNORECASE,
)


def _asks_permission_already_granted(question: str, user_request: str) -> bool:
    """True when ask_user re-requests information the request already gave.

    A model answering "Create the file core/models.py" with "that file does not
    exist, should I create it?" - or "where should it be created?" - has asked
    nothing: the request is the answer, and each such turn ends having written
    nothing (observed live 2026-08-02, twice). A real choice between
    alternatives ("sessions or JWT?") is untouched.
    """
    text = str(question or "").strip()
    if not text:
        return False
    if _USER_CHOICE_RE.search(text):
        # "Should I use sessions or JWT?" is permission-shaped but is a real
        # decision between alternatives - that still belongs to the user.
        return False
    asks_permission = bool(_PERMISSION_QUESTION_RE.search(text))
    asks_location = bool(_LOCATION_QUESTION_RE.search(text))
    if not asks_permission and not asks_location:
        return False
    for match in _PROSE_FILE_TOKEN_RE.finditer(text):
        token = match.group(0)
        if not _request_explicitly_creates_path(user_request, token):
            continue
        if asks_permission:
            return True
        # A location question is answered only when the request states a path
        # with a directory, not just a bare filename.
        if _request_states_directory_for(user_request, token):
            return True
    return False


def _request_states_directory_for(user_request: str, token: str) -> bool:
    """Whether the request names *token* with an explicit directory component."""
    basename = _basename(token.replace("\\", "/")).lower()
    if not basename:
        return False
    pattern = re.escape(basename)
    for match in re.finditer(rf"[\w./\\-]*{pattern}", user_request, re.IGNORECASE):
        if "/" in match.group(0).replace("\\", "/").strip("/"):
            return True
    return False


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

EXCEPTION - file content is never JSON. Do NOT put source code in a JSON string.
Use the raw block form below, because escaping code inside JSON is what breaks
these calls. This is the write_file example the three above deliberately do not
give you: there is no reliable way to hand-escape a file, so do not try.
"""

# Injected for EVERY model, native-tool-capable or not. This is deliberate: the
# tier models all carry supports_native_tools=True and therefore never see
# _TOOL_PROTOCOL_PROMPT, yet they still route write calls through TEXT often
# enough that this is exactly where mutations were being lost. On 2026-08-03 a
# complete 736-char write_file call for templates/my_orders.html was discarded
# over one unescaped quote and reported as prose. output.py can now repair that
# after the fact; this stops it happening at all by removing the escaping step.
_RAW_WRITE_PROTOCOL_PROMPT = """
## File writes: send code as a RAW block, never as escaped JSON

Never put file content inside a JSON string. Escaping quotes and backslashes in
source code is the single biggest cause of a lost turn here: one wrong escape
discards the entire call and nothing is written.

To write a file, emit a fenced block whose FIRST line names the tool and the
path, then the content exactly as it must appear on disk:

```html
# write_file: templates/my_orders.html
{% extends "base.html" %}
<a href="{% url 'my_orders' %}">Orders</a>
```

Rules for that block:
- The header line is `# <tool>: <path>`, where <tool> is write_file, append_file,
  or edit_file. The path is relative to the workspace root.
- Everything between the header line and the closing fence is written VERBATIM.
  Do not escape quotes or backslashes. Do not write \\n for a newline - press
  enter. Do not indent the content to line up with the fence.
- write_file needs the COMPLETE file. append_file adds only new lines at the end
  of an existing file.
- For edit_file the body is one or more search/replace pairs:

```python
# edit_file: core/urls.py
<<<<<<< SEARCH
urlpatterns = []
=======
urlpatterns = [path("orders/", views.my_orders, name="my_orders")]
>>>>>>> REPLACE
```
- If the content itself contains a ``` line, open and close the block with FOUR
  backticks instead of three. Markdown files always need four.
- One block per file. Emit the blocks plus a one-line summary, nothing else.
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


def _system_prompt(
    workspace: Path,
    include_tool_protocol: bool = False,
    include_raw_write_protocol: bool = True,
    profile: PromptProfile | str = PromptProfile.SMALL,
    phase: ExecutionPhase | str | None = ExecutionPhase.EXPLORE,
    current_step: Any = None,
    available_tools: Iterable[str] = (),
) -> str:
    return compose_agent_prompt(
        workspace,
        profile=profile,
        phase=phase,
        current_step=current_step,
        available_tools=available_tools,
        include_tool_protocol=include_tool_protocol,
        include_raw_write_protocol=include_raw_write_protocol,
    )




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
    r"\b(?:create|write|save|add|update|edit)\b"
    r"[^\r\n`]{0,120}(?:to|in|at|as)?\s*[`'\"]?"
    r"[\w./\\-]+\.[A-Za-z0-9_]{1,12}",
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
