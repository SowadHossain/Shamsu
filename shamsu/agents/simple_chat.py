"""Simple mode: Ollama chat with coding tools attached.

The default path SHAMSU should have had. One conversation, a small system
prompt, seven tools, and a loop that does exactly this:

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
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.agents.chat_state import ChatState
from shamsu.agents.simple_log import SimpleTurnLog, next_turn_number
from shamsu.agents.simple_memory import MEMORY_TYPES, render_memory
from shamsu.agents.simple_prompt import simple_system_prompt
from shamsu.agents.simple_verify import PROBLEM as VERIFY_PROBLEM
from shamsu.agents.simple_verify import SKIPPED as VERIFY_SKIPPED
from shamsu.agents.simple_verify import check_file
from shamsu.context.budget import (
    PER_MESSAGE_OVERHEAD,
    message_tokens,
    RESERVE_OUTPUT_TOKENS,
    SAFETY_MARGIN_TOKENS,
    clamp_calibration,
    count_tokens,
    ctx_window_for_model,
    messages_tokens,
    tool_schema_tokens,
)
from shamsu.llm.output import parse_model_turn
from shamsu.runtime.models import model_for_role, model_is_reasoning
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

# How many times one run may tell the model "you said you would, so do it".
# Separate from MAX_PROSE_NUDGES because it is a different failure: the prose
# nudge fires when the model SHOWS the code instead of writing it, this one when
# it shows nothing at all and only promises. Live 2026-08-19 that ended 14 turns.
MAX_PROMISE_NUDGES = 2

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
# An existing file bigger than this is patched, not rewritten whole. Small
# models truncate, hallucinate imports and drift in indentation when asked to
# reproduce a file; `patch_file` costs the same whatever the file size, and
# cannot lose the parts nobody meant to touch. Measured 2026-08-19, the two
# worst rewrite payloads in one session were 2,618 and 2,231 tokens - the
# limit sits well below both, and well above a config file worth replacing.
#
# This is a PREFERENCE with an escape, unlike `_refuse_unwritable_rewrite`,
# which is a physical limit and has none.
# Messages kept byte-for-byte at the end of history. Inside one turn the model
# MUST see what it just did - when it cannot, it repeats itself, which is the
# 12 no-op patches and the 3 identical `list_files` calls found live. Across
# turns it needs the gist, not the bytes. Measured on a 130-message session:
#
#     keep verbatim | prompt tokens | turns before a 24k budget fills
#         everything |        44,833 | ~13   <- before
#            last 20 |        10,476 | ~57   <- here
#            nothing |         7,569 | ~79
#
# Twenty is the knee: nearly all of the reclaim, and the model still holds the
# whole of the edit it is in the middle of.
KEEP_VERBATIM_MESSAGES = 20

# Thresholds and approach adapted from smallcode `bin/smallcode.js` (~L1000),
# MIT, (c) 2026 Doorman11991 - see reference/smallcode/LICENSE.
# A tool_call argument longer than this is shortened in OLD messages. Keys are
# always kept, so the model still reads `write_file(filepath=game.js)` rather
# than a hole where a call used to be.
MAX_OLD_ARGUMENT_CHARS = 100
OLD_ARGUMENT_KEEP_CHARS = 80

# Tools whose result can be fetched again exactly. Eliding these is lossless:
# the file is still on disk, the listing can be re-listed, the search re-run.
# `run_command` is deliberately ABSENT - a test failure or a stack trace cannot
# be recovered by calling anything, so it is compacted head-and-tail instead of
# being thrown away.
RECOVERABLE_TOOLS = frozenset({"read_file", "list_files", "search_files", "write_file", "patch_file"})

# Lines kept at each end of an unrecoverable result (shell output).
COMMAND_OUTPUT_HEAD_LINES = 8
COMMAND_OUTPUT_TAIL_LINES = 12

# Tool calls between mid-turn elision sweeps. A long edit turn fills the window
# WITHIN the turn, and waiting for the next user message is too late.
ELIDE_EVERY_N_TOOL_CALLS = 3

# Fraction of the history budget above which a mid-turn sweep gets aggressive.
# SmallCode uses 0.6 of the detected window for the same decision. Below this
# the sweep keeps the normal verbatim tail; above it, the turn is on course to
# fill the window before it ever reaches the user, so it keeps only what the
# The 0.6 trigger is smallcode's (`bin/smallcode.js`), MIT, (c) 2026 Doorman11991.
# edit in progress needs.
ELIDE_PRESSURE_FRACTION = 0.6
# Elide down TO this fraction of the budget, then stop - smallcode uses
# `maxBudget * 0.7`. Leaves slack so the next few calls do not immediately
# trigger another sweep.
ELIDE_TARGET_FRACTION = 0.7
KEEP_VERBATIM_UNDER_PRESSURE = 8

# How many files keep their most recent read verbatim, however old it is.
#
# `RECOVERABLE_TOOLS` asks "can this be fetched again?". That is true of a file
# read and it is the wrong question - the right one is "what is this result
# still doing in the reasoning?". A read taken to check a claim is the evidence
# for that claim and stays load-bearing while the claim is live.
#
# The asymmetry is what made it self-reinforcing: the harness elided what it
# COULD re-fetch and kept what it could not, and the un-refetchable thing is the
# model's own speculation. Live 2026-08-19 the final prompt held 15 reads of
# main.js reduced to stubs and 8 surviving assistant sentences all asserting the
# same wrong diagnosis, so the model re-derived it - that was the only evidence
# left in the room.
#
# Bounded at four paths because this is the one thing elision may not reclaim,
# and superseded reads of the SAME path are still elided: fifteen stubs for one
# file is pure loss either way.
MAX_PROTECTED_READ_PATHS = 4

# And bounded by size as well as by count. The most recent read is always kept -
# it is the file the model is working on, and dropping it is the whole defect -
# but further paths are only added while the protected total stays under this
# fraction of the history budget.
#
# The bound is not the escape, though. Protection only stops a payload being
# SHRUNK; `select_for_budget` still evicts whole messages to fit the window, so
# a protected read that genuinely cannot fit falls out of the prompt entirely
# and no amount of protection can deadlock a turn.
PROTECTED_READS_MAX_FRACTION = 0.35

WHOLE_REWRITE_LIMIT_TOKENS = 400

MAX_DIFF_LINES = 40

# How often the live status line reports that a model call is still running.
HEARTBEAT_SECONDS = 5.0

_FENCE_RE = re.compile(r"```[^\n]*\n(?P<body>.*?)```", re.DOTALL)

# Messages pulled back from the session transcript per turn. Deliberately large:
# the token budget in `_messages` is the real limiter, and it can see the whole
# conversation to choose from. A small cap here is invisible and unrecoverable -
# it silently truncates history BEFORE anything gets to weigh it.
HYDRATE_MAX_MESSAGES = 400


def _prompt_is_active() -> bool:
    """Whether something is currently waiting on a typed answer."""
    try:
        from shamsu.safety.approval import prompt_is_active

        return prompt_is_active()
    except Exception:
        return False


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


# The most a single reply may generate, however much window is free.
#
# Without a ceiling, one looping generation can spend the entire window in a
# single call, and at 24 rounds that is a turn nobody will wait out. 16,384
# tokens is roughly 60KB - about 1,500 lines of JavaScript - which is far more
# than any file worth writing in one go. Anything genuinely larger is what the
# truncation refusal teaches: first section with write_file, then append_file
# per section.
MAX_REPLY_TOKENS = 16384

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
# Seven, named the way a model expects them to be named. Six map to a method the
# registry already implements, with its sandbox and ledger intact; `remember`
# writes the working-memory scratchpad and is handled in `_execute`.

SIMPLE_TOOLS: dict[str, str] = {
    "read_file": "read_file",
    "list_files": "list_files",
    "search_files": "grep_files",
    "write_file": "write_file",
    "patch_file": "edit_file",
    "run_command": "run_command",
    # Handled inside `_execute`, not by the registry: it writes a scratchpad,
    # not a workspace file, and routing it through `write_file` would put it
    # behind the patch-first guard and the sandbox path rules for no reason.
    # Handled inside `_execute`, not by the registry. Names and shapes follow
    # smallcode `bin/tools.js` so the vocabulary is one a model has likely met.
    "memory_remember": "memory_remember",
    "memory_load": "memory_load",
    "memory_list": "memory_list",
    "memory_forget": "memory_forget",
    "graph_search": "graph_search",
    "explain_symbol": "explain_symbol",
    # The archive is lossless on disk; this is what makes it reachable.
    "history_search": "history_search",
    # From smallcode `bin/tools.js`. `append_file` is the one that matters:
    # it gives the model a way to BUILD a large file (skeleton, then sections)
    # rather than only being told it may not rewrite one.
    "append_file": "append_file",
    "find_files": "find_files",
    # Stage 1 of two-stage routing. Never offered alongside the real tools -
    # `active_tool_schemas` sends this OR them, never both.
    "select_category": "select_category",
    # Composite tools, from smallcode `bin/tools.js`. Two round trips in one
    # call: on a 24-round budget at ~100s a round that is the difference
    # between finishing and stopping half way. The rule that makes them safe
    # is ours - see `_composite_tool`: a half-failure returns the half that
    # WORKED, so a wasted round is impossible.
    "find_and_read": "find_and_read",
    "search_and_read": "search_and_read",
    "read_and_patch": "read_and_patch",
    "create_and_run": "create_and_run",
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
            "description": "Search the workspace by meaning AND by pattern at once. Plain English works ('the function that validates tokens'), so does a regex. Returns ranked code locations with the symbol name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Text or regular expression to find."},
                    "path": {"type": "string", "description": "Directory to search. Defaults to the whole workspace."},
                    "mode": {
                        "type": "string",
                        "description": (
                            "hybrid (default, meaning + pattern), regex or keyword "
                            "for exact matches only, semantic for meaning only."
                        ),
                    },
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
    {
        "type": "function",
        "function": {
            "name": "memory_remember",
            "description": (
                "Save durable knowledge about this project: a decision, a workflow, a "
                "gotcha, a convention. Only things that should outlive this conversation "
                "- not a summary of what you just did."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(MEMORY_TYPES),
                             "description": "Which kind of knowledge this is."},
                    "title": {"type": "string", "description": "A few words naming the fact."},
                    "content": {"type": "string", "description": "The fact itself, one or two sentences."},
                    "tags": {"type": "array", "items": {"type": "string"},
                             "description": "Optional words that should bring this back later."},
                },
                "required": ["type", "title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_load",
            "description": (
                "Load what was remembered about this project that bears on a task - past "
                "decisions, workflows, conventions and gotchas. Worth calling before "
                "starting anything substantial."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "What you are about to do."}
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_list",
            "description": (
                "List everything remembered about this project."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "description": "Optional: only this kind."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_forget",
            "description": (
                "Delete one remembered note by its id, once it has become wrong."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The note id."}
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "graph_search",
            "description": (
                "Search the code graph for a symbol, function or class and get back where "
                "it lives. Answers where-is-the-auth-logic without reading files. Needs "
                "the workspace to have been indexed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Symbol name or concept."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_symbol",
            "description": (
                "Where a symbol is defined and WHO CALLS IT. The callers cannot be got "
                "from a text search - that finds the string, this finds the call."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "The symbol to explain."}
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "history_search",
            "description": (
                "Search everything ever said in this conversation, including turns long "
                "since dropped from the window and sessions this one was forked from. Use "
                "it when the user refers to something decided earlier that you cannot see."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What was being discussed."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": (
                "Add content to the END of an existing file. This is how you build a large "
                "file: write_file the first section, then append_file each one after. Far "
                "safer than rewriting a whole file, and it cannot be cut off partway."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path relative to the workspace."},
                    "content": {"type": "string", "description": "The text to add at the end."},
                },
                "required": ["filepath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": (
                "Find files by glob pattern, e.g. **/*.py or src/**/test_*.js."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern."}
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_and_read",
            "description": (
                "Find a file by glob and read it, in one call. Use when you know roughly "
                "what the file is called but not where it lives."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob, e.g. **/settings.py."}
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_and_read",
            "description": (
                "Search the code and read the best match, in one call. Plain English "
                "works. Use when you know what the code DOES but not where it is."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What the code does, or a pattern."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_and_patch",
            "description": (
                "Read a file and change one exact snippet in it, in one call. If the "
                "snippet does not match, you get the file contents back anyway, so you "
                "can see the real text and retry without spending another read."
            ),
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
            "name": "create_and_run",
            "description": (
                "Write a file and then run a command, in one call - typically to run the "
                "file you just wrote, or its tests. If the command fails you still get "
                "the error, and the file is still written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path relative to the workspace."},
                    "content": {"type": "string", "description": "The complete file content."},
                    "command": {"type": "string", "description": "The command to run afterwards."},
                },
                "required": ["filepath", "content", "command"],
            },
        },
    },
]

# Two-step tools. Listed here so `_execute` can route them before the registry
# lookup, and so the rule they share - a half-failure returns the half that
# worked - lives in one place.
_COMPOSITE_TOOLS = frozenset(
    {"find_and_read", "search_and_read", "read_and_patch", "create_and_run"}
)

# A hand-rolled `SHAMSU_TOOLSET=core` switch used to live here. smallcode
# routes on the CONTEXT WINDOW instead - narrow the tools when the window is
# tight, send everything when it is not - which is better reasoning than
# defaulting to narrow and hoping someone measures it. See `simple_router`.

def active_tool_schemas(
    context_window: int = 0, category: str = ""
) -> list[dict[str, Any]]:
    """The tools to send on this call.

    Everything, unless the window is tight enough that two-stage routing earns
    its extra round: then the category selector alone, and once the model has
    chosen, that category tools.
    """
    from shamsu.agents.simple_router import (
        category_selector_tool,
        routing_mode,
        tools_for_category,
    )

    if not context_window or routing_mode(context_window) == "direct":
        return SIMPLE_TOOL_SCHEMAS
    if not category:
        return [category_selector_tool()]
    return tools_for_category(category, SIMPLE_TOOL_SCHEMAS)


MUTATING_TOOLS = frozenset({"write_file", "patch_file"})

# Every call that puts bytes on disk. Wider than MUTATING_TOOLS on purpose:
# that set drives the no-op and repeated-edit counters, which only make sense
# for the two tools that report a diff. THIS set answers a different question -
# would executing this call, cut off mid-argument, damage the workspace? - and
# `append_file` belongs in it. Live 2026-08-19, `Round 9 append_file -> ok` sits
# directly above a cut-off notice in the log.
WRITING_TOOLS = frozenset(
    {"write_file", "patch_file", "append_file", "read_and_patch", "create_and_run"}
)

# Consecutive writes refused for arriving truncated, before the turn stops.
# The refusal tells the model to send the file in pieces; if it will not, the
# window is the wrong shape for this file and spinning proves nothing. Every
# guard needs an exit, and this is that guard's.
MAX_TRUNCATED_WRITE_REFUSALS = 3

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
    # The one-word spelling shipped first and a model will keep reaching for it.
    "remember": "memory_remember",
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


def make_approval_func(console_approval: Any, *, main_loop: Any = None) -> Any:
    """Approval policy for simple mode.

    Only shell commands can reach a prompt. File writes are already constrained
    to the workspace by the sandbox, which refuses an escape outright rather than
    asking about it, so a second question adds friction without adding safety.

    `main_loop` is the event loop the REPL runs on. Tools execute via
    `asyncio.to_thread`, so without it the prompt would read the console from a
    WORKER thread - and on Windows the entire input stack (prompt_toolkit's
    console session, `msvcrt`, Rich's Live) is owned by the main thread. That is
    the classic run_in_executor+stdin trap, and it is why a turn could sit at
    "Approval Required" forever. With it, the question is handed back to the
    main thread, which is idle inside `await` and free to run it.
    """

    def ask(request: Any) -> bool:
        if main_loop is not None and threading.current_thread() is not threading.main_thread():
            try:
                future = asyncio.run_coroutine_threadsafe(_ask_on_main(request), main_loop)
                return bool(future.result())
            except Exception:
                # The loop is gone or refused the callback; a prompt that runs
                # in the wrong place still beats an action taken unasked.
                return bool(console_approval(request))
        return bool(console_approval(request))

    async def _ask_on_main(request: Any) -> bool:
        # Deliberately blocking. Nothing else should run while a human is being
        # asked a question, and the heartbeat is suppressed for the same reason.
        return bool(console_approval(request))

    def approve(request: Any) -> bool:
        action = str(getattr(request, "action_type", "") or getattr(request, "action", "") or "")
        if action in {"file_write", "file_edit", "file_read"}:
            return True
        command = str(getattr(request, "command", "") or "")
        if action == "run_command" or command:
            if not command_needs_approval(command):
                return True
            return ask(request)
        return ask(request)

    return approve


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


# Bucket breakdown modelled on smallcode `marrow/src/context/budget.ms`
# (TokenAllocation), MIT, (c) 2026 Doorman11991.
@dataclass
class TokenAllocation:
    """Where the prompt actually goes, by category rather than as one number.

    One total tells you the window is full; it does not tell you what filled
    it, so the only available response is to drop the OLDEST messages. That is
    usually the wrong thing: measured on a real session the majority bucket was
    tool results, not conversation, and dropping oldest evicts a user's early
    decisions while leaving a 2,618-token `write_file` payload untouched.
    """

    system_prompt: int = 0
    tool_schemas: int = 0
    grounding: int = 0
    conversation: int = 0
    tool_results: int = 0

    @property
    def total(self) -> int:
        return (
            self.system_prompt
            + self.tool_schemas
            + self.grounding
            + self.conversation
            + self.tool_results
        )

    @property
    def buckets(self) -> dict[str, int]:
        return {
            "system prompt": self.system_prompt,
            "tool schemas": self.tool_schemas,
            "grounding": self.grounding,
            "conversation": self.conversation,
            "tool results": self.tool_results,
        }

    def fattest(self) -> str:
        """The bucket eviction should attack first."""
        return max(self.buckets.items(), key=lambda item: item[1])[0]


# Meter and compaction/eviction counters modelled on smallcode
# `bin/token_monitor.js`, MIT, (c) 2026 Doorman11991.
@dataclass
class ContextCounters:
    """What the context machinery actually did, per session.

    A whole session once re-compacted the same 23 messages every single turn
    and nobody noticed, because the only evidence was a line in the scrollback.
    A counter turns that class of bug from invisible into obvious: compactions
    should be rare, and a number that climbs once per turn is a bug on sight.
    """

    compactions: int = 0
    evictions: int = 0
    truncations: int = 0
    calls: int = 0
    # Cumulative, from smallcode `bin/token_monitor.js`. The ratio of these
    # two is the number that says whether the context work is paying off:
    # completion tokens are the useful output, prompt tokens are what it cost
    # to get them. A session whose efficiency falls as it runs is one where
    # the window is filling with things the model is not using.
    total_prompt: int = 0
    total_completion: int = 0
    # Ground truth from the most recent response, for the meter.
    last_prompt_tokens: int = 0
    last_window: int = 0
    last_estimate: int = 0

    @property
    def pct(self) -> int:
        if not self.last_window:
            return 0
        return round(100 * self.last_prompt_tokens / self.last_window)
    @property
    def efficiency(self) -> float:
        """Completion tokens per 100 prompt tokens. Higher is better."""
        if not self.total_prompt:
            return 0.0
        return 100.0 * self.total_completion / self.total_prompt

    @property
    def average_prompt(self) -> int:
        return round(self.total_prompt / self.calls) if self.calls else 0


    def meter(self) -> str:
        """`ctx 68% (22.3k/32.8k)` - driven by prompt_eval_count, not a guess."""
        if not self.last_window or not self.last_prompt_tokens:
            return "ctx --"
        return (
            f"ctx {self.pct}% "
            f"({self.last_prompt_tokens / 1000:.1f}k/{self.last_window / 1000:.1f}k)"
        )


# Process-wide, because a fresh SimpleChatLoop is built per user message while
# the REPL that reports these numbers lives for the whole session.
SESSION_COUNTERS = ContextCounters()

# The most recent per-category split, for `/context meter`. A dict rather than
# a bare global so the REPL reads whatever the last loop actually built.
LAST_ALLOCATION: dict[str, Any] = {"value": None}


@dataclass
class SessionStalls:
    """What has already failed in THIS conversation, and how often.

    Everything here used to live on `SimpleChatLoop`, and `repl.py` builds a
    fresh one per user message - so every stall counter reset the moment the
    user typed. `MAX_UNPRODUCTIVE_EDITS = 4` existed the whole time and would
    have caught the live failure; it never fired because the model failed four
    times, was prompted, and started again from zero. Live 2026-08-19 that was
    29 patch calls, 11 distinct payloads, one of them sent NINE times
    byte-for-byte, and the run only ended when `max_rounds` did.

    Keyed by session id, so `/new` gives a clean slate without the `/new`
    handler having to know this exists.
    """

    # signature -> how many times that exact call has failed
    failures: dict[str, int] = field(default_factory=dict)
    # signature -> the error it failed with, so a repeat can quote it
    errors: dict[str, str] = field(default_factory=dict)
    # Consecutive mutations that changed nothing, ACROSS user turns.
    unproductive: int = 0

    def record_failure(self, signature: str, error: str) -> int:
        seen = self.failures.get(signature, 0) + 1
        self.failures[signature] = seen
        self.errors[signature] = error
        return seen

    def forget(self, predicate: Callable[[str], bool]) -> None:
        """Drop remembered failures the world has moved past.

        The escape. A patch that could not match yesterday may match once the
        file it targets has actually changed, and a memory with no way out
        would make the first success in a file the last one.
        """
        for signature in [s for s in self.failures if predicate(s)]:
            self.failures.pop(signature, None)
            self.errors.pop(signature, None)


_SESSION_STALLS: dict[str, SessionStalls] = {}


def session_stalls(key: str) -> SessionStalls:
    """The stall record for one conversation, created on first use."""
    return _SESSION_STALLS.setdefault(key or "default", SessionStalls())


def reset_session_stalls(key: str = "") -> None:
    """Forget one conversation's stalls, or all of them. Tests and `/new`."""
    if key:
        _SESSION_STALLS.pop(key, None)
    else:
        _SESSION_STALLS.clear()


# Identical failing calls tolerated before the tool stops being run at all.
# The third attempt is refused: two is a retry, three is a loop. Live the same
# payload went out nine times and failed nine times identically.
IDENTICAL_FAILURES_BEFORE_REFUSING = 2


# Warn once per session on the way UP, rather than at the wall, where the only
# thing left to say is that the answer was already cut.
CONTEXT_WARN_FRACTION = 0.8


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
    # A write arrived cut off mid-argument and was refused rather than
    # committed. The loop reads this to know the turn made no progress for a
    # reason it must eventually stop on.
    refused_truncated: str = ""


class SimpleChatLoop:
    """One conversation, seven tools, no ceremony."""

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
        feedback: Any | None = None,
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
        # Paths already refused a whole-file rewrite once. The SECOND attempt
        # is honoured: a full rewrite is sometimes genuinely right, and a guard
        # with no exit is a deadlock waiting for a user. The partial-read guard
        # once had none and blocked writes forever.
        # Tool calls since the last mid-turn elision sweep.
        # The tool category the model has selected this turn, when the window
        # is small enough for two-stage routing. Cleared per turn: what it
        # needs next is rarely what it needed last.
        # Where anything the user types mid-turn arrives. None means nobody is
        # listening, which is the case for tests and embedders.
        self.feedback = feedback
        self._tool_category = ""
        self._calls_since_elide = 0
        # Said once per loop, not once per round.
        self._warned_filling = False
        # Rounds spent recovering rather than progressing: an empty reply, a
        # nudge, a no-op edit. Drives `_should_disable_thinking`.
        self._repair_attempts = 0
        # How many sweeps and how many messages they shrank, for `/status`.
        self.evictions = 0
        self._rewrite_refused: set[str] = set()
        # Files the model has seen only part of - writing them whole loses data.
        self._partial_reads: set[str] = set()
        # Line ranges seen per file, so reading a big file IN PIECES still
        # adds up to having seen it.
        self._seen_ranges: dict[str, list[tuple[int, int]]] = {}
        # Stalls that must OUTLIVE this object. A fresh SimpleChatLoop is built
        # per user message, so anything tracking "the model is repeating itself"
        # has to be keyed by the conversation or it resets whenever the user
        # types - which is exactly how one payload was sent nine times.
        self._stalls = session_stalls(
            getattr(session_logger, "session_id", "") or str(self.workspace)
        )
        # Consecutive writes refused for arriving truncated, and the file the
        # last one was aimed at - so the second refusal can say something the
        # first did not.
        self._truncated_refusals = 0
        self._truncated_target = ""
        # Successful edits per file this turn - repeated blind fixes.
        self._edits_per_file: dict[str, int] = {}
        # How many times each identical read-only call has been made this turn.
        self._read_signatures: dict[str, int] = {}
        # Readable per-turn transcript of prompt + raw response.
        self.turn_log: SimpleTurnLog | None = None
        self._files: list[str] = []
        self._brief = ""
        # The user request this turn is about. Memory is recalled AGAINST it -
        # a store of hundreds of notes puts five in the prompt, which is what
        # keeps a growing memory from becoming a growing tax on the window.
        self._request = ""
        # Ground truth from the last response, and the estimate that predicted
        # it. `prompt_eval_count` is the only number in this file that is not a
        # guess; everything else is calibrated against it.
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_estimate = 0
        self.last_done_reason = ""
        # The per-reply cap the last call actually carried, so a cut-off message
        # can name the limit that bound instead of guessing at the window.
        self._last_reply_cap = 0
        # Per-model correction factor, persisted in `.shamsu/`. Already built
        # and already used by `llm/manager.py`; simple mode talks to Ollama
        # directly, so it was the one caller estimating without ever checking.
        self._budget: Any = None
        try:
            from shamsu.context.manager import ContextBudgetManager

            self._budget = ContextBudgetManager(workspace=self.workspace)
        except Exception:  # noqa: BLE001 - budgeting must never break a turn
            self._budget = None
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
        # Before the budget is computed, and before compaction: hydration has
        # just reloaded the transcript from disk with every payload intact, and
        # eliding after budgeting would mean budgeting against bytes that are
        # about to be thrown away.
        self._request = user_input
        self._tool_category = ""
        self._elide_payloads()
        # Expand `@file` before the model sees a word. Otherwise the literal
        # string goes through and the first round is spent on a `read_file`
        # working out what it meant - or on a guess.
        #
        # The expansion is sent but NOT persisted: `Mentioned file context:` is
        # one of the internal markers the transcript strips, so a resumed
        # session replays what the user typed rather than a stale copy of a
        # file that has since changed.
        expanded = await asyncio.to_thread(expand_mentions, self.workspace, user_input)
        self.state.append_user(expanded, persisted_content=user_input)
        # Before compaction, not after: `_fixed_overhead` charges the grounding
        # block against the budget, and compaction decides what to evict from
        # that same budget. Computed after, compaction saw a workspace listing
        # of nothing and believed it had hundreds of tokens more than it did.
        self._files = await asyncio.to_thread(workspace_files, self.workspace)
        # Once per user message, not per round: the graph lookup costs ~2s and
        # what a file exports does not change between rounds of the same turn.
        self._brief = await asyncio.to_thread(codebase_brief, self.workspace, user_input)
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
        changed: list[str] = []
        tool_calls = 0
        prose_nudges = 0
        empty_nudges = 0
        promise_nudges = 0
        for round_index in range(self.max_rounds):
            # Before the model is called, never during: a message appended
            # while a tool call is in flight lands between the assistant turn
            # and its own result and orphans the tool_call_id.
            self._take_feedback()
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
                        self._repair_attempts += 1
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
                    if turn.thinking and not self._hit_the_length_limit():
                        # A COMPLETE thought with no visible content. Reasoning
                        # models really do end turns this way, and re-asking
                        # just burns another 30s - so it is used as the answer.
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
                    if turn.thinking:
                        # Thinking that was CUT OFF. This is the case that put a
                        # sentence ending mid-word - "...means `window.asteroid"
                        # - in front of a user as a finished answer, and then
                        # into the transcript as permanent conversation. It is
                        # an incomplete turn, and it is not an answer. The
                        # thought stays in the turn log and on screen; it never
                        # becomes history.
                        self._activity("model ran out of room mid-thought")
                        return self._stop(
                            self._out_of_room_message(),
                            round_index,
                            tool_calls,
                            changed,
                        )
                        return self._stop(
                            self._out_of_room_message(),
                            round_index,
                            tool_calls,
                            changed,
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
                if described and asks_only_for_words(self._request):
                    # Asked for a plan or a review, it planned. Nudging here
                    # tells it to abandon the deliverable and start writing.
                    described = ""
                if described and prose_nudges < MAX_PROSE_NUDGES:
                    # It showed the code instead of writing it. Say so once,
                    # naming the file, and let it act - the alternative is what
                    # the user saw: a perfect answer and an unchanged file.
                    prose_nudges += 1
                    self._repair_attempts += 1
                    self.state.append_assistant(text)
                    self.state.append_user(
                        f"You showed the new contents of {described} but did not change the file. "
                        f"Apply it now: call write_file for the complete new {described}, "
                        "or patch_file for one exact replacement. Do not repeat the code in prose."
                    )
                    self._activity(f"described a change to {described} without making it; asked it to apply")
                    continue
                promised = ""
                if not self._hit_the_length_limit():
                    # A reply the OUTPUT CAP severed also ends mid-sentence, and
                    # that is C1's case, not a broken promise. Only an intact
                    # turn that chose to stop here is one.
                    promised = ends_on_an_unmade_promise(text)
                if promised and promise_nudges < MAX_PROMISE_NUDGES:
                    # The defect the user actually experienced: "I told it to
                    # read files but nothing happened, the agent remained dumb."
                    # It was not dumb - it was cut off at the exact moment it was
                    # about to act, every time, and told that was a complete
                    # answer. 14 turns in one session ended on a colon with no
                    # tool call, and every one was handed back as finished.
                    promise_nudges += 1
                    self._repair_attempts += 1
                    self.state.append_assistant(text)
                    self.state.append_user(
                        f"Your reply ended on {promised!r} and then stopped. Nothing "
                        "followed it and you called no tool, so nothing happened.\n\n"
                        "Do it now, in this turn: call the tool that carries out what you "
                        "just said you would do. Do not say you are about to do it again."
                    )
                    self._activity("ended on a promise with no tool call; asked it to act")
                    continue
                if promised and promise_nudges >= MAX_PROMISE_NUDGES:
                    # The exit. Saying it a third time is not going to become
                    # doing it, and handing the promise back as an answer is the
                    # defect itself. `_HARNESS_STATUS_PREFIXES` in chat_state
                    # already filters this opening on rehydration, so the notice
                    # never becomes conversation the model learns to imitate.
                    return self._stop(
                        "I said I would take an action and then did not take it, "
                        f"{promise_nudges + 1} times in a row. The last thing I said was "
                        f"{promised!r}, and no tool call followed it, so nothing in the "
                        "workspace changed.\n\n"
                        "Ask me for the single next step - one file, one change - and I "
                        "will carry it out rather than announce it.",
                        round_index,
                        tool_calls,
                        changed,
                    )
                if self._hit_the_length_limit():
                    # The model was still speaking when the window ran out.
                    # Keep what it managed to say, but never present it as a
                    # finished answer - `done_reason` told us it was not.
                    text = self._out_of_room_message(text)
                    self._activity("reply hit the context limit; labelled it partial")
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
            if self._truncated_refusals >= MAX_TRUNCATED_WRITE_REFUSALS:
                # The guard's exit. Three replies in a row cut off mid-write
                # means the file does not fit in one generation and the model
                # will not break it up on being asked. Spinning on that proves
                # nothing, and every refusal costs a full round.
                return self._stop(
                    f"My last {self._truncated_refusals} attempts to write "
                    f"{outcome.refused_truncated or 'that file'} were cut off by my own "
                    "output limit part-way through, so I refused all of them rather than "
                    "leave a half-written file on disk. Nothing was changed.\n\n"
                    "The file is too large for me to produce in one reply. Ask me for one "
                    "part at a time - a single function, or one section - and I can build "
                    "it up.",
                    round_index,
                    tool_calls,
                    changed,
                )
            if self._stalls.unproductive >= MAX_UNPRODUCTIVE_EDITS:
                # Spinning. Live 2026-08-18 a turn ran 12 no-op patches and 5
                # failed ones across 24 rounds and ~25 minutes, changing nothing
                # - and the only thing that stopped it was max_rounds. Say what
                # happened instead of burning the rest of the budget.
                tried = self._stalls.unproductive
                # Reset now the user has been TOLD. The defect was a counter
                # that reset silently without ever tripping; one that stays hot
                # after it fires would stop the next turn before it started.
                self._stalls.unproductive = 0
                return self._stop(
                    f"I tried {tried} edits in a row that changed nothing - "
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

    def _warn_if_filling(self) -> None:
        """Say the window is filling on the way UP, once.

        At the wall the only thing left to say is that the answer was already
        cut. Said at 80% there is still room to act on it.
        """
        if self._warned_filling or not SESSION_COUNTERS.last_window:
            return
        if SESSION_COUNTERS.pct < CONTEXT_WARN_FRACTION * 100:
            return
        self._warned_filling = True
        self._activity(
            f"{SESSION_COUNTERS.meter()} - the conversation is filling the window. "
            "`/new` starts a fresh one; older file payloads are already elided."
        )

    def _should_think(self) -> bool:
        """Whether to ask for a reasoning trace on THIS call.

        Two independent questions, and only the second one used to be asked.

        **Can this model think at all?** Ollama rejects `think=` outright for a
        model without a reasoning channel - `does not support thinking`, status
        400, and the turn is over before a single token is generated. Live
        2026-08-19 against qwen2.5:3b-instruct that killed all five turns. The
        cookbook had recorded `is_reasoning=False` for it the whole time; simple
        mode simply never consulted it, so every non-reasoning model - which is
        most of the roster, the 8GB default included - could not run at all.

        An unknown model falls back to the family patterns in
        `runtime/models.py`, which answer False unless the name says otherwise.
        False is the safe default here: a reasoning model asked not to think
        still answers, while a plain model asked to think returns nothing.

        **Should it think on this particular call?** That is
        `_should_disable_thinking` below, unchanged.
        """
        if not model_is_reasoning(self.model_name):
            return False
        return not self._should_disable_thinking()

    def _should_disable_thinking(self) -> bool:
        """Whether this call should be made without a reasoning trace.

        Adapted from smallcode `src/model/thinking_budget.js`
        (`shouldDisableThinking`), MIT, (c) 2026 Doorman11991. Their reasoning,
        which matches what was measured here: on a retry the model "already
        overthought the original solution. A fast, low-creativity retry is
        better."

        It is also the cheapest fix for the specific failure that started all
        of this - a reasoning model spending its whole reply budget thinking
        and returning empty. After the first recovery round, stop paying for
        the reasoning that just failed to produce anything.

        `SHAMSU_THINKING_DISABLE=1` forces it off everywhere, their
        SMALLCODE_THINKING_DISABLE equivalent.
        """
        if os.environ.get("SHAMSU_THINKING_DISABLE", "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
        return self._repair_attempts > 1

    def _hit_the_length_limit(self) -> bool:
        """Did the last generation stop because it ran out of room?

        Ollama says so in `done_reason`, and the harness used to ignore it -
        so a reply cut off mid-word was displayed as a finished answer.
        """
        return self.last_done_reason.strip().lower() == "length"

    def _out_of_room_message(self, partial: str = "") -> str:
        """Say which limit actually bound, and give advice that fits it.

        The old message blamed the window every time. Live 2026-08-19 it told a
        user

            "I ran out of room to answer in. The prompt was 2,270 tokens of a
             32,768 window. ... `/new` starts a fresh conversation."

        on a conversation five messages long. The window was 7% full and was
        never the constraint; the reply hit its own per-reply cap. `/new` would
        have changed nothing, and the same sentence was then replayed into later
        prompts - RC3 counted one frozen copy of it 54 times - teaching the model
        that "I ran out of room" is how a turn ends.

        Two different failures now, and they need different advice. The window
        binding means the conversation really is too long. The reply cap binding
        means one answer was too long, and the fix is a smaller piece of work,
        not a fresh conversation.
        """
        used = self.last_prompt_tokens or self.last_estimate
        ceiling = self._ceiling()
        cap = self._last_reply_cap or output_reserve(ceiling)
        # The window is only the constraint when the prompt left less room than
        # the reply was allowed to use. Otherwise the cap stopped it, whatever
        # the window happened to be.
        window_bound = bool(used) and (ceiling - used) <= cap
        if window_bound:
            text = (
                f"I ran out of room to answer in. The conversation filled the window - "
                f"{used:,} tokens of {ceiling:,} - so there was little left to reply in."
                + chr(10) * 2 +
                "`/new` starts a fresh conversation, or ask for a smaller piece of this one."
            )
        else:
            where = f" The conversation is only using {used:,} of {ceiling:,} tokens, so the window is not the problem." if used else ""
            text = (
                f"That answer hit my per-reply limit of {cap:,} tokens - one reply "
                "cannot be longer than that." + where + chr(10) * 2 +
                "Ask for one piece at a time: a single function, or one section of the "
                "file, and I can build it up."
            )
        if partial.strip():
            cut = "**This answer was cut off.** "
            return partial.rstrip() + chr(10) * 2 + "---" + chr(10) * 2 + cut + text
        return text

    async def _client_chat(self, kwargs: dict[str, Any], timeout: float | None = None) -> Any:
        """One chat call, degrading past keywords the client does not know.

        Simpler clients - and the fakes in the tests - accept only the basics.
        Dropping an unsupported keyword and retrying keeps the turn alive; the
        old code did this for `tools` alone, which meant adding `think` would
        have turned an old client into a silent hard failure instead.

        Optional keys are shed one at a time, cheapest first, so a client that
        rejects `think` does not also lose its tools.
        """
        optional = ("think", "tools")
        attempt = dict(kwargs)
        for _ in range(len(optional) + 1):
            try:
                return await asyncio.wait_for(
                    self.client.chat(**attempt),
                    timeout=timeout or self.request_timeout,
                )
            except TypeError:
                shed = next((key for key in optional if key in attempt), None)
                if shed is None:
                    raise
                attempt.pop(shed, None)
        raise TypeError("the model client rejected every supported keyword")

    def _reply_cap(self, messages: list[dict[str, Any]], num_ctx: int) -> int:
        """How many tokens this reply may actually use.

        `output_reserve` is what the prompt assembler HOLDS BACK, and it is
        right about that. Using the same number as the generation CAP throws
        away every token the prompt did not spend.

        Live 2026-08-19 on qwen2.5:3b-instruct, mid-way through writing a file:

            "This answer was cut off. The prompt was 2,270 tokens of a 32,768
             window."

        The window was 7% full. 30,498 tokens were free and the reply was
        stopped at 8,192 - so the run produced nothing, and the message blamed
        the window that was never the constraint.

        Floored at the reserve, so this can never be smaller than it was;
        ceilinged at `MAX_REPLY_TOKENS`, so one looping generation cannot spend
        the whole window; and the safety margin covers the estimator, which runs
        about 15% heavy on qwen2.5 and must not be trusted to the last token.
        """
        free = num_ctx - self._estimate_prompt(messages) - SAFETY_MARGIN_TOKENS
        return max(output_reserve(num_ctx), min(free, MAX_REPLY_TOKENS))

    def _remember_reply_cap(self, messages: list[dict[str, Any]], num_ctx: int) -> int:
        """`_reply_cap`, recorded - the cut-off message has to name the real
        number, and computing it twice invites the two to drift apart."""
        self._last_reply_cap = self._reply_cap(messages, num_ctx)
        return self._last_reply_cap

    async def _call_model(self) -> Any:
        messages = self._messages()
        num_ctx = self._num_ctx(messages)
        kwargs = {
            "model": self.model_name,
            "messages": messages,
            "tools": active_tool_schemas(num_ctx, self._tool_category),
            "stream": False,
            "think": self._should_think(),
            "options": {
                "temperature": self.temperature,
                "num_ctx": num_ctx,
                # What is actually FREE, not a fixed share. See `_reply_cap`.
                "num_predict": self._remember_reply_cap(messages, num_ctx),
                # The system prompt survives an overflow the budget failed to
                # prevent. Ollama keeps 4 tokens by default; see `_num_keep`.
                "num_keep": self._num_keep(num_ctx),
            },
        }
        self._trace(
            "simple.model_call",
            f"{len(messages)} messages, num_ctx {num_ctx}",
            {"messages": len(messages), "num_ctx": num_ctx},
        )
        if self.turn_log:
            approx = self._estimate_prompt(messages)
            self.turn_log.log_call(messages, num_ctx, approx)
        started = time.perf_counter()
        beat = asyncio.ensure_future(self._heartbeat("thinking..."))
        try:
            raw = await self._client_chat(kwargs)
        except Exception as exc:
            if self.turn_log:
                self.turn_log.log_error(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            beat.cancel()
            self._activity(f"model responded in {time.perf_counter() - started:.0f}s")
        self._record_usage(raw, self._estimate_prompt(messages))
        if self.turn_log:
            self.turn_log.log_response(raw, time.perf_counter() - started)
        return raw

    def _record_usage(self, raw: Any, estimate: int) -> None:
        """Take the real prompt size off the response and learn from it.

        `prompt_eval_count` is ground truth - the only number here Ollama
        measured rather than we guessed. Recording it does two things: it feeds
        the per-model correction factor, and it gives the context meter a real
        number to show instead of the estimate that was wrong by 30%.
        """
        self.last_prompt_tokens = int(_response_field(raw, "prompt_eval_count") or 0)
        self.last_completion_tokens = int(_response_field(raw, "eval_count") or 0)
        self.last_done_reason = str(_response_field(raw, "done_reason") or "")
        self.last_estimate = estimate
        SESSION_COUNTERS.calls += 1
        SESSION_COUNTERS.total_prompt += self.last_prompt_tokens
        SESSION_COUNTERS.total_completion += self.last_completion_tokens
        SESSION_COUNTERS.last_window = self._ceiling()
        if self.last_prompt_tokens:
            # Only when the server actually reported one. A response without a
            # count must not blank the meter - the window did not empty, we
            # just were not told about it.
            SESSION_COUNTERS.last_prompt_tokens = self.last_prompt_tokens
            SESSION_COUNTERS.last_estimate = estimate
        if self._hit_the_length_limit():
            SESSION_COUNTERS.truncations += 1
        self._warn_if_filling()
        if self._budget is None or estimate <= 0 or self.last_prompt_tokens <= 0:
            return
        try:
            self._budget.calibrate_from_response(
                self.model_name, self.last_prompt_tokens, estimate
            )
        except Exception:  # noqa: BLE001 - a failed save must never break a turn
            pass

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
        ceiling = self._ceiling()
        _tail, start_abs = self.state.select_for_budget(self._history_budget(), count_tokens)
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
            SESSION_COUNTERS.compactions += 1
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
            raw = await self._client_chat(
                {
                    "model": self.model_name,
                    "messages": [{"role": "user", "content": ask}],
                    "stream": False,
                    "options": {
                        "temperature": 0.0,
                        "num_ctx": num_ctx or self._num_ctx([]),
                        # Six lines. Left unbounded, this call could spend the
                        # whole reply budget summarising.
                        "num_predict": 512,
                    },
                    # Mechanical pass: a digest of what was said needs no
                    # reasoning trace, and a reasoning model will happily spend
                    # every token it is given producing one. `_client_chat`
                    # sheds the keyword for clients that do not know it, so a
                    # narration is never lost to an unsupported argument.
                    "think": False,
                },
                min(self.request_timeout, 120.0),
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
        tail, start_abs = self.state.select_for_budget(self._history_budget(), count_tokens)
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
        messages = self.state.build_ollama_messages(
            tail, include_summary=self.state.should_include_summary(start_abs)
        )
        # Placed AFTER the system prompt and before the conversation, and rebuilt
        # every call, so it can never be the stale thing the model reasons from.
        # Two halves of one question: the listing says which files exist, the
        # brief says what is inside the ones this turn is about.
        grounding = render_workspace_files(self._files)
        remembered = render_memory(self.workspace, self._request)
        if remembered:
            grounding = remembered + chr(10) * 2 + grounding
        if self._brief:
            grounding = f"{grounding}\n\n{self._brief}"
        # LAST, not at position 1. llama.cpp reuses the KV cache for the
        # longest common PREFIX of the token sequence, and this block changes
        # whenever a file changes or the turn is about a different file.
        # Sitting second, one edit invalidated everything after ~150 tokens
        # and forced a full re-prefill of the whole conversation - measured
        # live: 46 sends, 6 distinct versions, against a 23,000-token prompt.
        # Appended, the cached prefix covers the system prompt AND every
        # earlier turn, and it lands next to the request it describes.
        # Just BEFORE the final user turn: the cache benefit is identical (the
        # prefix up to here is the system prompt plus every earlier turn, all
        # stable), while the request the model must act on stays last, where a
        # small model actually reads it.
        block = {"role": "system", "content": grounding}
        if len(messages) > 1 and messages[-1].get("role") == "user":
            messages.insert(len(messages) - 1, block)
        else:
            messages.append(block)
        LAST_ALLOCATION["value"] = self.token_allocation(messages)
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

    def _elide_payloads(self, keep_recent: int = KEEP_VERBATIM_MESSAGES) -> int:
        """Shrink tool payloads older than the last *keep_recent* messages.

        Called at the START of a turn as well as during one. A fresh
        `SimpleChatLoop` - and so a fresh `ChatState` - is built per user
        message, and hydration reloads the transcript from disk with every
        `write_file` payload intact. Eliding only what this turn produced would
        therefore save nothing at all across turns, which is precisely the case
        the 44,833 -> 10,476 measurement was taken on.
        """

        protected = self._current_file_reads()

        def make_elide(spare: set[int]):
            def elide(message: Any) -> tuple[str, list[dict[str, Any]]] | None:
                if id(message) in spare:
                    # The current contents of a file still under discussion.
                    # Dropping this is what left the model with nothing but its
                    # own wrong conclusions to reason from.
                    return None
                if message.role == "assistant" and message.tool_calls:
                    return message.content, _shorten_arguments(message.tool_calls)
                if message.role == "tool":
                    name = canonical_tool_name(message.name or "")
                    return elide_tool_result(name, message.content), message.tool_calls
                return None

            return elide

        # Stop as soon as the history is under target rather than eliding
        # everything past the cutoff. smallcode evicts to `maxBudget * 0.7`
        # for the same reason: a session only just over budget should keep
        # almost all of its detail.
        target = int(self._history_budget() * ELIDE_TARGET_FRACTION)

        def cost() -> int:
            return messages_tokens(m.to_ollama() for m in self.state.all_messages)

        changed = self.state.elide_old_payloads(
            keep_recent,
            make_elide(protected),
            target=target,
            cost_of=cost,
        )
        if changed:
            SESSION_COUNTERS.evictions += changed
            self._trace(
                "simple.elide",
                f"elided {changed} older tool payloads",
                {"messages": changed},
            )
        return changed

    def _current_file_reads(self) -> set[int]:
        """The messages holding what each file ACTUALLY says right now.

        One per path, newest first, capped. Everything older for the same path
        is superseded and elides normally - fifteen stubs of one file carry no
        information, and neither did the fifteen full copies.

        Identity rather than index: `elide_old_payloads` walks its own slice of
        the history, and an index computed here would silently point at the
        wrong message the moment that slice changed.
        """
        protected: set[int] = set()
        seen: set[str] = set()
        allowance = int(self._history_budget() * PROTECTED_READS_MAX_FRACTION)
        spent = 0
        for message in reversed(self.state.all_messages):
            if len(seen) >= MAX_PROTECTED_READ_PATHS:
                break
            if message.role != "tool" or message.elided:
                continue
            path = _read_result_path(message.content)
            if not path or path in seen:
                continue
            cost = message_tokens(message.to_ollama())
            if protected and spent + cost > allowance:
                # The most recent read is kept whatever it costs - it is the
                # file being worked on. Everything after it is a nice-to-have
                # and stops at the allowance.
                break
            seen.add(path)
            spent += cost
            protected.add(id(message))
        return protected

    def token_allocation(
        self, messages: list[dict[str, Any]] | None = None
    ) -> TokenAllocation:
        """Split the prompt that was actually SENT into the buckets making it up.

        Classifies the assembled message list rather than re-deriving it, and
        that is the whole point. Walking every stored message reported 42,440
        tokens of tool results inside a 23,595-token prompt. Re-running the
        selection instead was closer but still wrong by ~900, because
        `_messages` UPDATES the rolling summary while it builds - so the second
        run sees a different state from the one that was sent.

        Measure what you built. A meter that overstates is worse than none,
        because it gets believed.
        """
        built = self._messages() if messages is None else messages
        allocation = TokenAllocation(tool_schemas=tool_schema_tokens(self._sent_schemas()))
        for index, message in enumerate(built):
            cost = message_tokens(message)
            role = str(message.get("role") or "")
            if role == "system":
                # Position 0 is the standing prompt; any later system message
                # is injected context - the summary and the grounding block.
                if index == 0:
                    allocation.system_prompt += cost
                else:
                    allocation.grounding += cost
            elif role == "tool":
                allocation.tool_results += cost
            elif role == "assistant" and message.get("tool_calls"):
                # The CALL is conversation; the payload inside it is a tool
                # cost. Lumping the two hid the biggest items in the prompt
                # behind a label that looked like ordinary chat.
                text_only = count_tokens(str(message.get("content") or "")) + PER_MESSAGE_OVERHEAD
                allocation.conversation += text_only
                allocation.tool_results += max(0, cost - text_only)
            else:
                allocation.conversation += cost
        return allocation

    def _history_pressure(self) -> float:
        """How full the conversation already is, as a fraction of its budget."""
        budget = self._history_budget()
        if budget <= 0:
            return 1.0
        used = messages_tokens(m.to_ollama() for m in self.state.all_messages)
        return used / budget

    def _elide_under_pressure(self) -> int:
        """A mid-turn sweep, keeping less verbatim the fuller the window is.

        Cadence alone is not a trigger: sweeping every three calls in a short
        turn finds nothing older than the verbatim tail and does no work. What
        matters is whether this turn is on course to fill the window BEFORE it
        reaches the user, which is what happened live over 24 rounds and 18
        whole-file writes.

        Which bucket is fat decides how hard to sweep. Tool results are the
        majority on any edit-heavy session and are lossless to elide, so a fat
        one is worth attacking hard. When the conversation itself is the fat
        bucket, eliding harder reclaims almost nothing and costs the model the
        edit it is in the middle of - compaction is the right tool there, and
        it runs at the turn boundary.
        """
        if self._history_pressure() <= ELIDE_PRESSURE_FRACTION:
            return self._elide_payloads()
        allocation = self.token_allocation()
        if allocation.fattest() != "tool results":
            self._activity(
                f"context is filling and the largest part is {allocation.fattest()}; "
                "eliding payloads will not help much here"
            )
            return self._elide_payloads()
        self._activity("context is filling; eliding older tool payloads")
        return self._elide_payloads(KEEP_VERBATIM_UNDER_PRESSURE)

    def _take_feedback(self) -> bool:
        """Fold anything the user typed mid-turn into the conversation.

        A turn here can run 24 rounds - live sessions have spent 18 minutes on
        18 whole-file writes, and 25 minutes on 17 mutations that changed
        nothing. Until now the user could watch that happen or Ctrl-C and lose
        the turn. "You are editing the wrong file" is one sentence that saves
        twenty minutes, and there was nowhere to put it.
        """
        if self.feedback is None:
            return False
        try:
            said = self.feedback.drain()
        except Exception:  # noqa: BLE001 - never let a steer break the turn
            return False
        if not said:
            return False
        from shamsu.agents.simple_feedback import render_interjection

        # An ordinary user message: recorded, archived, and findable by
        # history_search later. A steer that changed a session and left no
        # trace makes the log unreadable six weeks on.
        self.state.append_user(render_interjection(said))
        for message in said:
            self._activity(f"you said: {message}")
        return True

    def _sent_schemas(self) -> list[dict[str, Any]]:
        """Exactly the schemas the next call will carry.

        The budget must charge what goes over the wire, not the full roster -
        under two-stage routing those differ by ~1,700 tokens, and item A is
        entirely about not letting the estimate and the request disagree.
        """
        return active_tool_schemas(self._ceiling(), self._tool_category)

    def _ceiling(self) -> int:
        """The context window this session asks Ollama for.

        The model's own window, capped by `max_ctx()`, and lowered again if the
        GPU has already refused something larger this run.
        """
        ceiling = min(ctx_window_for_model(self.model_name), max_ctx())
        if self._num_ctx_ceiling:
            ceiling = min(ceiling, self._num_ctx_ceiling)
        return ceiling

    def _calibration_factor(self) -> float:
        """How much bigger the real prompt tends to be than our estimate.

        Measured per model against `prompt_eval_count` and persisted. An
        estimate nobody checks against reality drifts, and drifts silently -
        which is how a prompt believed to be 21,381 tokens was really ~31,400.
        """
        if self._budget is None:
            return 1.0
        try:
            return clamp_calibration(self._budget.calibration_factor(self.model_name))
        except Exception:  # noqa: BLE001 - budgeting must never break a turn
            return 1.0

    def _fixed_overhead(self) -> int:
        """Tokens every request carries that `select_for_budget` cannot see.

        Three of them, and none was charged to any budget before:

          * the six tool schemas, ~630 tokens on EVERY call;
          * the grounding block, which `_messages` inserts AFTER the budget has
            already been spent - up to 80 file paths plus the codebase brief;
          * the rolling summary, prepended by `build_ollama_messages`, bounded
            at `summary_budget()` - a further 2,048 tokens at a 32k window.

        Counting only `tool_calls` and leaving these three out still overshoots
        by ~3,900 tokens, which is the difference between the ~8,192 of headroom
        the reply reserve promises and the ~5,300 it would actually deliver.
        """
        total = tool_schema_tokens(self._sent_schemas())
        grounding = render_workspace_files(self._files)
        remembered = render_memory(self.workspace, self._request)
        if remembered:
            grounding = remembered + chr(10) * 2 + grounding
        if self._brief:
            grounding = grounding + chr(10) + chr(10) + self._brief
        total += count_tokens(grounding) + PER_MESSAGE_OVERHEAD
        summary = self.state.rolling_summary
        if summary.strip():
            total += count_tokens(summary) + PER_MESSAGE_OVERHEAD
        return total

    def _history_budget(self) -> int:
        """Tokens the conversation itself may occupy.

        window - reply reserve - safety margin - everything else the request
        carries, then divided by the calibration factor so the budget is stated
        in the same units the estimator speaks. Dividing the budget once is the
        same as multiplying every estimate, and it keeps the correction in one
        readable place.
        """
        ceiling = self._ceiling()
        usable = (
            ceiling
            - output_reserve(ceiling)
            - SAFETY_MARGIN_TOKENS
            - self._fixed_overhead()
        )
        return max(1024, int(usable / self._calibration_factor()))

    def _num_ctx(self, messages: list[dict[str, Any]]) -> int:
        chosen = self._bucket_for(self._estimate_prompt(messages))
        self._num_ctx_floor = chosen
        return chosen

    def _num_keep(self, num_ctx: int) -> int:
        """Tokens Ollama must keep from the FRONT if the prompt ever overflows.

        The server's default is 4. Four. If a prompt does overflow, Ollama
        shifts the context by dropping from the front and keeps essentially
        nothing - so the first thing lost is the system prompt, and the model
        carries on with no idea what it is or which tools it has.

        The budget is supposed to make overflow impossible, and mostly does.
        This is the floor under that assumption: our estimate can be wrong (it
        was wrong by 9,500 tokens as recently as this week), and the failure
        mode when it is should not be a lobotomy.

        Clamped to an eighth of the window so a long system prompt cannot
        itself become the thing that starves the conversation.
        """
        wanted = count_tokens(self.state.system_prompt) + PER_MESSAGE_OVERHEAD
        return max(4, min(wanted, num_ctx // 8))

    def _estimate_prompt(self, messages: list[dict[str, Any]]) -> int:
        """Our best guess at what Ollama will report as `prompt_eval_count`.

        Content, `tool_calls`, the per-message envelope, and the tool schemas -
        everything that actually goes over the wire. Deliberately UNcalibrated:
        this is the raw estimate the correction factor is measured against, so
        applying the factor here would feed the correction back into its own
        input and converge on the square root of the truth instead of the truth.
        """
        return messages_tokens(messages) + tool_schema_tokens(self._sent_schemas())

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
        ceiling = self._ceiling()
        return max(ceiling, self._num_ctx_floor) if self._num_ctx_floor else ceiling

    # -- tools -----------------------------------------------------------

    async def _run_tools(self, calls: list[Any]) -> _Round:
        outcome = _Round()
        # Was this whole generation stopped by the output cap? If so its LAST
        # call is the one the cap severed - everything before it finished
        # generating - and a write whose arguments were cut off is not a write.
        cut_off = self._hit_the_length_limit()
        last = len(calls) - 1
        for index, call in enumerate(calls):
            name = canonical_tool_name(_call_name(call))
            arguments = normalize_arguments(name, _call_arguments(call))
            outcome.tool_names.append(name)
            if cut_off and index == last and name in WRITING_TOOLS:
                self._refuse_truncated_write(call, name, arguments, outcome)
                continue
            signature = _call_signature(name, arguments)
            if (
                name in WRITING_TOOLS
                and self._stalls.failures.get(signature, 0)
                >= IDENTICAL_FAILURES_BEFORE_REFUSING
            ):
                self._refuse_repeated_failure(call, name, arguments, signature)
                continue
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
            self._calls_since_elide += 1
            if self._calls_since_elide >= ELIDE_EVERY_N_TOOL_CALLS:
                # Mid-turn, not only between turns. A single edit turn can fill
                # the window on its own - live 2026-08-18 one ran 24 rounds and
                # 18 whole-file writes - and by the time the user speaks again
                # the truncation has already happened.
                self._calls_since_elide = 0
                self.evictions += self._elide_under_pressure()
            if name in WRITING_TOOLS:
                if result.ok:
                    # The world moved. A patch that could not match before may
                    # match now that this file has actually changed, so its
                    # remembered failures stop being true.
                    changed_path = _signature_path(signature)
                    self._stalls.forget(
                        lambda sig, p=changed_path: _signature_path(sig) == p
                    )
                else:
                    self._stalls.record_failure(signature, result.message)
            if name in MUTATING_TOOLS:
                if _changed_nothing(result):
                    self._stalls.unproductive += 1
                    self._repair_attempts += 1
                else:
                    self._stalls.unproductive = 0
            else:
                read_signature = f"{name}({_argument_summary(arguments)})"
                seen = self._read_signatures.get(read_signature, 0) + 1
                self._read_signatures[read_signature] = seen
                if seen >= REPEATED_READS_BEFORE_WARNING:
                    outcome.repeated_read = read_signature
            if result.ok and name in MUTATING_TOOLS:
                path = str(arguments.get("filepath") or "").strip()
                if path:
                    outcome.written.append(path)
                    count = self._edits_per_file.get(path.lower(), 0) + 1
                    self._edits_per_file[path.lower()] = count
                    outcome.repeated_edit = max(outcome.repeated_edit, count)
                    outcome.repeated_path = path if count >= EDITS_PER_FILE_BEFORE_WARNING else outcome.repeated_path
                # A write that landed intact ends the truncation streak. The
                # counter is about consecutive refusals, not lifetime ones.
                self._truncated_refusals = 0
                self._truncated_target = ""
        return outcome

    def _refuse_repeated_failure(
        self,
        call: Any,
        name: str,
        arguments: dict[str, Any],
        signature: str,
    ) -> None:
        """Do not run a call that has already failed identically.

        `MAX_UNPRODUCTIVE_EDITS = 4` would have caught this and never fired,
        because the counter lived on an object rebuilt per user message: the
        model failed four times, the user typed, and it started again from zero.
        Live 2026-08-19, 29 patch calls, 11 distinct payloads, one sent NINE
        times byte-for-byte with an identical failure each time.

        Running it a tenth time cannot produce a different answer - the file has
        not changed and neither have the arguments - so it is not run. What the
        model gets back instead is the fact of the repetition and the error it
        already had, which is the one thing it apparently could not see.
        """
        seen = self._stalls.failures.get(signature, 0)
        previous = (self._stalls.errors.get(signature) or "").strip()
        path = str(arguments.get("filepath") or arguments.get("path") or "").strip()
        named = path or "that file"
        message = (
            f"NOT RUN. This exact {name} call has already failed {seen} times in this "
            f"conversation with the same arguments, and {named} is unchanged.\n\n"
            f"What it said every time:\n{previous}\n\n"
            f"The same call will fail again. Call read_file on {named} and copy the "
            "text you want to replace out of the result, character for character, "
            "rather than writing it from memory."
        )
        result = ToolResult(
            False, message, {"refused": "identical_call_already_failed", "attempts": seen}
        )
        # Counted, so a model that keeps finding new ways to fail still reaches
        # the session-scoped stop rather than spending every round here.
        self._stalls.unproductive += 1
        self._repair_attempts += 1
        self._activity(f"{name} on {named} already failed {seen} times identically; not run")
        self._trace(
            "simple.refused_repeat", f"{name} {named}", {"tool": name, "attempts": seen}
        )
        if self.turn_log:
            self.turn_log.log_tool_result(name, arguments, False, message)
        self.state.append_tool(_call_id(call), name, _budgeted(result.to_json()))

    def _refuse_truncated_write(
        self,
        call: Any,
        name: str,
        arguments: dict[str, Any],
        outcome: _Round,
    ) -> None:
        """Do not put a severed generation on disk.

        `_hit_the_length_limit()` has existed and worked the whole time - it was
        only ever consulted where the PROSE answer is assembled, never where the
        writes happen. So the harness knew the reply had been cut off mid
        `write_file` and executed the partial call anyway, five rounds running,
        reporting each one as `ok`. That is how `game.js` reached 60 open braces
        against 39 closes.

        Refused rather than written-and-flagged, because `write_file` REPLACES.
        A truncated write does not produce a slightly short file; it destroys
        whatever was already there and leaves the half that fit. There is no
        recovering the rest - the model no longer has it either.

        The correction goes in the tool result, where the model is already
        looking, and it names the exact next call rather than describing a
        principle. Vague guidance cost 674s and a failure on 2026-08-18 where
        naming the call took 42s.
        """
        target = str(
            arguments.get("filepath") or arguments.get("path") or arguments.get("file") or ""
        ).strip()
        same_file = bool(target) and target == self._truncated_target
        self._truncated_refusals += 1
        self._truncated_target = target
        outcome.refused_truncated = target or name
        self._repair_attempts += 1

        named = target or "that file"
        message = (
            f"REFUSED - nothing was written and {named} is unchanged on disk.\n\n"
            f"Your reply hit the output limit part-way through this {name} call, so the "
            "content you sent is cut off mid-way. Writing it would replace the file with "
            "the half that fit and lose the rest permanently."
        )
        if same_file or self._truncated_refusals > 1:
            message += (
                "\n\nThis is the second time. The file is too big to emit in one reply. "
                f"Call write_file for {named} with the FIRST 60 LINES ONLY and stop there. "
                "Then call append_file once per following section, 60 lines at a time. "
                "Do not resend the whole file."
            )
        else:
            message += (
                f"\n\nSend it in pieces instead: call write_file for {named} with the first "
                "section only, then call append_file for each following section. One "
                "section per reply."
            )
        result = ToolResult(False, message, {"refused": "truncated_generation", "tool": name})
        self._activity(f"{name} arrived cut off; refused rather than writing a partial {named}")
        self._trace(
            "simple.refused_truncated",
            f"{name} {named}",
            {"tool": name, "filepath": target, "refusals": self._truncated_refusals},
        )
        if self.turn_log:
            self.turn_log.log_tool_result(name, arguments, False, message)
        self.state.append_tool(_call_id(call), name, _budgeted(result.to_json()))

    def _execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        name = canonical_tool_name(name)
        if name == "search_files":
            hybrid = self._hybrid_search(arguments)
            if hybrid is not None:
                return hybrid

        if name.startswith("memory_"):
            return self._memory_tool(name, arguments)
        if name in {"graph_search", "explain_symbol"}:
            return self._graph_tool(name, arguments)
        if name == "history_search":
            return self._history_search(arguments)
        if name == "append_file":
            return self._append_file(arguments)
        if name == "find_files":
            return self._find_files(arguments)
        if name == "select_category":
            from shamsu.agents.simple_router import TOOL_CATEGORIES

            chosen = str(arguments.get("category") or "").strip().lower()
            if chosen not in TOOL_CATEGORIES:
                # Do not strand it. An invented category still says the model
                # wants to act; `tools_for_category` answers with everything.
                self._tool_category = chosen or "read"
                return ToolResult(
                    True,
                    f"{chosen!r} is not a category, so you now have every tool. "
                    "Categories are: " + ", ".join(TOOL_CATEGORIES) + ".",
                    {"category": chosen},
                )
            self._tool_category = chosen
            names = ", ".join(TOOL_CATEGORIES[chosen]["tools"])
            return ToolResult(
                True,
                f"You now have the {chosen} tools: {names}. Call one.",
                {"category": chosen},
            )

        if name in _COMPOSITE_TOOLS:
            return self._composite_tool(name, arguments)
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
            blocked = self._prefer_patch_over_rewrite(
                str(arguments.get("filepath") or "").strip()
            )
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

    def _composite_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Two steps in one call, and never a wasted round.

        Shapes from smallcode `bin/tools.js`. The reason to want them is a
        small model's round budget: 24 rounds at ~100s each, and half of them
        spent on "find the file" then "read the file" is half a session gone
        on bookkeeping.

        The reason NOT to want them - and what stopped this being a port - is
        that each one doubles the ways a call can half-fail. A composite that
        errors when its second step misses is worse than two plain calls,
        because the model paid for the first step and got nothing.

        So the rule here, which is ours and not theirs: **a half-failure
        returns the half that worked.** `read_and_patch` whose patch does not
        match hands back the file contents, so the next attempt is computed
        from the real text rather than guessed at again - which is the
        measured failure this harness has (12 no-op patches in one turn).
        `create_and_run` whose command fails still wrote the file, and says
        so. The composite is then never worse than the two calls it replaces.
        """
        if name == "find_and_read":
            found = self._find_files({"pattern": arguments.get("pattern")})
            files = (found.data or {}).get("files") or []
            if not files:
                return found  # already explains the usual glob mistake
            target = files[0]
            read = self._execute("read_file", {"filepath": target})
            others = (
                f" ({len(files) - 1} other file(s) also matched: "
                + ", ".join(files[1:4])
                + ")"
                if len(files) > 1
                else ""
            )
            return ToolResult(
                read.ok,
                f"Matched {target}{others}." + chr(10) + chr(10) + read.message,
                {**(read.data or {}), "matched": files[:5], "read": target},
            )

        if name == "search_and_read":
            query = str(arguments.get("query") or arguments.get("pattern") or "")
            found = self._execute("search_files", {"query": query})
            matches = (found.data or {}).get("matches") or []
            if not matches:
                return found
            target = str(matches[0].get("file") or matches[0].get("filepath") or "")
            if not target:
                return found
            read = self._execute("read_file", {"filepath": target})
            runners_up = ", ".join(
                str(m.get("file") or "") for m in matches[1:4] if m.get("file")
            )
            note = f" Other candidates: {runners_up}." if runners_up else ""
            return ToolResult(
                read.ok,
                f"Best match for {query!r}: {target}.{note}"
                + chr(10) + chr(10) + read.message,
                {**(read.data or {}), "read": target, "candidates": matches[:5]},
            )

        if name == "read_and_patch":
            path = str(arguments.get("filepath") or "")
            patched = self._execute(
                "patch_file",
                {
                    "filepath": path,
                    "old_string": arguments.get("old_string"),
                    "new_string": arguments.get("new_string"),
                },
            )
            if patched.ok:
                return patched
            # The patch missed. Hand back the file rather than the refusal
            # alone: without it the model retries from memory, which is how a
            # turn ends up running twelve patches that change nothing.
            read = self._execute("read_file", {"filepath": path})
            if not read.ok:
                return patched
            return ToolResult(
                False,
                f"The patch did not apply: {patched.message}"
                + chr(10) + chr(10)
                + "Here is the file as it actually is - match against this "
                + "text rather than retrying the same snippet:"
                + chr(10) + read.message,
                {**(read.data or {}), "patch_failed": True, "filepath": path},
            )

        if name == "create_and_run":
            written = self._execute(
                "write_file",
                {"filepath": arguments.get("filepath"), "content": arguments.get("content")},
            )
            if not written.ok:
                return written  # nothing was created, so nothing to run
            ran = self._execute("run_command", {"command": arguments.get("command")})
            return ToolResult(
                ran.ok,
                written.message + chr(10) + chr(10) + "Then ran the command:"
                + chr(10) + ran.message,
                {**(ran.data or {}), "file_written": True,
                 "filepath": arguments.get("filepath")},
            )

        return ToolResult(False, f"There is no tool called {name}.", {"tool": name})

    def _history_search(self, arguments: dict[str, Any]) -> ToolResult:
        """Search everything ever said, not just what still fits.

        The transcript has always been lossless on disk and completely out of
        reach: older turns survive as a few summary lines, so a decision from
        turn three was gone the moment the window moved past it. Nothing was
        lost - it just could not be got back. This gets it back, at the cost
        of the handful of lines that answer the question.
        """
        query = str(arguments.get("query") or arguments.get("task") or "").strip()
        if not query:
            return ToolResult(False, "Say what to look for in the history.", {})
        if self.session_logger is None:
            return ToolResult(
                True,
                "This conversation is not being recorded, so there is no history to search.",
                {"matches": []},
            )
        try:
            from shamsu.session.history import render_hits, search_history

            hits = search_history(
                self.session_logger.manager,
                self.session_logger.session_id,
                query,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(False, f"Could not search the history: {exc}", {})
        return ToolResult(
            True,
            render_hits(hits, query),
            {"query": query, "matches": len(hits)},
        )

    def _append_file(self, arguments: dict[str, Any]) -> ToolResult:
        """Add to the end of a file, so a big one can be built in pieces.

        SHAMSU could refuse a whole-file rewrite and could patch an existing
        snippet, and between those two there was no way to GROW a file. A model
        told "that file is too large to rewrite" had no next move except a
        patch against text it had to guess at. smallcode gives the obvious
        third option and says so in the tool description: write the first
        section, append each one after.

        Costs the same whatever the file already holds, and cannot be cut off
        partway, because what is generated is only the new part.
        """
        path = str(arguments.get("filepath") or "").strip()
        content = arguments.get("content")
        if not path or not isinstance(content, str) or not content:
            return ToolResult(False, "append_file needs a filepath and content.", {})
        try:
            target = self.tools.sandbox.validate(path)
        except Exception as exc:  # noqa: BLE001 - the sandbox owns this refusal
            return ToolResult(False, str(exc), {"filepath": path})
        existed = target.exists()
        before = ""
        if existed:
            try:
                before = target.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                return ToolResult(False, f"Could not read {path}: {exc}", {"filepath": path})
        joiner = "" if (not before or before.endswith(chr(10))) else chr(10)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(before + joiner + content, encoding="utf-8", newline="")
        except OSError as exc:
            return ToolResult(False, f"Could not write {path}: {exc}", {"filepath": path})
        added = content.count(chr(10)) + 1
        total = (before + joiner + content).count(chr(10)) + 1
        return ToolResult(
            True,
            f"Appended {added} line(s) to {path}; it is now {total} lines."
            + ("" if existed else " (the file did not exist and was created)"),
            {"filepath": path, "added_lines": added, "total_lines": total},
        )

    def _find_files(self, arguments: dict[str, Any]) -> ToolResult:
        """Files matching a glob. `list_files` shows one directory; this hunts."""
        pattern = str(arguments.get("pattern") or arguments.get("query") or "").strip()
        if not pattern:
            return ToolResult(False, "Pass a glob pattern, e.g. **/*.py.", {})
        try:
            found = sorted(
                path.relative_to(self.workspace).as_posix()
                for path in self.workspace.glob(pattern)
                if path.is_file()
                and not any(part in _IGNORED_DIRS for part in path.parts)
            )
        except (OSError, ValueError) as exc:
            return ToolResult(False, f"Bad pattern {pattern!r}: {exc}", {"pattern": pattern})
        if not found:
            return ToolResult(
                True,
                f"No files match {pattern!r}. Note ** matches directories: use "
                "**/*.py rather than *.py to search below the root.",
                {"pattern": pattern, "files": []},
            )
        shown = found[:MAX_LISTED_FILES]
        body = chr(10).join(f"  {path}" for path in shown)
        if len(found) > len(shown):
            body += chr(10) + f"  ... [{len(found) - len(shown)} more]"
        return ToolResult(
            True,
            f"{len(found)} file(s) match {pattern!r}:" + chr(10) + body,
            {"pattern": pattern, "files": shown, "count": len(found)},
        )

    def _memory_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """The four memory tools, shaped as smallcode shapes them.

        Writing was the only half that existed. A model could record a
        decision and then had no way to ask what it had recorded, or to
        correct one that had gone stale - so memory was write-only, which is
        not memory.
        """
        from shamsu.agents.simple_graph import format_notes
        from shamsu.agents.simple_memory import MemoryStore, render_memory

        if name == "memory_remember":
            from shamsu.agents.simple_memory import remember

            tags = arguments.get("tags")
            ok, message = remember(
                self.workspace,
                str(arguments.get("type") or "context"),
                str(arguments.get("title") or ""),
                # `note` is the one-word spelling that shipped first and the
                # one a model reaches for anyway.
                str(arguments.get("content") or arguments.get("note") or ""),
                [str(t) for t in tags] if isinstance(tags, list) else None,
            )
            return ToolResult(ok, message, {"tool": name})

        if name == "memory_load":
            task = str(arguments.get("task") or arguments.get("query") or "")
            block = render_memory(self.workspace, task)
            return ToolResult(
                True,
                block or "Nothing remembered about this project bears on that.",
                {"tool": name},
            )

        if name == "memory_list":
            wanted = str(arguments.get("type") or "").strip().lower()
            notes = MemoryStore(self.workspace).all_notes()
            if wanted:
                notes = [note for note in notes if note.type == wanted]
            return ToolResult(True, format_notes(notes), {"tool": name, "count": len(notes)})

        if name == "memory_forget":
            ok, message = MemoryStore(self.workspace).forget(str(arguments.get("id") or ""))
            return ToolResult(ok, message, {"tool": name})

        return ToolResult(False, f"There is no tool called {name}.", {"tool": name})

    def _graph_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """Let the model ask the code graph SHAMSU already maintains.

        It indexes a workspace into a graph and then never gave the model a way
        to query it, so the model read files and guessed at what calls what -
        which is the thing the graph exists to prevent.
        """
        from shamsu.agents.simple_graph import explain_symbol, graph_search

        if name == "graph_search":
            ok, message = graph_search(
                self.workspace, str(arguments.get("query") or arguments.get("pattern") or "")
            )
        else:
            ok, message = explain_symbol(
                self.workspace, str(arguments.get("symbol") or arguments.get("query") or "")
            )
        return ToolResult(ok, message, {"tool": name})

    def _hybrid_search(self, arguments: dict[str, Any]) -> ToolResult | None:
        """Search by meaning and by pattern in one call.

        `grep_files` matched with `query in line` - a literal substring, not
        even a regex. So a model asking for `def handle_.*login`, or for "the
        function that validates tokens", was told "Found 0 match(es)" as though
        it had asked a fair question and got a truthful no. Both now work.

        Returns None to fall back to `grep_files` - an empty workspace, or any
        failure at all. A search that errors is worse than a plain one.
        """
        query = str(arguments.get("query") or arguments.get("pattern") or "").strip()
        if not query:
            return None
        mode = str(arguments.get("mode") or "hybrid").strip().lower()
        if mode not in {"hybrid", "regex", "keyword", "semantic"}:
            mode = "hybrid"
        try:
            from shamsu.indexer.policy import SOURCE_SUFFIXES, walk_workspace_files
            from shamsu.tools.hybrid_search import (
                format_results,
                hybrid_search,
                index_was_truncated,
            )

            # `walk_workspace_files` resolves its root, so the paths coming
            # back are absolute. Resolve ours too before relativising, or a
            # symlinked or differently-cased workspace raises instead of
            # searching.
            root = self.workspace.resolve()
            sub = str(arguments.get("path") or "").strip().strip("./")
            if sub and (root / sub).is_dir():
                root = (root / sub).resolve()
            files = []
            for found in walk_workspace_files(root, suffixes=SOURCE_SUFFIXES):
                try:
                    files.append(found.resolve().relative_to(root).as_posix())
                except ValueError:
                    continue
            if not files:
                return None
            results = hybrid_search(query, root, files, mode=mode, limit=10)
            truncated = index_was_truncated(root)
        except Exception:  # noqa: BLE001 - grep is always there
            return None
        if not results and mode == "hybrid":
            # Nothing scored at all. grep may still find a literal the
            # tokeniser threw away - punctuation, a stopword, a bare number.
            return None
        return ToolResult(
            True,
            format_results(results, query, mode, index_was_truncated(root)),
            {
                "query": query,
                "mode": mode,
                "matches": results,
                "count": len(results),
                "index_truncated": truncated,
            },
        )

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
        if not path:
            return None
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

    def _prefer_patch_over_rewrite(self, path: str) -> ToolResult | None:
        """Steer a whole-file rewrite of an existing file towards patch_file.

        Not a physical limit like `_refuse_unwritable_rewrite` - a preference,
        and the reasons are cost and safety rather than capacity. A small model
        reproducing a file truncates it, invents imports and drifts in
        indentation, and each attempt costs ~100s of generation; live
        2026-08-18 whole-file rewrites drove one turn to 18 minutes over 18
        rounds. `patch_file` costs the same whatever the file size.

        The correction goes in the ERROR STRING, not the system prompt: a small
        model acts on the message it just received, while a standing
        prohibition only dilutes the prompt it sits in.

        And it has an exit. A second attempt at the same path is honoured,
        because a full rewrite is sometimes exactly right and a guard the model
        cannot get past is a deadlock waiting for a user to notice.
        """
        key = path.lower()
        if key in self._rewrite_refused:
            self._activity(f"allowing the whole-file rewrite of {path} on the second attempt")
            return None
        try:
            existing = (self.workspace / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None  # a new file - there is nothing to patch
        if count_tokens(existing) <= WHOLE_REWRITE_LIMIT_TOKENS:
            return None  # small enough that rewriting it whole is honest
        self._rewrite_refused.add(key)
        return ToolResult(
            False,
            f"{path} already exists and is {len(existing.splitlines())} lines. Use "
            "patch_file to change just the part you mean - it is far faster and "
            "cannot lose the rest of the file. Call patch_file with the exact "
            "old_string you want replaced and the new_string to put there.",
            {"filepath": path, "prefer": "patch_file", "retry_allowed": True},
        )

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
        writable_chars = output_reserve(self._ceiling()) * 4
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
        self._record_verification(report)

    def _record_verification(self, report: str) -> None:
        """Put the verdict where the RUN OUTCOME can see it, not only the model.

        Live 2026-08-19 the verifier did its job perfectly - it caught a no-op
        write that left `js/main.js` unparseable, and said so in the tool result
        the model could read. The run then exited 0, because that verdict
        existed only in the chat transcript: `evidence_outcome()` saw a mutation
        applied and no verification event, so it reported success on a file that
        does not parse.

        The events and their supersede rule already exist for the legacy path.
        Keyed per FILE so `_has_unrecovered_verification_failure` can do what it
        was written for - a later good write of the same file clears the earlier
        failure, and only a file whose LAST verdict failed counts against the
        run.
        """
        if self.action_ledger is None:
            return
        try:
            parsed = json.loads(report)
            data = parsed.get("data") or {}
        except (ValueError, TypeError):  # pragma: no cover - defensive
            return
        for relative in data.get("checked") or []:
            self._log_verdict(True, str(relative), "")
        for problem in data.get("problems") or []:
            # "path: detail" - the path is what the verdict is keyed on, and a
            # problem without one still has to fail the run, so it gets the
            # whole string as its key rather than being dropped.
            text = str(problem)
            relative = text.split(":", 1)[0].strip() if ":" in text else text
            self._log_verdict(False, relative, text)

    def _log_verdict(self, passed: bool, relative: str, detail: str) -> None:
        try:
            self.action_ledger.log_event(
                "verification_passed" if passed else "verification_failed",
                verifier_id=f"syntax:{relative.lower()}",
                path=relative,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 - a ledger write must never end a turn
            pass

    def _verify(self, written: list[str]) -> str:
        """Parse what was just written, and claim only what was parsed.

        The old version appended the filename to `checked` BEFORE testing the
        extension, so every non-Python write came back as "no syntax errors"
        from a checker that never opened it - 572 such claims in the session of
        2026-08-19, on files with 21 unclosed braces. A false pass is worse than
        no check: it is the signal that told the model the truncated code was
        complete.

        Three outcomes now, and `skipped` is the escape - a `.md` file nobody
        can parse is reported as unchecked, never as a problem to repair.
        """
        problems: list[str] = []
        checked: list[str] = []
        skipped: list[str] = []
        for relative in dict.fromkeys(written):
            path = (self.workspace / relative).resolve()
            if not path.is_file():
                problems.append(f"{relative}: file was not created")
                continue
            verdict = check_file(path)
            if verdict.status == VERIFY_PROBLEM:
                problems.append(f"{relative}: {verdict.detail}")
            elif verdict.status == VERIFY_SKIPPED:
                skipped.append(f"{relative} ({verdict.detail})")
            else:
                checked.append(relative)
        if problems:
            return json.dumps(
                {"ok": False, "message": "Problems in the files just written.",
                 "data": {"problems": problems, "checked": checked, "skipped": skipped}},
                ensure_ascii=True,
            )
        if not checked and not skipped:
            return ""
        if checked:
            message = f"Checked {', '.join(checked)}: no syntax errors."
            if skipped:
                message += f" NOT checked: {', '.join(skipped)}."
        else:
            message = f"Nothing was syntax-checked. NOT checked: {', '.join(skipped)}."
        return json.dumps(
            {"ok": True, "message": message,
             "data": {"checked": checked, "skipped": skipped}},
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
                if _prompt_is_active():
                    # A tool is blocked on an approval prompt. Ticking here
                    # redraws over the question and the half-typed answer -
                    # which is why answering within 5s worked and waiting did
                    # not.
                    continue
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


def _response_field(response: Any, key: str) -> Any:
    """Read a top-level field off a chat response, dict or model object.

    Live, the ollama SDK returns a pydantic `ChatResponse`; the tests script
    plain dicts. Both carry `prompt_eval_count`, `eval_count` and
    `done_reason`, and a reader that understands only one of them reports zero
    for the other - which would make the calibration silently do nothing.
    """
    if isinstance(response, dict):
        return response.get(key)
    return getattr(response, key, None)


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


# Ways a model announces that it is about to act. Only ever consulted on the
# LAST line of a reply, where "about to" was never followed by anything.
_PROMISE_OPENERS = (
    "let me",
    "let's",
    "lets",
    "i'll",
    "i will",
    "i am going to",
    "i'm going to",
    "i need to",
    "i should",
    "now i",
    "next i",
    "first i",
    "here is what i",
    "here's what i",
)


# Verbs whose presence in a promise means WORK, not a request for information.
# Inflected explicitly rather than stemmed: `_CHANGE_VERBS` is matched on word
# boundaries elsewhere for a good reason - a substring match once made "it" mean
# "this names a product" - and "fixed" has to match without "prefixed" doing so.
_PROMISE_ACTIONS = (
    "fix", "fixes", "fixed", "fixing",
    "write", "writes", "writing", "wrote", "rewrite", "rewriting",
    "create", "creates", "creating",
    "add", "adds", "adding",
    "update", "updates", "updating",
    "change", "changes", "changing",
    "patch", "patches", "patching",
    "edit", "edits", "editing",
    "correct", "corrects", "corrected", "correcting",
    "apply", "applies", "applying",
    "replace", "replaces", "replacing",
    "remove", "removes", "removing",
    "delete", "deletes", "deleting",
    "implement", "implements", "implementing",
    "refactor", "refactors", "refactoring",
)


def ends_on_an_unmade_promise(text: str) -> str:
    """The announcement a turn ended on and never carried out, or ``""``.

    `describes_an_unmade_edit` is meant to catch this and cannot: it requires a
    fenced code block of four lines or more, so it only fires when the model
    SHOWS the code instead of writing it. Here the model shows nothing at all -
    it promises, and stops.

    Fourteen assistant turns in the session of 2026-08-19 ended this way:

        "...I'll use patch_file to replace just those two lines:"
        "...I'll read lines 420-435 to see exactly what needs to be replaced:"
        "...let me create a simple test to verify everything works:"

    Every one ends in a colon. The next thing should be a tool call; there is
    none, and the turn was handed back to the user as a finished answer. This is
    the defect the user actually experienced - *"I told it to read files but
    nothing happened, the agent remained dumb."* It was not dumb; it stopped at
    the exact moment it was about to act, and was told that was complete.

    Two halves are required, and the second half is not just the colon.

    The first version demanded one, because all fourteen examples in the report
    ended that way and it says so. Live 2026-08-19 on qwen2.5:3b-instruct, the
    model was handed an honest verify failure and answered

        "...I will ensure this is fixed."

    - a promise, no tool call, turn over, file still broken, and the guard sat
    silent because of the full stop. Small models do not punctuate like the one
    the report was written from.

    So: an announcement of intent, and then either a colon OR a verb that
    changes something. The second arm is what separates "I am about to edit a
    file" from "I will need more information", which is a legitimate way to end
    a turn - it is a question to the user, not an unmade edit.
    """
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    last = lines[-1]
    if last.startswith(("#", "|", ">", "```")):
        # A heading or a table row introduces a section that was cut, which is
        # a different problem and not a promise to act.
        return ""
    lowered = f" {last.lower()} "

    def names(vocabulary) -> bool:
        return any(
            re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered)
            for word in vocabulary
        )

    if not names(_PROMISE_OPENERS):
        return ""
    if last.endswith(":") or names(_PROMISE_ACTIONS):
        return last
    return ""


# Verbs that ask for WORDS, and verbs that ask for a CHANGE. Matched on word
# boundaries: `_PRD_BUILD_NOUNS` once held "it" as a raw substring, which made
# almost any English sentence "name a product". The same mistake here would
# silently switch off the one guard that makes the model act.
_WORDS_VERBS = (
    "plan",
    "review",
    "explain",
    "describe",
    "outline",
    "summarize",
    "summarise",
    "analyse",
    "analyze",
    "assess",
    "compare",
    "suggest",
    "recommend",
    "propose",
    "what would you",
    "how would you",
)
_CHANGE_VERBS = (
    "implement",
    "apply",
    "fix",
    "write",
    "create",
    "add",
    "change",
    "update",
    "edit",
    "patch",
    "refactor",
    "rename",
    "delete",
    "remove",
    "build",
    "make",
)


def asks_only_for_words(request: str) -> bool:
    """True when the deliverable is prose, so showing code is not a failure.

    `describes_an_unmade_edit` assumes prose plus a code block means the model
    answered and skipped the job. Asked to "review the PRD and plan the next
    steps" that is exactly backwards: describing the change IS the deliverable,
    and the nudge told the model to stop planning and start writing - the same
    presumption that cost 24 rounds and 577s before the system prompt was made
    conditional (live 2026-08-18).

    Deliberately asymmetric. A words-verb only counts when NO change-verb is
    present, so "review the code and fix the bug" still nudges. Skipping the
    nudge wrongly means the work silently does not happen; nudging wrongly
    costs one round. The cheap error is the one to prefer.
    """
    lowered = f" {(request or '').lower()} "

    def names(vocabulary: tuple[str, ...]) -> bool:
        return any(
            re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", lowered)
            for word in vocabulary
        )

    return names(_WORDS_VERBS) and not names(_CHANGE_VERBS)


def _shorten_arguments(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shrink long argument VALUES in an old tool call, keeping every key.

    Once the result has come back the model does not need the whole file it
    asked to write - but it does still need to see that it wrote that file.
    Keeping the keys means the call still reads as
    `write_file(filepath=frontend/game.js)` instead of a hole in the history.
    """
    shortened: list[dict[str, Any]] = []
    for call in tool_calls:
        function = dict(call.get("function") or {}) if isinstance(call, dict) else {}
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                arguments = {}
        if isinstance(arguments, dict):
            function["arguments"] = {
                key: (
                    value[:OLD_ARGUMENT_KEEP_CHARS] + " ...[elided]"
                    if isinstance(value, str) and len(value) > MAX_OLD_ARGUMENT_CHARS
                    else value
                )
                for key, value in arguments.items()
            }
        updated = dict(call) if isinstance(call, dict) else {}
        updated["function"] = function
        shortened.append(updated)
    return shortened


def _middle_out(text: str, head: int, tail: int) -> str:
    """Keep both ends of an unrecoverable result, name what went missing.

    Shell output is the one payload `read_file` cannot fetch back, and the two
    ends are where the answer is: the command that ran, and how it failed.
    """
    body = text.splitlines()
    if len(body) <= head + tail + 1:
        return text
    omitted = len(body) - head - tail
    kept = body[:head] + [f"... [{omitted} lines elided - re-run to see them] ..."] + body[-tail:]
    return chr(10).join(kept)


def _call_signature(name: str, arguments: dict[str, Any]) -> str:
    """A stable identity for one tool call, arguments and all.

    `_argument_summary` truncates, which is right for a status line and wrong
    here: two 4,000-character patches differing only at the end would share a
    summary and be mistaken for the same call. The digest is over the whole
    argument set.

    The path is kept readable in front of the digest so a successful edit can
    find and forget every failure recorded against that file.
    """
    path = str(arguments.get("filepath") or arguments.get("path") or "").strip().lower()
    try:
        body = json.dumps(arguments, sort_keys=True, ensure_ascii=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        body = repr(sorted(arguments.items(), key=lambda item: str(item[0])))
    digest = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{name}|{path}|{digest}"


def _signature_path(signature: str) -> str:
    """The file a signature was recorded against, or ``""``."""
    parts = signature.split("|")
    return parts[1] if len(parts) >= 3 else ""


def _read_result_path(payload: str) -> str:
    """The file whose CONTENTS this tool result carries, or ``""``.

    Keyed on the payload rather than the tool name so the composites count too:
    `find_and_read` and `search_and_read` spread the `read_file` data into their
    own result, so they hand the model a file body under a different name.

    A result that has already been elided down to a stub is not evidence of
    anything - it says so itself - so it is not a candidate to protect.
    """
    if '"resolved_filepath"' not in payload:
        return ""
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return ""
    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, dict) or "elided" in data:
        return ""
    if "content" not in data and "lines" not in data and "text" not in data:
        # A write echo also carries `resolved_filepath`, and it is not a read.
        # What distinguishes a read is that the body came back with it.
        return ""
    return str(data.get("resolved_filepath") or "").strip().lower()


def elide_tool_result(name: str, payload: str) -> str:
    """What an old tool result is worth keeping as.

    A `read_file` body and a `write_file` echo have both served their purpose
    the moment the call returns, and the file is still on disk. What survives
    is the fact of the call and its outcome - including, for a mutation, the
    diff `_with_diff` already computed, which is the part the model reasons
    from next.
    """
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return _middle_out(payload, COMMAND_OUTPUT_HEAD_LINES, COMMAND_OUTPUT_TAIL_LINES)
    if not isinstance(parsed, dict):
        return payload
    if name not in RECOVERABLE_TOOLS:
        # Not recoverable by any call - compact it, never drop it.
        message = str(parsed.get("message") or "")
        parsed["message"] = _middle_out(
            message, COMMAND_OUTPUT_HEAD_LINES, COMMAND_OUTPUT_TAIL_LINES
        )
        data = parsed.get("data")
        if isinstance(data, dict):
            for key in ("stdout", "stderr", "output", "content"):
                if isinstance(data.get(key), str):
                    data[key] = _middle_out(
                        data[key], COMMAND_OUTPUT_HEAD_LINES, COMMAND_OUTPUT_TAIL_LINES
                    )
        return json.dumps(parsed, ensure_ascii=True)
    # Recoverable. Keep the verdict and the diff; drop the bytes.
    message = str(parsed.get("message") or "")
    kept: dict[str, Any] = {"ok": parsed.get("ok", True), "message": message}
    data = parsed.get("data")
    if isinstance(data, dict):
        summary = {
            key: data[key]
            for key in ("resolved_filepath", "total_lines", "diff_lines", "matches")
            if key in data
        }
        summary["elided"] = "call read_file for the current contents"
        kept["data"] = summary
    return json.dumps(kept, ensure_ascii=True)


# An `@file` is expanded before the model sees a word, but it must not be able
# to swallow the window on its own. Same order as a tool result, and the model
# can always `read_file` the rest.
MAX_MENTION_TOKENS = 4000


def expand_mentions(workspace: Path, text: str) -> str:
    """Turn `@file` into the file, before any tokens are spent.

    The user typed `@ASTEROID_SHOOTER_SHAMSU_BUILD_SPEC.md` and the literal
    string was passed straight through: the model had to spend a whole round
    calling `read_file` to find out what it referred to, and sometimes guessed
    at it instead. `MentionResolver` already did this for the legacy path and
    simple mode was never wired to it.

    Best-effort: an unresolvable mention leaves the text exactly as typed,
    because a mention that is really just an email address or a decorator must
    not turn a request into a file dump.
    """
    try:
        from shamsu.tools.workspace import MentionResolver, render_mention_context

        contexts = MentionResolver(workspace).resolve_all(text or "")
    except Exception:  # noqa: BLE001 - never let grounding break a turn
        return text
    usable = [c for c in contexts if c.resolved or c.error or c.kind == "ambiguous"]
    if not usable:
        return text
    rendered = render_mention_context(usable)
    if not rendered.strip():
        return text
    # Two things can shorten this: our cap, and MentionResolver own cap. Either
    # way the model ends up holding part of a file, and must be told how to get
    # the rest - a truncated mention with no recovery route is how a turn ends
    # up answering from half a spec.
    clipped = "[truncated" in rendered
    if count_tokens(rendered) > MAX_MENTION_TOKENS:
        rendered = rendered[: MAX_MENTION_TOKENS * 4]
        clipped = True
    if clipped:
        rendered += (
            chr(10)
            + "... [only part of this file is shown - call read_file with "
            + "start_line/end_line for the rest]"
        )
    return text + chr(10) * 2 + "Mentioned file context:" + chr(10) + rendered


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
    main_loop: Any = None,
) -> AgentToolRegistry:
    """An AgentToolRegistry wired for simple mode's approval policy."""
    return AgentToolRegistry(
        workspace,
        approval_func=make_approval_func(console_approval, main_loop=main_loop),
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
    "active_tool_schemas",
    "SIMPLE_TRANSCRIPT_TOOLS",
    "SimpleChatLoop",
    "SimpleChatResult",
    "TokenAllocation",
    "build_simple_tools",
    "codebase_brief",
    "command_needs_approval",
    "asks_only_for_words",
    "describes_an_unmade_edit",
    "ends_on_an_unmade_promise",
    "expand_mentions",
    "render_memory",
    "make_approval_func",
    "names_a_workspace_file",
    "normalize_arguments",
    "render_workspace_files",
    "simple_mode_enabled",
    "workspace_files",
]
