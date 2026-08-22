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
from functools import lru_cache
from hashlib import sha256
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.agents.chat_state import ChatState
from shamsu.agents.loop_guards import (
    LOOKING_TOOLS,
    READ_LOOP_EXHAUSTED,
    adapted_temperature,
    ReadLoopDetector,
    TrustDecay,
    closest_tool_names,
    greeting_regression,
    invented_capability_hint,
    leaked_tool_call,
)
from shamsu.agents.plan_anchor import anchor as plan_anchor
from shamsu.agents.plan_anchor import ask_for_a_plan
from shamsu.agents.simple_memory import MEMORY_TYPES, render_memory
from shamsu.agents.simple_outline import (
    can_outline,
    find_symbol,
    find_symbols,
    outline,
    render_outline,
)
from shamsu.agents.simple_prompt import simple_system_prompt
from shamsu.safety.approval_context import get_approval_override
from shamsu.agents.simple_tests import detect_test_command
from shamsu.agents.simple_verify import PROBLEM as VERIFY_PROBLEM
from shamsu.agents.simple_verify import SKIPPED as VERIFY_SKIPPED
from shamsu.agents.simple_verify import (
    check_file,
    truncation_signature,
    unfinished_blocks,
)
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
from shamsu.runtime.turn_stream import TurnEvent
from shamsu.session.manager import ORIGIN_LOOP, SessionLogger
from shamsu.tools.agent_tools import (
    ELIDED_VALUE,
    LEGACY_ELISION_MARKER,
    MAX_READ_CHARS,
    AgentToolRegistry,
    looks_elided,
)
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
#
# A CEILING now, not the cap itself - see `tool_result_budget`. Flat, it was
# 24% of a 32k window and 97.7% of an 8k one, and `_shrink_for_oom` walks
# sessions INTO 8k. This is the same defect `output_reserve` already fixed once
# ("a fixed 4096 reserve is what starved simple mode"): a number that is right
# at one window size and silently wrong at every other.
MAX_TOOL_RESULT_TOKENS = 8000

#: The share of the window one tool result may occupy. A quarter leaves three
#: quarters for the system prompt, the schemas, the conversation and the reply -
#: and a file worth editing is genuinely worth a quarter.
TOOL_RESULT_WINDOW_SHARE = 4

#: Below this a result is a fragment rather than a smaller answer, so the floor
#: holds even when the share would go lower. At the 4096 minimum window this is
#: what binds, and it should: the honest reading is that the window is too small
#: for the file, which `_refuse_unwritable_rewrite` and the read guard say out
#: loud rather than by silently truncating.
TOOL_RESULT_FLOOR_TOKENS = 1500


def tool_result_budget(ceiling: int | None = None) -> int:
    """How many tokens one tool result may carry, for this window."""
    window = ceiling or max_ctx()
    return int(
        max(
            TOOL_RESULT_FLOOR_TOKENS,
            min(MAX_TOOL_RESULT_TOKENS, window // TOOL_RESULT_WINDOW_SHARE),
        )
    )

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

# ...and twenty is the knee for CONVERSATION. It is the wrong unit for this.
#
# The measurement above assumed conversational messages. Measured again over 77
# prompts of a real file-writing session, where a message can be an entire file:
#
#     older (elidable)   4,655 messages   2,352,729 chars    505 chars/msg
#     last-20 verbatim   1,475 messages   2,485,838 chars  1,685 chars/msg
#
# The protected tail was 24% of the messages and 51% of the CONTENT - 87% in the
# worst prompt, where one single assistant message was 25,473 characters. Elision
# was working perfectly and was simply not allowed to touch any of it.
#
# So the tail is bounded by tokens instead: twenty small turns still stay whole,
# and three whole-file writes do not. Bounded above by KEEP_VERBATIM_MESSAGES so
# this can only ever shrink the tail, never grow it, and below by the current
# exchange - a model that cannot see the edit it is in the middle of is worse
# off than one paying for it.
VERBATIM_TAIL_FRACTION = 0.35
VERBATIM_TAIL_FRACTION_UNDER_PRESSURE = 0.15
MIN_VERBATIM_MESSAGES = 4

# Thresholds and approach adapted from smallcode `bin/smallcode.js` (~L1000),
# MIT, (c) 2026 Doorman11991 - see reference/smallcode/LICENSE.
# A tool_call argument longer than this is shortened in OLD messages. Keys are
# always kept, so the model still reads `write_file(filepath=game.js)` rather
# than a hole where a call used to be.
MAX_OLD_ARGUMENT_CHARS = 100
OLD_ARGUMENT_KEEP_CHARS = 80

# Argument keys whose VALUE is file content the model might send back verbatim.
#
# These are dropped from an old call rather than shortened. Live 2026-08-21 a
# 9B lost half its patches to the difference: a truncated `old_string` reads
# like the text it was, so the model retried by copying it out of its own
# history - including the ` ...[elided]` the harness had appended - and
# `patch_file` could only answer "old_string not found". Three attempts at the
# same function, the third one carrying our own marker as if it were code.
#
# A shortened value invites that. A missing one cannot be copied at all.
CONTENT_ARGUMENT_KEYS = frozenset({"old_string", "new_string", "content", "body", "text"})

#: `ELIDED_VALUE`, `LEGACY_ELISION_MARKER` and `looks_elided` come from
#: `shamsu.tools.agent_tools`: the loop produces these and `patch_file` refuses
#: them, and the refusing end owns the definition.

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
    # Then whatever was chosen in the CLI, the web portal or Telegram - one
    # setting, install-wide, so changing it in one surface is not undone by
    # opening another. The env var still wins: an operator who exported it did
    # so for a reason.
    try:
        from shamsu.runtime.settings import chat_max_ctx

        chosen = chat_max_ctx()
    except Exception:  # noqa: BLE001 - a bad settings file is not a dead model
        chosen = None
    if chosen:
        return chosen
    return 32768


def output_reserve(ceiling: int) -> int:
    """Tokens held back for the model's reply, as a SHARE of the window.

    A fixed 4096 reserve is what starved simple mode: at num_ctx 32768 the
    prompt was allowed to grow to 28160 and the reply - thinking AND answer
    together - got the same 4096 it would have had at 8k. A reasoning model
    spends that thinking and emits nothing, which the loop then read as an
    empty reply and nudged, forever.

    A quarter of the window scales with it: 8k -> 4096, 32k -> 8192.

    But the FLOOR must not outgrow the window it is a share of. Taken as a bare
    `max(4096, ceiling // 4)` this returned 4096 at every window below 16k -
    50% of an 8k window and **100% of a 4k one**, leaving nothing at all for the
    prompt. It was unreachable while 32k was the only setting anyone used;
    `/context window` makes it reachable, and `_shrink_for_oom` was already
    walking sessions down into it.

    So the floor applies only where it still leaves room to think: capped at a
    third of the window. A model given a third of 4k to answer in is
    constrained, which is true and survivable. One given all of it cannot be
    sent a prompt.
    """
    quarter = ceiling // 4
    if quarter >= RESERVE_OUTPUT_TOKENS:
        return quarter
    return max(1, min(RESERVE_OUTPUT_TOKENS, ceiling // 3))


# The most a single reply may generate, however much window is free.
#
# Without a ceiling, one looping generation can spend the entire window in a
# single call, and at 24 rounds that is a turn nobody will wait out. 16,384
# tokens is roughly 60KB - about 1,500 lines of JavaScript - which is far more
# than any file worth writing in one go. Anything genuinely larger is what the
# truncation refusal teaches: first section with write_file, then append_file
# per section.
MAX_REPLY_TOKENS = 16384

# --- how much content ONE tool call may carry --------------------------------
#
# smallcode's ratio, and the whole mechanism behind it: an 8,192-token reply
# budget against an 8,000-char write cap, so the model is never permitted to
# attempt a write large enough to exhaust its own output budget. Four times the
# headroom. SHAMSU had one times - `MAX_REPLY_TOKENS` was the only limit, and a
# write allowed to fill the entire budget is a write that truncates.
#
# The ratio is restored by bounding the UNIT OF WORK, not the budget. Do not
# shrink `MAX_REPLY_TOKENS` to fix a truncation: a large reply budget is still
# genuinely useful for prose - a long explanation, a review, a plan - and once
# the content cap exists the budget stops being what binds a write.
#
# The trade this makes is deliberate (2026-08-20): more tool calls is correct,
# truncation is not. A truncated write is not a slow path, it is a pure-waste
# path - every token after the cut is refused AND no longer held by the model,
# so a 500-line file that truncates burns the full budget and produces nothing.
# The same file in six chunks burns fewer output tokens and all of them land.
WRITE_CHARS_FLOOR = 2_000

# Wall B, and the reason a budget-derived cap alone is not enough. llama.cpp's
# tool-argument JSON parser gives up somewhere around 13KB, and it does NOT
# report `done_reason: "length"` when it does - it returns a mangled or empty
# tool call, which is why the truncation guard sometimes never fires at all.
# smallcode caps at 8,000 chars specifically to sit 1.6x under it. Absolute:
# never scaled up, however much window is free.
WRITE_CHARS_CEILING = 8_000

# content of C chars ~= C/4 tokens (CHARS_PER_TOKEN_ESTIMATE), x ~1.10 for JSON
# escaping, so C/3.6 tokens on the wire; wanting content <= cap/4 gives
# C <= 0.9 x cap. `chars/4` is tuned for prose and dense code runs 3.3-3.7, so
# the estimate runs the wrong way here - 0.85 absorbs that.
WRITE_CHARS_PER_REPLY_TOKEN = 0.85

# Every argument that carries a payload rather than a reference. `patch_file`
# replacing ten lines with eight hundred has the identical problem, so the SIZE
# cap is not a `write_file` special case.
CONTENT_ARGUMENTS = ("content", "new_string")

# The much narrower set whose payload is a WHOLE FILE, and the only set the
# truncation gate may judge.
#
# Live 2026-08-20 this distinction cost a user their session. The gate ran on
# `patch_file.new_string` and refused it three times with "it ends inside a /*
# comment opened on line 23". A fragment is not a file: a patch replaces a
# region that may START inside one block and END inside another, and an
# `append_file` chunk is unfinished BY DESIGN - that is the entire point of
# chunked writing. Judging either by whole-file structure produces confident,
# repeatable false refusals, and the run then stopped blaming an output limit
# that had never fired.
#
# smallcode does not do this at all: `bin/executor.js` caps payload SIZE and
# checks nothing else. The gate is ours, it closes a real hole (§1.1 - a
# brand-new file had no structural check at write time), and it earns its place
# only where the payload really is the whole file.
WHOLE_FILE_ARGUMENTS = frozenset({"write_file", "create_and_run"})


def max_write_chars(reply_cap_tokens: int) -> int:
    """The most content one tool call may carry, given this reply budget.

    The minimum of two INDEPENDENT walls. Wall A - the reply budget - is soft,
    dynamic, and already understood here. Wall B - llama.cpp's tool-argument
    parser - is hard and fixed, and a cap derived only from Wall A would still
    allow a 60KB write on a large window and walk straight into it.
    """
    return int(
        max(
            WRITE_CHARS_FLOOR,
            min(WRITE_CHARS_PER_REPLY_TOKEN * max(reply_cap_tokens, 0), WRITE_CHARS_CEILING),
        )
    )


def write_budget_is_unworkable(reply_cap_tokens: int) -> bool:
    """Is the floor the thing that bound, rather than either wall?

    When even 2,000 chars - about fifty lines - exceeds what the budget can
    safely carry, the honest answer is not to let the model write in 1,700-char
    pieces. It is that the window is the wrong shape for this task. Silently
    degrading to useless chunk sizes is how a turn burns 24 rounds achieving
    nothing.
    """
    return WRITE_CHARS_PER_REPLY_TOKEN * max(reply_cap_tokens, 0) < WRITE_CHARS_FLOOR


# What the PROMPT says, as opposed to what the tool enforces. Prose guidance has
# to be memorable; the tool is what has to be exact. Sixty lines of dense code is
# ~2,500 chars, comfortably under every cap `max_write_chars` can return, so a
# model that follows the prompt never reaches the hard refusal. That gap is
# deliberate belt-and-braces - it is what smallcode does too (their prompt says
# 60 lines, their enforced cap is 8,000 chars, about 200).
WRITE_LINES_GUIDANCE = 60

# --- how much of a file the model is handed at once --------------------------
#
# Above this many lines, `read_file` with no range returns the file's OUTLINE -
# every class and function, its signature and its exact line range - instead of
# its body. The body is then fetched per symbol or per range, on demand.
#
# The read cap it replaces was a fixed 24,000-character head clip
# (`agent_tools.MAX_READ_CHARS`), and head-clipping is what starts the dead end
# in SMALLCODE_GAP_ANALYSIS.md §2: the model patches from what it saw,
# `old_string` is not found because the part it needed was in the half it never
# saw, the fuzzy retry misses too, and the whole-file rewrite is refused for
# being a partial read. There is no fifth move.
#
# 200 lines is smallcode's threshold (`bin/executor.js:132`) and it is a sound
# one - below it a file is worth reading whole, and the outline would cost a
# round to save nothing.
OUTLINE_OVER_LINES = 200

# The same rule applied to ONE symbol. A class is a symbol, and a class can be
# most of a file: live 2026-08-20 on qwen2.5-coder:3b, the model did exactly what
# it was told - read the outline, then `read_symbol` the class it needed - and
# got 313 lines back, because `export class Player` spans lines 34-347. The
# outline had just saved the window and the very next call spent it.
#
# So a container symbol answers the way the file does: its shape, and the ranges
# to fetch the parts. Lower than the file threshold because a symbol this long is
# already a container rather than a unit of work.
OUTLINE_SYMBOL_OVER_LINES = 80

# How many lines of each END of a file nothing can outline. smallcode's read
# guard keeps both ends; SHAMSU's fallback kept only the head, so the model was
# shown a changelog's oldest entries and never its newest - and then asked to
# add one at the bottom, against text it had not seen.
HEAD_TAIL_LINES = 60

# The gutter `read_file` puts in front of every line, smallcode's shape
# (`bin/executor.js:110`). Line numbers are what make the follow-up call
# possible: "patch line 412" and "read_file(start_line=400)" are both guesses
# unless the model was shown which line is which, and an outline that names
# ranges is worthless if the body it points at is unnumbered.
LINE_NUMBER_GUTTER = "|"

# Matches a numbered line the model copied straight back out of a read. See
# `_strip_line_numbers`.
_NUMBERED_LINE = re.compile(r"^\s*\d+\s*\|\s?", re.MULTILINE)

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
    # Renaming is not a write plus a delete, and simple mode had neither half.
    # `rename_file_via_move_tool` is an eval case NAMED after this tool and it
    # sat at 1/3, labelled model variance in BENCHMARK.md - the only route to a
    # pass was guessing `mv` against `move` against `ren` through
    # `run_command`. Of the 36 registry tools simple mode never offered, this is
    # the one with a failing measurement already pointing at it.
    "move_file": "move_file",
    # Deleting is the other half of editing a project, and simple mode had
    # neither half. It ships WITH `ask_user` on purpose: `delete_file`'s own
    # description tells the model to ask rather than guess between candidate
    # targets, and until now it was pointing at a tool simple mode did not
    # offer.
    "delete_file": "delete_file",
    # The prompt has always told the model to ask when a decision is the
    # user's. The tool that does it was built, tested and reachable only from
    # the legacy loop.
    "ask_user": "ask_user",
    # Read-only git, so the model can see what it has actually done rather than
    # what it believes it did. `_with_diff` shows one edit; these show the turn,
    # the branch and the history. Withheld outside a repository - see
    # CONDITIONAL_TOOL_FAMILIES.
    "web_search": "web_search",
    "fetch_url": "fetch_url",
    "git_status": "git_status",
    "git_diff": "git_diff",
    "git_log": "git_log",
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
    # The other half of outlining a large file. An outline is only useful if
    # the thing it points at can be fetched, and a line range the model derived
    # from a listing is a guess - this one is computed from the same parse.
    "read_symbol": "read_symbol",
    # The model was already told to check its work and then left to guess the
    # command. Detection lives in `simple_tests`; the run goes through
    # `run_command`, so approval and the risk classifier still apply.
    "run_tests": "run_tests",
    # smallcode's `use_skill`, over the skill loader SHAMSU already had and
    # never showed the model. The INDEX goes in the prompt (a name and one
    # line); the body is fetched only when the model asks, so a dozen skills
    # cost a dozen lines of window instead of a dozen documents.
    "use_skill": "use_skill",
    # Symbol-aware editing: "replace Game.render" instead of matching a snippet
    # by hand. The range comes from the same parse `read_symbol` uses, so the
    # thing being replaced is exactly the thing the model was shown.
    "replace_symbol": "replace_symbol",
    # Definition of Done. The point is to move a claim of completion out of
    # PROSE and into STATE: "done" stops being a sentence to believe and becomes
    # a set of assertions that are resolved or are not.
    "contract_create": "contract_create",
    "contract_status": "contract_status",
    "contract_assert_pass": "contract_assert_pass",
    "contract_assert_fail": "contract_assert_fail",
    "contract_assert_skip": "contract_assert_skip",
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
                "Create a NEW file, or replace a small one completely. "
                "LIMIT: 60 lines / 8KB per call - for anything longer, write the first "
                "60 lines here and append_file the rest. To change part of an existing "
                "file, prefer patch_file: it is far faster and cannot lose the parts "
                "you did not mean to touch."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path relative to the workspace."},
                    "content": {
                        "type": "string",
                        "description": "The file content. 60 lines / 8KB maximum.",
                    },
                },
                "required": ["filepath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": (
                "Rename or move a file inside the workspace. Use this instead of "
                "writing a copy under the new name and leaving the old one behind, "
                "and instead of shell mv/move/ren. Backed up, so it can be undone. "
                "Refuses if the destination already exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Existing path, relative to the workspace.",
                    },
                    "destination": {
                        "type": "string",
                        "description": "New path, relative to the workspace.",
                    },
                },
                "required": ["source", "destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for CURRENT or EXTERNAL information this workspace "
                "cannot answer: a library's API, a third-party error message, a "
                "version, published documentation. Never for this project's own "
                "code - use search_files or graph_search for that. Needs approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "What to search for."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": (
                "Fetch one web page's readable text - typically a documentation page "
                "found with web_search. Needs approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "Absolute http(s) URL."}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": (
                "Show short git status for the workspace - which files you have "
                "changed, staged or left untracked."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": (
                "Show the unstaged git diff: exactly what your edits changed, across "
                "every file, rather than one edit at a time."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Show recent git commits.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "string",
                        "description": "How many commits. Default 10, max 100.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": (
                "Delete a workspace file. Backed up first, so it can be undone. "
                "Only delete when the task clearly calls for it; if several files "
                "could be the intended target, call ask_user instead of guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path relative to the workspace.",
                    },
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user a question when the answer is THEIRS to give: choosing "
                "between valid approaches, naming, scope, anything destructive or hard "
                "to undo, or an ambiguous target where several files match. Look up "
                "plain facts with find_files/search_files/read_file yourself instead of "
                "asking. Calling this ENDS your turn and waits for their answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask."},
                    "options": {
                        "type": "array",
                        "description": "Optional choices, each {label, description}.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contract_create",
            "description": (
                "Write down what DONE means for this task, as a list of checkable "
                "assertions. Use it at the start of a job with more than one part - you "
                "cannot report the task finished while an assertion is unchecked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short name for the task."},
                    "brief": {"type": "string", "description": "What the task is. Optional."},
                    "assertions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "One checkable claim each, e.g. 'npm test exits 0' or "
                            "'/login returns 200'."
                        ),
                    },
                },
                "required": ["title", "assertions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contract_status",
            "description": (
                "Show the current contract and which assertions are still unchecked. "
                "Call this before you say the task is finished."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contract_assert_pass",
            "description": (
                "Record that an assertion holds, with the evidence that shows it - what "
                "you ran and what it said."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "assertion_id": {"type": "string", "description": "Assertion id, e.g. a01."},
                    "evidence": {
                        "type": "string",
                        "description": "What you ran and what it returned.",
                    },
                },
                "required": ["assertion_id", "evidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contract_assert_fail",
            "description": (
                "Record that an assertion does NOT hold, with what actually happened. "
                "Use this when you checked and the answer was bad - not to skip a check."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "assertion_id": {"type": "string", "description": "Assertion id, e.g. a01."},
                    "evidence": {"type": "string", "description": "What went wrong."},
                },
                "required": ["assertion_id", "evidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "contract_assert_skip",
            "description": "Record that an assertion is out of scope, and why.",
            "parameters": {
                "type": "object",
                "properties": {
                    "assertion_id": {"type": "string", "description": "Assertion id, e.g. a01."},
                    "reason": {"type": "string", "description": "Why it does not apply."},
                },
                "required": ["assertion_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_symbol",
            "description": (
                "Replace ONE whole function or class with new source, by name. Use this "
                "instead of patch_file when you are rewriting a whole function - you do "
                "not have to match the old text, only name it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path relative to the workspace."},
                    "symbol": {
                        "type": "string",
                        "description": "Function or class name, e.g. render or Game.render.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "The complete new source for it, including its signature line. "
                            "60 lines / 8KB maximum."
                        ),
                    },
                },
                "required": ["filepath", "symbol", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_symbol",
            "description": (
                "Read ONE function or class from a file, by name - the exact source, "
                "nothing else. Use this after read_file shows you an outline, rather "
                "than reading the whole file to see one function."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path relative to the workspace."},
                    "symbol": {
                        "type": "string",
                        "description": "Function or class name, e.g. render or Game.render.",
                    },
                },
                "required": ["filepath", "symbol"],
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
                    "new_string": {
                        "type": "string",
                        "description": "The text to put in its place. 60 lines / 8KB maximum.",
                    },
                },
                "required": ["filepath", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "use_skill",
            "description": (
                "Load the full instructions for a named skill - a short worked "
                "procedure for a kind of task. Use it when the skill index lists one "
                "that fits what you are doing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Skill name from the index."}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run this project's tests. Finds the right command itself - npm test, "
                "pytest, cargo test - so you do not have to guess it. Use this to check "
                "your work after a change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_filter": {
                        "type": "string",
                        "description": "Optional: run only tests matching this name.",
                    }
                },
                "required": [],
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
                "file: write_file the first section, then append_file each one after. "
                "LIMIT: 60 lines / 8KB per call. Far safer than rewriting a whole file, "
                "and it cannot be cut off partway."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path relative to the workspace."},
                    "content": {
                        "type": "string",
                        "description": "The text to add at the end. 60 lines / 8KB maximum.",
                    },
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
                    "new_string": {
                        "type": "string",
                        "description": "The text to put in its place. 60 lines / 8KB maximum.",
                    },
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
                    "content": {
                        "type": "string",
                        "description": "The file content. 60 lines / 8KB maximum.",
                    },
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

# Tool families that can only answer once something exists to answer FROM, and
# what each one needs. Measured 2026-08-19: the full roster costs a flat 2,111
# tokens on every single call - 85% of a fresh 3B prompt, and never under 30%
# however long the session runs. Across seven live sessions the model called 7
# of 19 tools; the 12 it never touched cost 1,131 of those tokens.
#
# These three families are the ones that can be answered honestly by asking the
# filesystem, and the check is a file stat. A graph tool with no index, a
# history search with no prior session, and a memory reader with no notes can
# only return "nothing here" - so sending their schemas buys nothing and costs
# 516 tokens on every call of a fresh workspace.
#
# `memory_remember` is deliberately NOT in here. It is the tool that CREATES the
# notes the readers need, and gating it behind notes existing would mean a
# workspace could never get its first one - which is M1, and the point is not to
# make it permanent.
CONDITIONAL_TOOL_FAMILIES: dict[str, tuple[str, ...]] = {
    "memory": ("memory_load", "memory_list", "memory_forget"),
    "graph": ("graph_search", "explain_symbol"),
    "history": ("history_search",),
    # Read-only git. Gated on the workspace actually BEING a repo, which is the
    # same principle as every family above: a `git_status` offered outside a
    # repository is a tool with nothing to answer from, and the model spends a
    # round finding that out. The mutating half of the git suite stays out -
    # `run_command` reaches it with approval and the risk classifier, and 19
    # more schemas is not a trade worth making for that.
    "git": ("git_status", "git_diff", "git_log"),
    # Offered only when a search backend is actually answering AND the user has
    # opted in - see `_web_is_reachable`. Both halves matter: reaching the
    # network is a decision a local-first tool must be asked to make, and a tool
    # pointed at nothing is a round spent learning that.
    "web": ("web_search", "fetch_url"),
}


def available_tool_families(workspace: Path) -> frozenset[str]:
    """Which conditional families have something to answer from, right now.

    Three file stats. Deliberately generous on failure: anything that cannot be
    determined counts as AVAILABLE, because withholding a tool the model needed
    is a worse error than paying for a schema it did not.
    """
    available: set[str] = set()
    for family, probe in (
        ("memory", _has_memory_notes),
        ("graph", _has_code_graph),
        ("history", _has_earlier_sessions),
        ("git", _is_a_git_repo),
        ("web", _web_is_reachable),
    ):
        try:
            if probe(workspace):
                available.add(family)
        except Exception:  # noqa: BLE001 - an unreadable probe must not remove a tool
            available.add(family)
    return frozenset(available)


def _web_is_reachable(_workspace: Path) -> bool:
    """Is there a search backend actually answering right now?

    The probe the web tools were waiting on. They were built, tested and never
    offered, because offering a tool that may always fail contradicts the rule
    this codebase committed under the title *"Offer only the tools that have
    something to answer from"* - and `web_search` depends on a SearXNG instance
    that may or may not be up. Reporting that as a permanent gap was the wrong
    call: the missing piece was a probe, and every other conditional family
    already has one.

    A one-second HEAD against the configured URL, and DISABLED by default -
    `SHAMSU_WEB_ENABLED` gates it, because reaching the network is a decision a
    local-first tool should not make on the user's behalf without being asked.
    Unreachable is a normal answer here, not an error, so it is quiet.
    """
    import os as _os

    if _os.environ.get("SHAMSU_WEB_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    try:
        import httpx

        from shamsu.tools.web import DEFAULT_SEARXNG_URL

        url = _os.environ.get("SHAMSU_SEARXNG_URL", DEFAULT_SEARXNG_URL).rstrip("/")
        # Any HTTP answer means something is listening; the tool itself handles
        # a backend that is up but unhappy. What is being excluded here is the
        # case where nothing is there at all.
        httpx.head(url, timeout=1.0, follow_redirects=True)
        return True
    except Exception:  # noqa: BLE001 - unreachable is the expected answer
        return False


def _is_a_git_repo(workspace: Path) -> bool:
    """Is there a repository here for `git status` to describe?

    A worktree keeps a `.git` FILE rather than a directory, so both count.
    """
    return (workspace / ".git").exists()


def _has_memory_notes(workspace: Path) -> bool:
    from shamsu import paths

    notes = paths.memory_notes_dir(workspace)
    return notes.is_dir() and any(notes.glob("*.md"))


def _has_code_graph(workspace: Path) -> bool:
    # The same file `AbstractService.ensure_ready()` gates on, so this agrees
    # with whether the graph would actually answer.
    return (workspace / ".shamsu" / "abstract" / "last-index.json").is_file()


def _has_earlier_sessions(workspace: Path) -> bool:
    from shamsu import paths

    sessions = paths.sessions_dir(workspace)
    if not sessions.is_dir():
        return False
    # More than the one being written right now: with a single session there is
    # no history to search that is not already in the window.
    return sum(1 for entry in sessions.iterdir() if entry.is_dir()) > 1


def _without_unavailable_families(
    schemas: list[dict[str, Any]], available: frozenset[str]
) -> list[dict[str, Any]]:
    withheld = {
        name
        for family, names in CONDITIONAL_TOOL_FAMILIES.items()
        if family not in available
        for name in names
    }
    if not withheld:
        return schemas
    return [s for s in schemas if s.get("function", {}).get("name") not in withheld]


def active_tool_schemas(
    context_window: int = 0,
    category: str = "",
    available: frozenset[str] | None = None,
    request: str = "",
) -> list[dict[str, Any]]:
    """The tools to send on this call.

    Everything that can currently do something, unless the window is tight
    enough that two-stage routing earns its extra round: then the category
    selector alone, and once the model has chosen, that category tools.

    Above that threshold - which is where the 32k models this project targets
    live - the catalogue used to go out WHOLE: measured at 26 schemas and 3,196
    tokens on every turn, about a tenth of the window, growing with every tool
    added. `request` narrows that deterministically, with no extra round: see
    `agents/tool_classifier.py`. It is a guess, so it only ever narrows to a
    category and its companion, never to one, and an unsure guess sends
    everything.

    `available=None` means "do not filter" - the old behaviour, kept so every
    existing caller and test still gets the full roster.
    """
    from shamsu.agents.simple_router import (
        category_selector_tool,
        routing_mode,
        tools_for_category,
    )

    if not context_window or routing_mode(context_window) == "direct":
        # An explicit choice by the MODEL outranks the scorer's guess about the
        # user's words - that is the whole value of keeping `select_category`
        # in the roster. Without this branch the escape hatch is decorative:
        # the model asks for the write tools, the guess is re-applied, and it
        # gets the same read tools back.
        schemas = (
            tools_for_category(category, SIMPLE_TOOL_SCHEMAS)
            if category
            else _narrowed_by_request(SIMPLE_TOOL_SCHEMAS, request)
        )
    elif not category:
        return [category_selector_tool()]
    else:
        schemas = tools_for_category(category, SIMPLE_TOOL_SCHEMAS)
    if available is None:
        return schemas
    return _without_unavailable_families(schemas, available)


def _narrowed_by_request(
    schemas: list[dict[str, Any]], request: str
) -> list[dict[str, Any]]:
    """Drop the tool families this request plainly does not need.

    The escape hatch is the reason this is safe, and it is what smallcode's
    direct mode does not have. `select_category` stays in the roster whatever
    the scorer decided, so a misclassified turn costs ONE round trip to
    correct - the model asks for the write tools and gets them - rather than
    leaving it holding read tools for an edit it cannot make and no way to say
    so.
    """
    if not request:
        return schemas
    from shamsu.agents.simple_router import (
        ALWAYS_TOOLS,
        TOOL_CATEGORIES,
        category_selector_tool,
    )
    from shamsu.agents.tool_classifier import categories_for

    wanted = categories_for(request)
    if not wanted:
        return schemas
    keep: set[str] = set()
    for name in wanted:
        keep.update(TOOL_CATEGORIES.get(name, {}).get("tools", ()))
    if not keep:
        return schemas
    # The skill index is injected on every turn, so `use_skill` has to stay
    # callable however the request was scored - and `ask_user` is the answer to
    # an ambiguity, which by definition turns up in a category nobody predicted.
    keep.update({"use_skill", *ALWAYS_TOOLS})
    narrowed = [s for s in schemas if s.get("function", {}).get("name") in keep]
    if not narrowed:
        # Never hand back an empty roster over a scoring accident.
        return schemas
    # APPENDED, not filtered in: the selector is generated rather than being a
    # member of `SIMPLE_TOOL_SCHEMAS`, so keeping its name in `keep` would have
    # silently dropped the one tool that makes a wrong guess recoverable.
    return [*narrowed, category_selector_tool()]


MUTATING_TOOLS = frozenset({"write_file", "patch_file", "replace_symbol"})

# Calls that EXERCISE the code rather than describe it. The only kind of tool
# call that can back a contract assertion, because it is the only kind whose
# result the harness watched rather than the model narrated.
RUNNING_TOOLS = frozenset({"run_command", "run_tests", "create_and_run"})

# Every call that puts bytes on disk. Wider than MUTATING_TOOLS on purpose:
# that set drives the no-op and repeated-edit counters, which only make sense
# for the two tools that report a diff. THIS set answers a different question -
# would executing this call, cut off mid-argument, damage the workspace? - and
# `append_file` belongs in it. Live 2026-08-19, `Round 9 append_file -> ok` sits
# directly above a cut-off notice in the log.
WRITING_TOOLS = frozenset(
    {
        "write_file", "patch_file", "append_file", "read_and_patch",
        "create_and_run", "replace_symbol",
    }
)

# Consecutive writes refused for arriving truncated, before the turn stops.
# The refusal tells the model to send the file in pieces; if it will not, the
# window is the wrong shape for this file and spinning proves nothing. Every
# guard needs an exit, and this is that guard's.
MAX_TRUNCATED_WRITE_REFUSALS = 3

# How many times a turn may be sent back for claiming done with the contract
# unresolved. Every guard here needs an exit; this is that guard's.
MAX_CONTRACT_NUDGES = 2

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
    # Claude/OpenAI-shaped names, smallcode's `normalizeToolCall`. A model
    # trained on those transcripts reaches for `Edit` or `Bash` by reflex, and
    # they are far enough from a SHAMSU name that fuzzy matching finds nothing
    # - live, `Edit` fell all the way through to a re-listing of the roster.
    # Accepting them costs one dict entry and saves a whole round each time.
    "read": "read_file",
    "edit": "patch_file",
    "str_replace": "patch_file",
    "str_replace_editor": "patch_file",
    "write": "write_file",
    "create": "write_file",
    "bash": "run_command",
    "shell": "run_command",
    "terminal": "run_command",
    "glob": "find_files",
    "grep": "search_files",
    "ls": "list_files",
    "todowrite": "contract_create",
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

# The contract tools take `assertion_id`, and across four live runs on
# qwen3.5:9b the model called them 23 times without ever using that name.
# Seventeen of those carried the RIGHT id under a different key:
#
#     7x  assertion_index='a01'      6x  contract_id=...      2x  claim=...
#     1x  claim_id=...               1x  assertion='a01.1'
#
# Every one was refused with "No such assertion", and those refusals were the
# single largest waste in the roster - more rounds than every other failure
# combined, 7 of 24 in one run. This is the `search_files` defect again, where
# the schema said `pattern` and the implementation read `query`: a name the
# model reaches for and the code does not answer to.
_ASSERTION_ID_ALIASES = {
    name: "assertion_id"
    for name in (
        "assertion_id", "assertion_index", "assertion", "assertion_number",
        "contract_id", "claim", "claim_id", "id", "index", "number", "item",
    )
}
for _contract_tool_name in (
    "contract_assert_pass", "contract_assert_fail", "contract_assert_skip",
):
    _TOOL_ARG_ALIASES[_contract_tool_name] = dict(_ASSERTION_ID_ALIASES)


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
        # A caller that has already decided answers first. `approval_override`
        # is how the headless runner, the eval harness and `shamsu run` say
        # "allow" or "deny" without a terminal, and simple mode never consulted
        # it - so every one of them fell through to a console prompt with no TTY
        # behind it and got a refusal.
        #
        # The visible cost: `run_command_verify` went 3/3 to 0/3 and stayed
        # there, and the model's own answer said why - "if the command were
        # allowed to execute". A tool that is silently denied looks exactly like
        # a tool that does not work.
        injected = get_approval_override()
        if injected is not None:
            return bool(injected(request))
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


# One set of counters per CONVERSATION, not per process.
#
# This was a single module-level object, on the reasoning that a fresh
# SimpleChatLoop is built per user message while the REPL lives for the whole
# session. That is an argument against per-LOOP; it is not an argument for
# per-PROCESS, and per-process is what it meant in practice. Live 2026-08-21:
# `/sessions resume` into a second conversation and the meter kept reporting
# the first one's numbers as if they belonged to the new thread. Silently -
# the object's own docstring says "per session" and it was not.
#
# Keyed, so contaminating one session with another's numbers is now structurally
# impossible rather than something the REPL has to remember not to do.
_COUNTERS_BY_SESSION: dict[str, ContextCounters] = {}

#: Which conversation the bare readers (`/context meter`, the web status
#: endpoint) are asking about. Set by the loop when a turn starts, and by the
#: REPL when the user switches threads - the switch is the moment the old
#: numbers stop being an answer to the question being asked.
_ACTIVE_SESSION = ""


def counters_for(session_id: str) -> ContextCounters:
    """The counters for one conversation, created on first use."""
    key = str(session_id or "")
    counters = _COUNTERS_BY_SESSION.get(key)
    if counters is None:
        counters = ContextCounters()
        _COUNTERS_BY_SESSION[key] = counters
    return counters


def set_active_session(session_id: str) -> None:
    """Point the bare readers at *session_id*."""
    global _ACTIVE_SESSION
    _ACTIVE_SESSION = str(session_id or "")


def active_session_id() -> str:
    """Which conversation the bare readers are reporting on."""
    return _ACTIVE_SESSION


def active_counters() -> ContextCounters:
    """The counters for whichever conversation is in front of the user."""
    return counters_for(_ACTIVE_SESSION)


class _ActiveCounters:
    """`SESSION_COUNTERS.x` reads the ACTIVE session's counters.

    A proxy rather than a rename because three surfaces read this name and none
    of them have a session id in scope at the point of reading. The proxy keeps
    those call sites honest without each having to learn how to find the
    conversation they are already looking at.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(active_counters(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(active_counters(), name, value)


SESSION_COUNTERS = _ActiveCounters()

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
    #: The answer was cut off - the reply cap or the window stopped it
    #: mid-sentence. The loop already SAYS so in the text ("This answer was cut
    #: off."); without this the surfaces above it had no way to know, so a turn
    #: whose own answer admitted it was incomplete was still badged SUCCESS.
    truncated: bool = False


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
    # The model asked the user something. `ask_user` does not block - it hands
    # back a structured question and expects the LOOP to end the turn on it,
    # which is the half simple mode never had, so the tool sat in the registry
    # unreachable while the prompt told the model to ask when a decision was
    # the user's to make.
    asked: str = ""


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
        feedback: Any | None = None,
        emit: Any | None = None,
        source: str = "cli",
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
        # The ONE seam every surface reads. `on_activity`/`on_status` stay as
        # thin shims over it so nothing that builds this loop today breaks, but
        # they cannot carry a turn on their own: a callback that takes a string
        # cannot say whether the string was a tool call or a heartbeat, and a
        # renderer that cannot tell those apart is the reason Telegram threw
        # most of a turn away.
        self.emit = emit
        # Which surface started this turn: "cli" | "telegram" | "web". It rides
        # on every event so a mirror can label a remote turn as remote.
        self.source = source or "cli"
        self.turn_id = ""
        self._event_seq = 0
        self.verify_changes = verify_changes
        self.temperature = temperature
        self.request_timeout = request_timeout
        self.state = state or ChatState(
            simple_system_prompt(
                self.workspace, has_history=_thread_has_history(session_logger)
            ),
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
            # Stamped onto every message this turn writes, so the transcript
            # says which surface asked - the same value the turn stream already
            # carries on its events.
            source=self.source,
        )
        # Which round the turn is on, for the live meter. Read by `_status`.
        self._round_index = 0
        # Counters follow the conversation, not the process.
        set_active_session(str(getattr(session_logger, "session_id", "") or ""))
        self._watch_approvals()
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
        # Which conditional tool families have anything to answer from. Computed
        # once per turn in `run()`, not per round: three file stats are cheap,
        # but a roster that changes mid-turn would invalidate the KV prefix on
        # every call for no gain.
        self._available_families: frozenset[str] = frozenset(
            CONDITIONAL_TOOL_FAMILIES
        )
        self._calls_since_elide = 0
        # Said once per loop, not once per round.
        self._warned_filling = False
        # CONSECUTIVE rounds spent recovering rather than progressing: an empty
        # reply, a nudge, a no-op edit. Drives `_should_disable_thinking`, and
        # reset by any write that lands - see `_run_tools`. A streak, not a
        # tally: smallcode's rule is `isRepair && attempt > 1`, meaning the
        # model already overthought THIS solution, and a turn-wide counter that
        # only ever went up turned that into "the model made two mistakes at
        # any point, so it may not reason for the rest of the turn."
        self._repair_streak = 0
        # How many sweeps and how many messages they shrank, for `/status`.
        self.evictions = 0
        self._rewrite_refused: set[str] = set()
        # Files the model has seen only part of - writing them whole loses data.
        self._partial_reads: set[str] = set()
        # Line ranges seen per file, so reading a big file IN PIECES still
        # adds up to having seen it.
        self._seen_ranges: dict[str, list[tuple[int, int]]] = {}
        # Ranges actually SENT to the model, per file - kept apart from
        # `_seen_ranges` above, which answers a different question and clears
        # itself once a file has been covered in pieces. This one only ever
        # grows within a turn, and is emptied by an elision sweep, because a
        # payload that has been elided is one the model no longer has.
        self._ranges_sent: dict[str, list[tuple[int, int]]] = {}
        # path -> how many reads of it have been answered from cache, and the
        # paths `read_file` has been withdrawn for as a result.
        self._blocked_reads: dict[str, int] = {}
        self._read_withdrawn: set[str] = set()
        # Announced once, when it first happens. A tool that vanishes without a
        # word is a model wondering what it did wrong; the same message every
        # round is the nudge spiral this whole fix exists to end.
        self._announced_withdrawal: set[str] = set()
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
        # Tool failures this turn, for the evidence note written at turn end.
        self._turn_failures: list[tuple[str, str]] = []
        #: What the HARNESS watched happen, as opposed to what the model says
        #: happened. Deliberately per-SESSION, not per-turn: a contract spans
        #: turns, and a check run in the turn that wrote the code still backs
        #: the assertion recorded in the turn after it.
        self._observed_runs: list[str] = []
        self._observed_writes: list[str] = []
        self._truncated_refusals = 0
        self._truncated_target = ""
        # Files whose last verdict was "still being built" - open blocks and
        # nothing else wrong. Reported as progress while the model is still
        # writing, and settled at turn end, when "not finished yet" stops being
        # a true description of a file the model has walked away from.
        self._unfinished: dict[str, str] = {}
        # Did the write that LAST landed on this file add to it?
        #
        # The whole of the "still being built" exemption now turns on this, and
        # it had to. A file with open blocks means two opposite things: a
        # section of a file still being written (progress), or a file a patch
        # just broke (a fault). Nothing distinguished them, so a patch that ate
        # a closing brace was reported to the model as `ok: true`, "that is
        # expected part-way through - continue with append_file" - and the
        # advice was wrong twice over, because appending to the END cannot
        # close a brace missing in the MIDDLE.
        #
        # A write that GREW the file is the chunked path the prompt asks for,
        # whichever tool carried it. A patch that shrank it is not.
        self._last_write_grew: dict[str, bool] = {}
        # Every file this turn has appended to or created, ever - unlike the
        # above, never turned off by a later patch. Only used to add context to
        # a real failure: "you have been building this file this turn" is worth
        # saying next to a syntax error, and is not grounds for suppressing it.
        self._built_up: set[str] = set()
        # Did the streak come from a REFUSED payload rather than from Ollama
        # cutting the generation off? The two need different endings - one is
        # "the file is too big for one reply", the other is "what you sent would
        # not have parsed" - and telling a user the first when it was the second
        # sends them to a limit that never fired.
        self._refused_unparseable = False
        # Has the model already been told to stop patching and edit by symbol?
        # Once per turn: a strategy change offered twice is not a strategy, and
        # the second one would be the loop insisting rather than helping.
        self._strategy_switched = False
        # The file the last unproductive mutation was aimed at, so the change of
        # strategy can NAME it. "Try a different approach" is the advice that
        # already failed; "read_symbol then replace_symbol on game.js" is a call.
        self._last_failed_path = ""
        # One line about what this project IS. Computed once per loop.
        self._project = ""
        # The detectors simple mode did not have. See `agents/loop_guards.py`
        # for why they live there and the older eight still live inline.
        self._read_loop = ReadLoopDetector()
        self._trust = TrustDecay(protected=WRITING_TOOLS | {"run_command", "ask_user"})
        # Content hash of the last WHOLE read per file, so a re-read of an
        # unchanged file can say so instead of resending it. Dropped on every
        # elision sweep - see `_note_unchanged_since_last_read`.
        self._read_digests: dict[str, str] = {}
        # Successful edits per file this turn - repeated blind fixes.
        self._edits_per_file: dict[str, int] = {}
        # How many times each identical read-only call has been made this turn.
        self._read_signatures: dict[str, int] = {}
        # Readable per-turn transcript of prompt + raw response.
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
        """One turn, and a note about what it did.

        The note is the point of the wrapper: `memory_remember` is the only
        caller of `remember()` in simple mode, so memory exists ONLY when the
        model volunteers a tool call - and it does not. A real 2-turn run
        produced no notes at all, and across seven live sessions on 2026-08-19
        `memory_load`, `memory_list` and `memory_forget` were called zero times
        between them.

        smallcode has two writers and SHAMSU had one. `src/memory/evidence.js`
        says why: evidence is "auto-derived from the trace recorder at task
        end", distinct from the manual notes a model chooses to write. The
        models that most need a scratchpad are exactly the ones least likely to
        spend a tool call on it.
        """
        started = time.perf_counter()
        self._turn_failures = []
        # A fresh turn id and a fresh sequence per turn: renderers dedupe and
        # reorder on `seq`, and a counter that carried across turns would make
        # "everything after N" mean different things on different surfaces.
        self.turn_id = f"turn-{uuid.uuid4().hex[:12]}"
        self._event_seq = 0
        self._publish("turn.start", user_input, prompt=user_input)
        try:
            result = await self._run_turn(user_input)
        except BaseException as exc:  # noqa: BLE001 - re-raised; the stream must not lie
            self._publish("error", f"{type(exc).__name__}: {exc}")
            self._publish(
                "turn.end",
                _turn_verdict(time.perf_counter() - started, (), stopped=True),
                status="error",
                elapsed=time.perf_counter() - started,
            )
            raise
        elapsed = time.perf_counter() - started
        if result.error:
            self._publish("error", result.error)
        self._publish("assistant", result.final)
        # "done" used to mean "the loop returned without raising", which is a
        # claim about the PROCESS, and every surface above was reading it as a
        # claim about the OUTCOME. Live 2026-08-22: a turn that changed no file,
        # failed four tool calls and printed "This answer was cut off." was
        # badged `✓ SUCCESS  done in 21m52s`. A turn whose own answer says it
        # is incomplete must not be reported as finished.
        if result.stopped:
            status = "stopped"
        elif result.truncated:
            status = "incomplete"
        else:
            status = "done"
        failures = len(dict.fromkeys(self._turn_failures))
        self._publish(
            "turn.end",
            _turn_verdict(
                elapsed,
                result.changed_files,
                stopped=result.stopped,
                failures=failures,
                truncated=result.truncated,
            ),
            status=status,
            error=result.error,
            elapsed=elapsed,
            rounds=result.rounds,
            tool_calls=result.tool_calls,
            failures=failures,
            truncated=result.truncated,
            changed_files=list(result.changed_files),
        )
        try:
            await asyncio.to_thread(
                self._record_evidence, user_input, result, time.perf_counter() - started
            )
        except Exception:  # noqa: BLE001 - a note must never fail a turn
            pass
        return result

    async def _run_turn(self, user_input: str) -> SimpleChatResult:
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
        # Once per turn, before anything is budgeted: the roster is part of
        # the prompt, and `_fixed_overhead` has to charge for what will
        # really be sent. Not per round - three file stats are cheap, but a
        # roster that changed mid-turn would invalidate the KV prefix on
        # every call for no gain.
        self._available_families = await asyncio.to_thread(
            available_tool_families, self.workspace
        )
        # Once per turn, like the roster: a project does not change its language
        # or its test runner between rounds, and `_fixed_overhead` has to charge
        # for what will really be sent.
        if not self._project:
            self._project = await asyncio.to_thread(project_brief, self.workspace)
        # Ask for a plan ONCE, and only when there is not already one standing.
        # `contract_create` has been offered all along and a model that does not
        # think to call it never does; this is the ask. Skipped when a contract
        # already exists, because the anchor is already showing it and asking
        # again would start a second plan for the same job.
        if not self._standing_plan():
            wanted = ask_for_a_plan(user_input)
            if wanted:
                self.state.append_user(wanted, origin=ORIGIN_LOOP)
                self._activity("this has several parts; asked it to write them down first")
        # Once per user message, not per round: the graph lookup costs ~2s and
        # what a file exports does not change between rounds of the same turn.
        self._brief = await asyncio.to_thread(codebase_brief, self.workspace, user_input)
        await self._compact_if_needed()
        changed: list[str] = []
        tool_calls = 0
        prose_nudges = 0
        # Bounded like every other nudge here. A guard the model cannot get past
        # is a deadlock waiting for a user to notice, and two rounds is enough
        # to either check the assertions or say what is blocking them.
        contract_nudges = 0
        empty_nudges = 0
        promise_nudges = 0
        for round_index in range(self.max_rounds):
            self._round_index = round_index
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
                        self._repair_streak += 1
                        # Keep the transcript alternating: an assistant turn,
                        # then the nudge. Stacking user messages is what broke
                        # it. And nudge BEFORE salvaging - a reasoning model's
                        # first empty turn usually means it is about to call a
                        # tool, so ending the turn here does no work at all.
                        # (Salvaging first cost the probe turns 8-10: 0 tools,
                        # main.py never written.)
                        self.state.append_assistant("")
                        self.state.append_user(
                            "That reply was empty. Answer the question, or call one tool.",
                            origin=ORIGIN_LOOP,
                        )
                        continue
                    if turn.thinking and not self._hit_the_length_limit():
                        # A COMPLETE thought with no visible content. Reasoning
                        # models really do end turns this way, and re-asking
                        # just burns another 30s - so it is used as the answer.
                        self._activity("model only reasoned; using its thinking as the answer")
                        self.state.append_assistant(turn.thinking)
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
                            truncated=True,
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
                    self._repair_streak += 1
                    self.state.append_assistant(text)
                    self.state.append_user(
                        f"You showed the new contents of {described} but did not change the file. "
                        f"Apply it now: replace_symbol if that is a whole function or class, "
                        f"patch_file for one exact replacement inside {described}, or "
                        "append_file only if it belongs at the END of the file. Do not "
                        "repeat the code in prose.",
                        origin=ORIGIN_LOOP,
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
                    self._repair_streak += 1
                    self.state.append_assistant(text)
                    self.state.append_user(
                        f"Your reply ended on {promised!r} and then stopped. Nothing "
                        "followed it and you called no tool, so nothing happened.\n\n"
                        "Do it now, in this turn: call the tool that carries out what you "
                        "just said you would do. Do not say you are about to do it again.",
                        origin=ORIGIN_LOOP,
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
                leaked = leaked_tool_call(text)
                if leaked and empty_nudges < MAX_EMPTY_NUDGES:
                    # The whole reply was a tool-call object for a name the
                    # parser did not recognise, so it was never salvaged and
                    # was about to be handed over as the answer. Raw JSON is
                    # not an answer; say which names exist and let it retry.
                    empty_nudges += 1
                    self._repair_streak += 1
                    close = closest_tool_names(leaked, sorted(SIMPLE_TOOLS))
                    suggestion = (
                        f" Did you mean {' or '.join(close)}?" if close
                        else " Pick one from the tools you were given."
                    )
                    self.state.append_assistant(text)
                    self.state.append_user(
                        f"That reply was a tool call for {leaked!r}, which is not a "
                        f"tool.{suggestion} Make the call properly this time - as a "
                        "tool call, not as text in your reply.",
                        origin=ORIGIN_LOOP,
                    )
                    self._activity(f"replied with a bare {leaked} call; asked it to call a real tool")
                    continue
                lost = greeting_regression(text, work_happened=tool_calls > 0)
                if lost and empty_nudges < MAX_EMPTY_NUDGES:
                    # Counted against the empty-turn budget rather than getting
                    # its own: both are "that reply was not an answer", and a
                    # model producing greetings after tool calls has lost the
                    # thread in a way one nudge either recovers or does not.
                    empty_nudges += 1
                    self._repair_streak += 1
                    self.state.append_assistant(text)
                    self.state.append_user(lost.correction, origin=ORIGIN_LOOP)
                    self._activity(lost.activity)
                    continue
                blocked = self._contract_blocks_this_claim(text, contract_nudges)
                if blocked:
                    # Not a block and not a rewrite: the sentence stands in the
                    # history and the model is handed the list of things it has
                    # not checked. A standing "do not claim complete" has been in
                    # this project's prompts four times over and never worked;
                    # this one arrives at the moment of the claim and names the
                    # exact next call.
                    contract_nudges += 1
                    self._repair_streak += 1
                    self.state.append_assistant(text)
                    self.state.append_user(blocked)
                    self._activity("claimed done with the contract unresolved; asked it to check")
                    continue
                cut_off = self._hit_the_length_limit()
                if cut_off:
                    # The model was still speaking when the window ran out.
                    # Keep what it managed to say, but never present it as a
                    # finished answer - `done_reason` told us it was not.
                    text = self._out_of_room_message(text)
                    self._activity("reply hit the context limit; labelled it partial")
                self._settle_unfinished()
                self.state.append_assistant(text)
                return SimpleChatResult(
                    final=text,
                    rounds=round_index + 1,
                    tool_calls=tool_calls,
                    changed_files=tuple(dict.fromkeys(changed)),
                    truncated=cut_off,
                )

            self.state.append_assistant(
                turn.text or "",
                tool_calls=[_call_to_message(call) for call in turn.tool_calls],
            )
            outcome = await self._run_tools(turn.tool_calls)
            tool_calls += len(turn.tool_calls)
            changed.extend(outcome.written)
            if outcome.asked:
                # The turn ends on the question. Not a stop - nothing went
                # wrong, and marking it stopped would file a deliberate,
                # correct ask alongside timeouts and refusals in the run
                # outcome. The question stands in the transcript as the
                # assistant's turn, so whatever the user types next is read as
                # its answer without any pending-question store: the
                # conversation IS the store.
                if outcome.written and self.verify_changes:
                    await self._append_verification(outcome.written)
                self._settle_unfinished()
                self.state.append_assistant(outcome.asked)
                self._activity("asked the user a question; waiting for the answer")
                return SimpleChatResult(
                    final=outcome.asked,
                    rounds=round_index + 1,
                    tool_calls=tool_calls,
                    changed_files=tuple(dict.fromkeys(changed)),
                )
            if self._truncated_refusals >= MAX_TRUNCATED_WRITE_REFUSALS:
                # The guard's exit. Three replies in a row cut off mid-write
                # means the file does not fit in one generation and the model
                # will not break it up on being asked. Spinning on that proves
                # nothing, and every refusal costs a full round.
                named = outcome.refused_truncated or self._truncated_target or "that file"
                if self._refused_unparseable:
                    # Live 2026-08-20 a user was told "cut off by my own output
                    # limit" three times for writes the output limit never
                    # touched - they were refused by the content gate. Blaming a
                    # limit that did not fire sends someone to the wrong dial.
                    reason = (
                        f"My last {self._truncated_refusals} attempts to write {named} "
                        "each stopped part-way through a string or a block, so I refused "
                        "them rather than leave a file on disk that will not parse. "
                        "Nothing was changed.\n\n"
                        "Tell me which function or section to write and I will do that "
                        "one on its own, or say `continue` for the next section only."
                    )
                else:
                    reason = (
                        f"My last {self._truncated_refusals} attempts to write {named} "
                        "were cut off by my own output limit part-way through, so I "
                        "refused all of them rather than leave a half-written file on "
                        "disk. Nothing was changed.\n\n"
                        "The file is too large for me to produce in one reply. Ask me "
                        "for one part at a time - a single function, or one section - "
                        "and I can build it up."
                    )
                return self._stop(reason, round_index, tool_calls, changed)
            if self._stalls.unproductive >= MAX_UNPRODUCTIVE_EDITS:
                # Spinning. Live 2026-08-18 a turn ran 12 no-op patches and 5
                # failed ones across 24 rounds and ~25 minutes, changing nothing
                # - and the only thing that stopped it was max_rounds. Say what
                # happened instead of burning the rest of the budget.
                tried = self._stalls.unproductive
                switch = self._change_of_strategy()
                if switch:
                    # THE THIRD EXIT. This ceiling used to end the turn on an
                    # apology, and the apology asked the user to do the work:
                    # "tell me the exact text to look for". That is the defect
                    # the user reported - four failed matches and the agent
                    # hands the problem back. smallcode's early-stop detector
                    # does not stop here, it changes the approach, and it is
                    # right: patching is one strategy among several and only
                    # one of them has been tried.
                    #
                    # Ours names `replace_symbol`, where smallcode forces a
                    # whole-file rewrite. We have a symbol editor they do not,
                    # and a whole-file rewrite is what the write cap exists to
                    # refuse - recommending it would swap one dead end for
                    # another.
                    self._stalls.unproductive = 0
                    self._strategy_switched = True
                    self.state.append_user(switch, origin=ORIGIN_LOOP)
                    self._activity("patching is not working; asked it to change approach")
                    continue
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
            announce = self._announce_withdrawn_reads()
            if announce:
                # Said once per path, at the moment the tool goes. A model that
                # watches a tool vanish without a word spends a round working
                # out what it did wrong; a model told the same thing every round
                # is the nudge spiral this fix exists to end.
                self.state.append_user(announce, origin=ORIGIN_LOOP)
            looping = self._read_loop.record(
                outcome.tool_names,
                # Producing ANYTHING ends the streak - a file changed, a command
                # run, a note written. Not just a write: an answer is production
                # too, and this guard is about a turn with nothing to show.
                produced_something=bool(outcome.written)
                or bool(set(outcome.tool_names) - LOOKING_TOOLS),
            )
            if looping and looping.reason == READ_LOOP_EXHAUSTED:
                # The one guard signal that ends a turn. Two nudges were spent
                # and ignored; a third sentence costs another eight reads and
                # buys the same nothing.
                self._activity(looping.activity)
                return self._stop(looping.correction, round_index, tool_calls, changed)
            if looping:
                self.state.append_user(looping.correction, origin=ORIGIN_LOOP)
                self._activity(looping.activity)
            if outcome.repeated_read:
                # A read that repeats verbatim is the model having lost track of
                # what it already has, not new information. Say so once and name
                # the result it is re-fetching, rather than letting it spend the
                # round budget re-reading the same listing.
                self.state.append_user(
                    f"You have already called {outcome.repeated_read} this turn and the "
                    "result has not changed. Use the result you already have, and either "
                    "answer now or make a DIFFERENT call.",
                    origin=ORIGIN_LOOP,
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
                    "exact error or the lines around the problem.",
                    origin=ORIGIN_LOOP,
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

        A CONSECUTIVE count, and that is the whole of the 2026-08-20 fix. Their
        condition is `isRepair && attempt > 1` - attempt being the retry number
        of one repair - and ours read a turn-wide tally that only ever went up,
        incremented by ten different things including nudges that are not
        repairs at all. So two failed patches anywhere in a turn switched
        reasoning off for every round after them, including rounds working on
        something else entirely. On a reasoning model that is the repair losing
        its reasoning at the exact moment a brace hunt needs it. Any write that
        lands now resets it: see `_run_tools`.

        `SHAMSU_THINKING_DISABLE=1` forces it off everywhere, their
        SMALLCODE_THINKING_DISABLE equivalent.
        """
        if os.environ.get("SHAMSU_THINKING_DISABLE", "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
        return self._repair_streak > 1

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
            "tools": self._without_broken_tools(
                active_tool_schemas(
                    num_ctx, self._tool_category, self._available_families, self._request
                )
            ),
            "stream": False,
            "think": self._should_think(),
            "options": {
                # Colder on the first retry, warmer on the second. At one fixed
                # temperature a retry produces the same strategy and the same
                # mistake - which is how the same payload went out nine times.
                "temperature": adapted_temperature(self.temperature, self._repair_streak),
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
        ledger_call_id = self._ledger_model_started(messages, kwargs)
        started = time.perf_counter()
        beat = asyncio.ensure_future(self._heartbeat("thinking..."))
        try:
            raw = await self._client_chat(kwargs)
        except Exception as exc:
            self._ledger_model_finished(ledger_call_id, None, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            beat.cancel()
            # `model_seconds` rides along so a display does not have to parse
            # the sentence back into a number. The split between model time and
            # tool time is the whole diagnosis of a turn that took 22 minutes
            # and changed nothing.
            _model_seconds = time.perf_counter() - started
            self._activity(
                f"model responded in {_model_seconds:.0f}s",
                model_seconds=_model_seconds,
            )
        self._record_usage(raw, self._estimate_prompt(messages))
        # The usage numbers only exist once a call has come back, and the meter
        # rides on status events. Without a tick here a fast model never
        # produces one, and the context meter stays blank on exactly the
        # machines where a turn is cheap enough to run many rounds.
        self._status("thinking...")
        self._ledger_model_finished(ledger_call_id, raw, "")
        return raw

    # -- mirroring simple mode into the ledger -----------------------------
    #
    # The session log (`shamsu/ui/turnlog.py`) is built entirely from the
    # ActionLedger, on the documented promise that every execution path records
    # its tools and model calls there. Simple mode is the DEFAULT path and never
    # honoured it, so a real turn produced a log with approvals and file writes
    # in it and no sign of what the agent read, ran, or said. These four are
    # that promise, kept. All best-effort: logging must never break a turn.

    def _notice(self, message: str) -> None:
        """Say something about the TURN, to the console and to the log.

        `_activity` only ever reached a screen, so "context is filling" - the
        one line that explains why every row after it looks different - was
        never in the record anyone reads afterwards."""
        self._activity(message)
        if self.action_ledger is None:
            return
        try:
            self.action_ledger.log_notice(message)
        except Exception:
            pass

    #: Writing tools this loop executes itself, in `_execute`, instead of
    #: handing to the registry. The registry journals a mutation for everything
    #: it runs; these had nobody doing it, so a file they changed never reached
    #: `summary.json:changed_files` - and a turn whose only edit was a
    #: `replace_symbol` reported that nothing happened. Live 2026-08-21: calc.py
    #: was correctly fixed and the run closed "failed" with `changed_files: []`.
    _SELF_EXECUTED_WRITERS = frozenset({"replace_symbol", "append_file"})

    def _ledger_mutation(
        self, name: str, arguments: dict[str, Any], result: ToolResult
    ) -> None:
        """Journal a write this loop performed itself.

        Deliberately narrow: anything the registry ran has already recorded its
        own mutation with real hashes and a rollback, and a second record would
        double-count the file in every changed-files list."""
        if self.action_ledger is None or name not in self._SELF_EXECUTED_WRITERS:
            return
        path = str(
            (result.data or {}).get("resolved_filepath")
            or arguments.get("filepath")
            or ""
        ).strip()
        if not path:
            return
        try:
            self.action_ledger.log_mutation_finished(
                f"txn_{name}_{self._event_seq}",
                "applied",
                [path],
                # No transaction and no backup: this write went straight to
                # disk, so promising a rollback would be a lie.
                rollback_available=False,
            )
        except Exception:
            pass

    def _ledger_tool_call(self, name: str, arguments: dict[str, Any]) -> str:
        if self.action_ledger is None:
            return ""
        try:
            return self.action_ledger.log_tool_call(name, arguments)
        except Exception:
            return ""

    def _ledger_tool_result(self, call_id: str, name: str, result: ToolResult) -> None:
        if self.action_ledger is None or not call_id:
            return
        try:
            self.action_ledger.log_tool_result(
                call_id, name, bool(result.ok), result.message, result.data
            )
        except Exception:
            pass

    def _ledger_model_started(
        self, messages: list[dict[str, Any]], kwargs: dict[str, Any]
    ) -> str:
        if self.action_ledger is None:
            return ""
        try:
            return self.action_ledger.log_model_call_started(
                "coder",
                self.model_name,
                messages=messages,
                tools=kwargs.get("tools") or [],
            )
        except Exception:
            return ""

    def _ledger_model_finished(self, call_id: str, raw: Any, error: str) -> None:
        """Record the response and, when the model produced one, its reasoning.

        The reasoning is logged FIRST so the session log can fold it into the
        response's own entry - it is a sub-panel of that answer, not a step
        before it."""
        if self.action_ledger is None or not call_id:
            return
        try:
            # `_response_field` reads a dict or a pydantic object, which is
            # what the live SDK and the tests respectively hand back.
            message = _response_field(raw, "message") if raw is not None else None
            thinking = str(_response_field(message, "thinking") or "")
            content = str(_response_field(message, "content") or "")
            if thinking:
                # Not a body line: a trace belongs to the response it produced,
                # so a surface renders it inside that entry rather than as its
                # own step. See `body_kinds`.
                self._publish("reasoning", _first_line(thinking), text_full=thinking)
                self.action_ledger.log_model_thinking(
                    call_id, "coder", self.model_name, thinking
                )
            self.action_ledger.log_model_call_finished(
                "coder", self.model_name, content, call_id=call_id, error=error
            )
        except Exception:
            pass

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
        standing = self._standing_plan()
        if standing:
            # The contract has existed for weeks and reached the model only if
            # it called `contract_status` - so the thing meant to keep a
            # multi-step task on the rails was invisible to exactly the model
            # that had lost the thread and stopped asking.
            grounding = standing + chr(10) * 2 + grounding
        if self._project:
            # First, and one line: what KIND of project this is and how its
            # tests run. Without it a model opening a fresh workspace spends
            # three to five calls working that out, every session.
            grounding = self._project + chr(10) * 2 + grounding
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

    def _verbatim_tail(self, fraction: float = VERBATIM_TAIL_FRACTION) -> int:
        """How many recent messages to keep whole, measured in TOKENS.

        Walks back from the newest message spending a share of the history
        budget. Twenty short turns cost almost nothing and all stay; three
        whole-file payloads spend it immediately and only the newest survives.

        Never more than `KEEP_VERBATIM_MESSAGES`, so this cannot make a prompt
        bigger than it was before. Never fewer than `MIN_VERBATIM_MESSAGES`,
        because the current exchange is what the model is working on.
        """
        budget = int(self._history_budget() * fraction)
        history = self.state.all_messages[1:]
        kept = 0
        spent = 0
        for message in reversed(history):
            spent += message_tokens(message.to_ollama())
            if spent > budget and kept >= MIN_VERBATIM_MESSAGES:
                break
            kept += 1
            if kept >= KEEP_VERBATIM_MESSAGES:
                break
        return max(MIN_VERBATIM_MESSAGES, min(kept, KEEP_VERBATIM_MESSAGES))

    def _elide_payloads(self, keep_recent: int | None = None) -> int:
        """Shrink tool payloads older than the last *keep_recent* messages.

        Called at the START of a turn as well as during one. A fresh
        `SimpleChatLoop` - and so a fresh `ChatState` - is built per user
        message, and hydration reloads the transcript from disk with every
        `write_file` payload intact. Eliding only what this turn produced would
        therefore save nothing at all across turns, which is precisely the case
        the 44,833 -> 10,476 measurement was taken on.
        """

        if keep_recent is None:
            keep_recent = self._verbatim_tail()
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
            self._notice(
                f"context is filling and the largest part is {allocation.fattest()}; "
                "eliding payloads will not help much here"
            )
            return self._elide_payloads()
        self._notice("context is filling; eliding older tool payloads")
        return self._elide_payloads(
            self._verbatim_tail(VERBATIM_TAIL_FRACTION_UNDER_PRESSURE)
        )

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
        return self._without_broken_tools(
            active_tool_schemas(
                self._ceiling(), self._tool_category, self._available_families, self._request
            )
        )

    def _without_broken_tools(
        self, schemas: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Drop tools that have failed repeatedly THIS session.

        smallcode's trust decay, with the difference that matters: a writing
        tool is never dropped. Theirs may withhold any tool after five
        consecutive failures, and withholding `patch_file` would leave a model
        that cannot edit anything - a worse state than the loop it prevents. A
        search that keeps returning nothing can be taken away; the ability to
        change a file cannot. `TrustDecay.protected` carries that list.

        Never empties the roster: if everything offered has been failing, the
        answer is not to send zero tools.
        """
        dropped = set(self._trust.dropped())
        if self._read_withdrawn:
            # `read_file` goes for the WHOLE turn, not per path, because a tool
            # schema has no path to scope to. That is a real cost - another file
            # cannot be read whole - and it is bounded three ways: `read_symbol`
            # and `find_and_read` still reach any file, the withdrawal is undone
            # by the first write that lands (see `_run_tools`), and it only ever
            # happens after three reads of one path were answered from cache.
            dropped.add("read_file")
        if not dropped:
            return schemas
        kept = [
            schema
            for schema in schemas
            if (schema.get("function") or {}).get("name") not in dropped
        ]
        return kept or schemas

    def _announce_withdrawn_reads(self) -> str:
        """The one message that says the tool is gone and where the exit is.

        Points at a DIFFERENT ACTION, which is the whole lesson of the three
        runs this came from. The cache note said "use what you have" - true, and
        not something a model can do - and was ignored twelve times in one turn.
        The patch nudge that worked named `replace_symbol`, so this one does
        too.
        """
        fresh = sorted(self._read_withdrawn - self._announced_withdrawal)
        if not fresh:
            return ""
        self._announced_withdrawal.update(fresh)
        named = ", ".join(fresh)
        return (
            f"read_file has been withdrawn for {named}. Every part of it you "
            "asked for has already been given to you - the last three reads "
            "returned nothing new, so reading it again cannot move this "
            f"forward.{chr(10) * 2}"
            "Make the change now, from what you were already shown:\n"
            "- replace_symbol to replace a whole function or class by NAME, or "
            "to DELETE one by passing empty content\n"
            "- patch_file with text copied out of a result you already have\n\n"
            "read_symbol still works if you need one function's source again. "
            "The moment an edit lands, read_file comes back."
        )

    def _read_is_withdrawn(self, arguments: dict[str, Any]) -> ToolResult | None:
        """Refuse a read of a path the tool has been taken away for.

        Needed as well as the schema filter: a model that has seen `read_file`
        for twenty rounds will call it from memory for a round or two after it
        disappears, and answering that with the ordinary cache note would put
        it straight back in the loop this exists to break.
        """
        path = str(arguments.get("filepath") or "").strip()
        if not path or path.lower() not in self._read_withdrawn:
            return None
        self._activity(f"read_file is withdrawn for {path}; refused")
        return ToolResult(
            False,
            f"read_file has been withdrawn for {path}. You have already been "
            "given every part of it you asked for, three times over, and reading "
            f"it again cannot tell you anything new.{chr(10) * 2}"
            "Make the change now: replace_symbol to swap or delete a whole "
            "function by name, or patch_file with text copied from what you were "
            "already shown. read_symbol still works if you need one function's "
            "source again.",
            {"filepath": path, "read_withdrawn": True},
        )

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
            if name in WRITING_TOOLS:
                oversized = self._oversized_content(name, arguments)
                if oversized is not None:
                    self._refuse_oversized_write(call, name, arguments, oversized)
                    continue
                cut = self._truncated_content(name, arguments)
                if cut is not None:
                    self._refuse_cut_off_content(call, name, arguments, cut)
                    continue
            signature = _call_signature(name, arguments)
            if (
                name in WRITING_TOOLS
                and self._stalls.failures.get(signature, 0)
                >= IDENTICAL_FAILURES_BEFORE_REFUSING
            ):
                self._refuse_repeated_failure(call, name, arguments, signature)
                continue
            summary = _argument_summary(arguments)
            self._activity(
                f"{name} {summary}",
                kind="tool.call",
                tool=name,
                summary=summary,
                arguments=_publishable_arguments(arguments),
            )
            self._trace("simple.tool", f"{name} {summary}", {"tool": name})
            tool_started = time.perf_counter()
            # A tool can block for as long as its timeout allows - `run_command`
            # defaults to 120s, and a server started in the foreground will use
            # every second of it. Without a tick that is two minutes of silence
            # immediately AFTER an approval prompt, which reads as a hang.
            # Immediately, not after the first heartbeat. A heartbeat only
            # ticks every HEARTBEAT_SECONDS, and most tools finish inside that
            # - so the spinner said "thinking..." right through a file read and
            # never once named the thing it was doing.
            self._status(f"{name} {summary}".strip())
            beat = asyncio.ensure_future(self._heartbeat(f"running {name}..."))
            # The session log is built from the ledger, on the documented
            # promise that every execution path records its tools there. Simple
            # mode never did, so the default path produced a log of approvals
            # and file writes with no sign of what the agent actually ran.
            ledger_call_id = self._ledger_tool_call(name, arguments)
            try:
                result = await asyncio.to_thread(self._execute, name, arguments)
            finally:
                beat.cancel()
            self._ledger_tool_result(ledger_call_id, name, result)
            # The CLI has never printed this, and at `normal` verbosity no
            # surface shows it either - it is here so the web UI can build a
            # collapsible tool card and a diff preview without re-running
            # anything.
            self._publish(
                "tool.result",
                f"{name} {'ok' if result.ok else 'failed'}",
                tool=name,
                ok=bool(result.ok),
                message=_first_line(result.message),
                # The three fields a terminal needs and could not previously
                # get: how long it took, what it was pointed at, and - for a
                # write - what actually changed. Without the diff here the CLI
                # would have to re-read the file to show one, which is both
                # slow and a lie, because the file may have moved on.
                duration_ms=(time.perf_counter() - tool_started) * 1000,
                target=_argument_summary(arguments),
                diff=_diff_of(result),
            )
            self._trust.record(name, bool(result.ok))
            if not result.ok:
                # Kept for the evidence note. smallcode keeps the error TAIL for
                # the same reason: the last line says what went wrong, the rest
                # is where it happened, and a full trace is 5-50KB.
                self._turn_failures.append((name, _first_line(result.message)))
            if result.ok and (result.data or {}).get("ask_user"):
                # The question, for the loop to end the turn on. Recorded here
                # rather than returned, because the calls after this one in the
                # same generation still deserve to run - the model may have
                # asked and read a file in one turn.
                outcome.asked = result.message.strip() or outcome.asked
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
                swept = self._elide_under_pressure()
                self.evictions += swept
                if swept:
                    # The copies the model "already has" may be exactly what was
                    # just evicted. Forgetting here keeps "unchanged since you
                    # last read it" from ever being a lie.
                    self._read_digests.clear()
                    self._ranges_sent.clear()
            if name in WRITING_TOOLS:
                if result.ok:
                    self._ledger_mutation(name, arguments, result)
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
                    self._repair_streak += 1
                    self._last_failed_path = (
                        str(arguments.get("filepath") or "").strip()
                        or self._last_failed_path
                    )
                else:
                    self._stalls.unproductive = 0
            else:
                read_signature = f"{name}({_read_argument_summary(arguments)})"
                seen = self._read_signatures.get(read_signature, 0) + 1
                self._read_signatures[read_signature] = seen
                if seen >= REPEATED_READS_BEFORE_WARNING:
                    outcome.repeated_read = read_signature
            if result.ok and name in RUNNING_TOOLS:
                # What this session can VOUCH for. `contract_assert_pass` reads
                # it, so an assertion cannot be signed off on a paragraph the
                # model wrote about its own code.
                summary = _argument_summary(arguments) or name
                self._observed_runs.append(f"{name}({summary[:80]}) exited 0")
            if result.ok and name in WRITING_TOOLS:
                # WRITING_TOOLS, not MUTATING_TOOLS: `append_file` puts bytes on
                # disk and was not in the narrower set, so nothing verified a
                # file built up in pieces. The last verdict the model saw was
                # the one taken after the FIRST chunk - "1 unclosed {" - which
                # is true of half a file and false of the finished one, and is
                # exactly the sort of stale verdict that sends a model
                # repairing something already correct.
                path = str(arguments.get("filepath") or "").strip()
                if path:
                    outcome.written.append(path)
                    self._observed_writes.append(path)
                    # It changed, so the remembered read is stale.
                    self._read_digests.pop(path.lower(), None)
                    # Progress. The repair streak is about being STUCK, and a
                    # write that landed is the proof that the model is not - so
                    # a reasoning model gets its reasoning back for whatever it
                    # hits next, instead of losing it for the rest of the turn
                    # over two mistakes it has already recovered from.
                    self._repair_streak = 0
                    # The withdrawal was about a model reading instead of
                    # acting. It has now acted, so the tool comes back - and the
                    # counter with it, or the next re-read would trip the
                    # threshold immediately.
                    self._read_withdrawn.clear()
                    self._blocked_reads.clear()
                    self._announced_withdrawal.clear()
                    grew = _added_to_the_file(name, result)
                    self._last_write_grew[path.lower()] = grew
                    if grew:
                        self._built_up.add(path.lower())
                    if name in MUTATING_TOOLS and not _extended_the_file(result):
                        # Only whole-file writes and patches count toward the
                        # repeated-edit ceiling. Appending section after section
                        # is how a large file is MEANT to be built here, so
                        # counting each one would stop the very behaviour the
                        # truncation refusal asks for.
                        #
                        # `_extended_the_file` extends that exemption from the
                        # TOOL to the SHAPE, and it had to. Live 2026-08-20, told
                        # to build a 1,500-line file, qwen2.5:3b said "I will
                        # write 60 lines at a time" and did exactly that - with
                        # `write_file`, re-sending the growing file each time
                        # rather than appending. Five sections in, all five
                        # verified clean, the turn was stopped for "5 blind edits
                        # I cannot confirm". Every one of them was confirmed and
                        # every one of them added lines; the ceiling was reading
                        # a build as a repair loop. A write that GROWS a file is
                        # the chunked path the prompt now asks for, whichever
                        # tool carries it.
                        count = self._edits_per_file.get(path.lower(), 0) + 1
                        self._edits_per_file[path.lower()] = count
                        outcome.repeated_edit = max(outcome.repeated_edit, count)
                        outcome.repeated_path = path if count >= EDITS_PER_FILE_BEFORE_WARNING else outcome.repeated_path
                # A write that landed intact ends the truncation streak. The
                # counter is about consecutive refusals, not lifetime ones.
                self._truncated_refusals = 0
                self._truncated_target = ""
        return outcome

    def _change_of_strategy(self) -> str:
        """Stop patching, edit by symbol instead - or ``""`` if already said.

        The third exit. Four failed matches used to end the turn with *"I have
        stopped rather than keep guessing. It would help to tell me the exact
        text to look for"* - an apology that hands the work back to the user,
        which is exactly what they reported. Patching is one strategy and it is
        the only one that had been tried.

        Why `replace_symbol` rather than smallcode's forced whole-file rewrite:
        a failing `patch_file` means the model cannot reproduce the old text
        byte-for-byte, and `replace_symbol` is the tool that does not require it
        to - it names the function and sends the new body. A whole-file rewrite
        needs the model to hold the entire file correctly, which is a harder
        version of the thing it is currently failing at, and `MAX_WRITE_CHARS`
        would refuse it for anything sizeable anyway.

        Once per turn. Said twice it stops being a change of strategy and
        becomes the loop repeating itself at the model.
        """
        if self._strategy_switched:
            return ""
        named = self._last_failed_path or self._stalls_last_path()
        target = named or "that file"
        return (
            f"Stop patching {target}. Four edits in a row changed nothing, so the "
            "text you are matching is not what the file actually contains, and "
            "sending it again cannot help.\n\n"
            "Change approach now:\n"
            f"1. read_file on {target} - over 200 lines you get its outline, with "
            "the exact line range of every function and class.\n"
            "2. read_symbol on the ONE function or class that is wrong, to see "
            "its real current text.\n"
            f"3. replace_symbol on {target} with that symbol's name and its "
            "complete new body. It replaces the whole symbol by NAME, so you "
            "never have to reproduce the old text exactly - which is the step "
            "that has been failing."
        )

    def _stalls_last_path(self) -> str:
        """The file most of this conversation's failures were aimed at."""
        counts: dict[str, int] = {}
        for signature, failures in self._stalls.failures.items():
            path = _signature_path(signature)
            if path:
                counts[path] = counts.get(path, 0) + failures
        if not counts:
            return ""
        return max(counts.items(), key=lambda item: item[1])[0]

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
        self._repair_streak += 1
        self._activity(f"{name} on {named} already failed {seen} times identically; not run")
        self._trace(
            "simple.refused_repeat", f"{name} {named}", {"tool": name, "attempts": seen}
        )
        self.state.append_tool(_call_id(call), name, _budgeted(result.to_json()))

    def _max_write_chars(self) -> int:
        """This session's content cap, from the reply budget that last bound.

        `_last_reply_cap` is the number the last generation actually carried;
        before the first call there is none, so fall back to the reserve - the
        floor `_reply_cap` itself can never go below.
        """
        return max_write_chars(self._last_reply_cap or output_reserve(self._ceiling()))

    def _spill_oversized(self, target: str, content: str) -> str:
        """Save a refused payload where the model can still fetch it.

        The refusal used to end "Nothing you generated is lost; resend it in
        pieces", and that was true for about four messages. `_shorten_arguments`
        drops content arguments out of the history and `MIN_VERBATIM_MESSAGES`
        is 4, so a model that does anything else first comes back to find its
        own text replaced by `<omitted from history>`. Live 2026-08-22: a
        10,477-character plan was refused, the model read eight more times, and
        then rebuilt the document into its reply - where the reply cap cut it
        off. Two ceilings refused the same deliverable and the second one was
        only reached because the first had lied about the first one.

        Under `.shamsu/`, not in the tree: a half-written document appearing
        next to the user's source is worse than the dead end it fixes. Returns
        the workspace-relative path, or "" - a spill that cannot be written is
        never a reason to fail a turn, and the caller says something else then.
        """
        try:
            stem = Path(str(target or "content")).name or "content"
            safe = "".join(ch if ch.isalnum() or ch in "-._" else "-" for ch in stem)[:60]
            folder = self.workspace / ".shamsu" / "oversized"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{self.turn_id or 'turn'}-{safe}"
            path.write_text(content, encoding="utf-8")
            return path.relative_to(self.workspace).as_posix()
        except Exception:  # noqa: BLE001 - observability, never a turn failure
            return ""

    def _content_argument(self, arguments: dict[str, Any]) -> tuple[str, str]:
        """The payload this call carries, and which argument holds it."""
        for key in CONTENT_ARGUMENTS:
            value = arguments.get(key)
            if isinstance(value, str) and value:
                return key, value
        return "", ""

    def _oversized_content(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[str, str, int] | None:
        """Is this call carrying more than one call may carry?

        Returns `(argument, content, cap)` when it is, `None` otherwise. The
        check is on the ARGUMENT, before anything is executed: the content was
        fully generated and is merely rejected at the door, rather than never
        having existed - which is the whole point of moving the limit here from
        the output cap.

        "The model still holds every character of it" is what this used to say,
        and it holds for about four messages: `_shorten_arguments` drops content
        arguments and `MIN_VERBATIM_MESSAGES` is 4. `_spill_oversized` is what
        actually makes the payload recoverable.
        """
        key, content = self._content_argument(arguments)
        if not key:
            return None
        cap = self._max_write_chars()
        if len(content) <= cap:
            return None
        return key, content, cap

    def _refuse_oversized_write(
        self,
        call: Any,
        name: str,
        arguments: dict[str, Any],
        oversized: tuple[str, str, int],
    ) -> None:
        """Refuse a payload too large to survive the wire, and NAME THE STRATEGY.

        The error is the whole value here. A refusal that states only the limit
        converts one unrecoverable failure into one useless round; a refusal
        that names the next call converts it into a recoverable one, and the
        model learns the strategy at the moment it needs it. Vague guidance cost
        674s and a failure on 2026-08-18 where naming the call took 42s.
        """
        key, content, cap = oversized
        target = str(
            arguments.get("filepath") or arguments.get("path") or arguments.get("file") or ""
        ).strip()
        named = target or "that file"
        lines = content.count(chr(10)) + 1
        self._repair_streak += 1

        message = (
            f"REFUSED - nothing was written and {named} is unchanged on disk."
            + chr(10) * 2
            + f"{name}: {key} too large ({lines:,} lines / {len(content) / 1024:.1f}KB). "
            f"Tool calls larger than {cap / 1024:.1f}KB cannot be parsed reliably, so "
            "sending this would have been cut off mid-argument rather than written."
            + chr(10) * 2
        )
        if write_budget_is_unworkable(self._last_reply_cap or output_reserve(self._ceiling())):
            # The floor bound, not either wall. Chunking to 1,700 characters at
            # a time is not a strategy, it is 24 rounds of nothing.
            message += (
                "There is also too little room left in this conversation to write in "
                "useful pieces. Say `/new` to start a fresh conversation, then ask for "
                "this file again."
            )
        else:
            message += (
                f"Strategy: call write_file for {named} with just the first "
                f"{WRITE_LINES_GUIDANCE} lines - imports and empty stubs are enough - "
                "then call append_file once per following section, "
                f"{WRITE_LINES_GUIDANCE} lines at a time. Keep every call under "
                f"{WRITE_LINES_GUIDANCE} lines and under {cap:,} characters."
            )
            spill = self._spill_oversized(target, content)
            if spill:
                message += (
                    f" Your full text is saved at {spill} - read_file it "
                    f"{WRITE_LINES_GUIDANCE} lines at a time and send each piece, "
                    "rather than generating it again."
                )
            else:
                message += " Resend it in pieces from what you just generated."
        result = ToolResult(
            False,
            message,
            {
                "refused": "content_too_large",
                "tool": name,
                "argument": key,
                "chars": len(content),
                "max_chars": cap,
            },
        )
        self._turn_failures.append((name, _first_line(message)))
        self._activity(
            f"{name} carried {len(content):,} chars for {named}; the cap is {cap:,}, refused"
        )
        self._trace(
            "simple.refused_oversized",
            f"{name} {named}",
            {"tool": name, "filepath": target, "chars": len(content), "max_chars": cap},
        )
        self.state.append_tool(_call_id(call), name, _budgeted(result.to_json()))

    def _truncated_content(
        self, name: str, arguments: dict[str, Any]
    ) -> tuple[str, str] | None:
        """Does this payload carry a TRUNCATION SIGNATURE? `(argument, why)`.

        The gate that was missing for a brand-new file: both write-time gates in
        the tool layer bail out when the target does not already exist, so
        SHAMSU would refuse to damage a good file and happily create a broken
        one - and only for Python at that.

        What it must NOT do is test for validity. Under a chunking strategy the
        first section of a file correctly has unclosed blocks, and a gate that
        refused those would refuse every legitimate first chunk. So it tests for
        the shapes a generation cut mid-token leaves behind - an unterminated
        string, a dangling operator, a bracket opened on the last line and never
        closed - and lets an unfinished-but-clean section through.
        """
        if name not in WHOLE_FILE_ARGUMENTS:
            # A fragment cannot be judged as a file. See WHOLE_FILE_ARGUMENTS.
            return None
        key, content = self._content_argument(arguments)
        if not key:
            return None
        path = str(arguments.get("filepath") or arguments.get("path") or "").strip()
        why = truncation_signature(content, suffix=Path(path).suffix if path else "")
        if not why:
            return None
        return key, why

    def _refuse_cut_off_content(
        self,
        call: Any,
        name: str,
        arguments: dict[str, Any],
        cut: tuple[str, str],
    ) -> None:
        """Do not create a file that stops mid-construct.

        Counted into the truncation streak, because that is what this is - the
        same failure the `done_reason` guard catches, seen from the argument
        instead of from the response. Sharing the counter means the streak's
        exit covers both, rather than a model that truncates without Ollama
        admitting it spinning past a guard that never counts.
        """
        key, why = cut
        target = str(
            arguments.get("filepath") or arguments.get("path") or arguments.get("file") or ""
        ).strip()
        named = target or "that file"
        self._truncated_refusals += 1
        self._truncated_target = target
        self._refused_unparseable = True
        self._repair_streak += 1

        message = (
            f"REFUSED - nothing was written and {named} is unchanged on disk."
            + chr(10) * 2
            + f"The {key} you sent stops part-way through: {why}. Writing it would put a "
            "file on disk that cannot be parsed."
            + chr(10) * 2
            + f"Send {named} in pieces instead: write_file with the first "
            f"{WRITE_LINES_GUIDANCE} lines, ending on a COMPLETE line, then append_file "
            "for each following section. An unfinished section is fine - an unfinished "
            "line is not."
        )
        result = ToolResult(
            False, message, {"refused": "content_truncated", "tool": name, "why": why}
        )
        self._turn_failures.append((name, _first_line(message)))
        self._activity(f"{name} content for {named} {why}; refused rather than writing it")
        self._trace(
            "simple.refused_cut_off",
            f"{name} {named}",
            {"tool": name, "filepath": target, "why": why},
        )
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
        self._repair_streak += 1

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
        if name == "read_symbol":
            return self._read_symbol(arguments)
        if name == "replace_symbol":
            return self._replace_symbol(arguments)
        if name.startswith("contract_"):
            return self._contract_tool(name, arguments)
        if name == "run_tests":
            return self._run_tests(arguments)
        if name == "use_skill":
            return self._use_skill(arguments)
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
            # A correction, not a repetition. This used to answer an invented
            # name with the full list of thirty - the same list already in the
            # prompt the model has just demonstrated it is not reading - handed
            # back at the exact moment it is confused.
            close = closest_tool_names(name, sorted(SIMPLE_TOOLS))
            hint = invented_capability_hint(name)
            if close:
                suggestion = f" Did you mean {' or '.join(close)}?"
            elif hint:
                # A name that is nowhere near a real one is a model asking for
                # a CAPABILITY, not fumbling a spelling, and the list of every
                # tool answers neither question. Live: `plan`.
                suggestion = f" {hint}"
            else:
                # Still no list of thirty. The categories are how tools are
                # reached here anyway, and naming the door is shorter than
                # naming every room behind it.
                suggestion = (
                    " Call select_category to see the tools for what you are doing."
                )
            return ToolResult(
                False,
                f"There is no tool called {name}.{suggestion}",
                {"unknown_tool": name, "closest": close},
            )
        if name == "read_file":
            withdrawn = self._read_is_withdrawn(arguments)
            if withdrawn is not None:
                return withdrawn
            already = self._already_sent_this_range(arguments)
            if already is not None:
                return already
        if name in {"patch_file", "read_and_patch"}:
            arguments = _strip_line_numbers(arguments)
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
            result = self._outline_instead_of_body(arguments, result)
            result = self._note_unchanged_since_last_read(arguments, result)
            result = self._number_the_lines(arguments, result)
            self._note_partial_read(arguments, result)
            self._record_range_sent(arguments, result)
        if before is not None and result.ok:
            erased = self._erased_a_definition(name, arguments, before)
            if erased is not None:
                return erased
            return self._with_diff(arguments, before, result)
        return result

    def _erased_a_definition(
        self, name: str, arguments: dict[str, Any], before: str
    ) -> ToolResult | None:
        """Put the file back if the edit removed the last of something.

        Judged after the write and rolled back, which is the pattern
        `_append_file` already uses and for the same reason: the question needs
        a real parse of the real result, and a parse needs the file.

        `replace_symbol` is exempt - it has its own, better-targeted guard
        (`_members_lost`) that knows which symbol was deliberately replaced, and
        deleting by name through it is now an explicit, supported move.
        """
        if name not in {"patch_file", "read_and_patch", "write_file"}:
            return None
        path = str(arguments.get("filepath") or "").strip()
        if not path:
            return None
        try:
            target = self.tools.sandbox.validate(path)
            after = target.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - unreadable is not evidence of loss
            return None
        erased = _symbols_erased(before, after, target.suffix)
        if not erased:
            return None
        try:
            target.write_text(before, encoding="utf-8", newline="")
        except OSError:
            # Could not undo it. Say so loudly rather than reporting a refusal
            # that did not happen - the file really is changed.
            return ToolResult(
                False,
                f"{path} lost {', '.join(erased)} and could NOT be restored. "
                "Check the file before going further.",
                {"filepath": path, "erased": erased, "rolled_back": False},
            )
        listed = ", ".join(erased[:6]) + (f" (+{len(erased) - 6} more)" if len(erased) > 6 else "")
        self._activity(f"that edit would have deleted {listed}; put {path} back")
        return ToolResult(
            False,
            f"NOT APPLIED - {path} is back as it was.{chr(10) * 2}"
            f"That edit removed the last definition of: {listed}. The file still "
            f"parsed afterwards, which is why nothing else caught it.{chr(10) * 2}"
            "If you meant to delete only a DUPLICATE, the other copy has to "
            "survive - narrow the range so it does not reach past the copy you "
            "are removing. If you did mean to delete these, say so and use "
            "replace_symbol on each one by name.",
            {"filepath": path, "erased": erased, "rolled_back": True},
        )

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
        healthy = existed and bool(before.strip()) and check_file(target).status != VERIFY_PROBLEM
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(before + joiner + content, encoding="utf-8", newline="")
        except OSError as exc:
            return ToolResult(False, f"Could not write {path}: {exc}", {"filepath": path})
        broke = self._append_broke_it(target, healthy)
        if broke:
            # Put it back. Live 2026-08-20 on qwen2.5-coder:3b, the model was
            # shown a REPLACEMENT for `takeDamage` and appended it to the end of
            # the file instead - past the closing brace of the class, so the
            # method landed at top level and node rejected the whole module. The
            # verifier said so; the model appended the same eleven lines AGAIN
            # and broke it a second way.
            #
            # Structural counting cannot catch this: the appended block is
            # perfectly brace-balanced. Only a real parser sees it, and a real
            # parser needs the file on disk - so the write happens, is judged,
            # and is rolled back. Silent when the file was ALREADY failing,
            # because a file being built in sections is failing by design and
            # refusing to grow it would break chunked writing entirely.
            try:
                target.write_text(before, encoding="utf-8", newline="")
            except OSError:
                pass
            return ToolResult(
                False,
                f"NOT APPENDED - {path} is back as it was.{chr(10) * 2}"
                f"Adding that to the END of the file broke it: {broke}{chr(10) * 2}"
                "If that content REPLACES something already in the file, use "
                "replace_symbol to name what it replaces, or patch_file with the exact "
                "old text. append_file only adds to the end, which is right for a new "
                "section and wrong for a rewrite.",
                {"filepath": path, "would_break": broke, "rolled_back": True},
            )
        added = content.count(chr(10)) + 1
        total = (before + joiner + content).count(chr(10)) + 1
        return ToolResult(
            True,
            f"Appended {added} line(s) to {path}; it is now {total} lines."
            + ("" if existed else " (the file did not exist and was created)"),
            {"filepath": path, "added_lines": added, "total_lines": total},
        )

    def _outline_instead_of_body(
        self, arguments: dict[str, Any], result: ToolResult
    ) -> ToolResult:
        """Hand back the SHAPE of a large file, not its contents.

        Only when the model asked for the whole thing. An explicit
        start_line/end_line is a request for exactly those lines and is answered
        with exactly those lines - the outline is what replaces the *unbounded*
        read, which is the one that could never fit.

        Marked as a partial read on the way out, which matters: the model has
        genuinely not seen the body, so `_refuse_blind_overwrite` must still stop
        it rewriting the file whole. An outline that quietly counted as having
        read the file would license exactly the data loss that guard exists for.
        """
        if not result.ok:
            return result
        if arguments.get("start_line") or arguments.get("end_line"):
            return result
        data = dict(result.data) if isinstance(result.data, dict) else {}
        body = str(data.get("content") or "")
        relative = str(data.get("resolved_filepath") or arguments.get("filepath") or "")
        if not body or not relative:
            return result
        total = int(data.get("total_lines") or (body.count(chr(10)) + 1))
        if total <= OUTLINE_OVER_LINES:
            return result
        suffix = Path(relative).suffix
        if not can_outline(suffix):
            # No parser and no declaration scan for this file type - a `.md`,
            # a `.txt`, a `.csv`. Inventing structure for those would be worse
            # than not trying, but the old answer was a HEAD CLIP, and a head
            # clip is what starts the dead end in SMALLCODE_GAP_ANALYSIS.md §2:
            # the model never sees how the file ends, so it patches against a
            # half it was never shown. smallcode's read guard keeps both ends.
            return self._head_and_tail(relative, body, total, data)
        rendered = render_outline(relative, body, suffix)
        if not rendered:
            return result
        data["content"] = rendered
        data["outlined"] = True
        # `_note_partial_read` keys on this, and it is TRUE in the only sense
        # that matters here: the bodies were not sent.
        data["truncated"] = True
        self._activity(f"{relative} is {total:,} lines; sent its outline instead of the body")
        return ToolResult(
            True,
            f"{relative} is {total:,} lines, so this is its outline rather than its "
            "contents. Call read_symbol for one function or class, or read_file with "
            "start_line and end_line for a specific part.",
            data,
        )

    def _head_and_tail(
        self, relative: str, body: str, total: int, data: dict[str, Any]
    ) -> ToolResult:
        """Both ends of a file nothing can outline, rather than only the first.

        smallcode's `read_guard.js` trims head AND tail; SHAMSU's fallback kept
        the head alone. For a 900-line changelog or a long `.md` spec that means
        the model is shown the oldest entries and never the newest - and then
        asked to add one at the end, against text it has not seen.

        Deliberately not applied to code: `can_outline` handled that first, and
        an outline beats any amount of head-and-tail because it shows the whole
        shape rather than two arbitrary slices.
        """
        lines = body.splitlines()
        if len(lines) <= HEAD_TAIL_LINES * 2 + 10:
            # Small enough that two ends would overlap, or nearly. Sending it
            # whole is both simpler and more useful.
            return ToolResult(True, f"{relative} ({total:,} lines).", data)
        head = lines[:HEAD_TAIL_LINES]
        tail = lines[-HEAD_TAIL_LINES:]
        hidden = len(lines) - (HEAD_TAIL_LINES * 2)
        shown = (
            chr(10).join(head)
            + f"{chr(10)}{chr(10)}... [{hidden:,} lines not shown - "
            f"read_file with start_line and end_line for any of them] ...{chr(10)}{chr(10)}"
            + chr(10).join(tail)
        )
        data = {**data, "content": shown, "truncated": True, "head_and_tail": True}
        self._activity(
            f"{relative} is {total:,} lines; sent its first and last "
            f"{HEAD_TAIL_LINES} lines"
        )
        return ToolResult(
            True,
            f"{relative} is {total:,} lines, so this is its beginning and its END - "
            f"{hidden:,} lines in the middle are not shown. Call read_file with "
            "start_line and end_line for any part of the middle.",
            data,
        )

    def _standing_plan(self) -> str:
        """The active contract, re-shown as this turn's plan, or ``""``.

        Read from disk rather than cached on the loop, because a fresh
        `SimpleChatLoop` is built per user message and the plan has to survive
        that - which is the whole point of anchoring it.
        """
        try:
            from shamsu.agents.simple_contract import contract_disabled, load_contract

            if contract_disabled():
                return ""
            contract = load_contract(self.workspace)
        except Exception:  # noqa: BLE001 - an anchor must never fail a turn
            return ""
        if contract is None or contract.done:
            # A finished contract is not a plan, it is history. Re-showing it
            # would tell a model starting something new to keep working through
            # a list it has already completed.
            return ""
        return plan_anchor(contract.render())

    def _contract_blocks_this_claim(self, text: str, already_nudged: int) -> str:
        """The contract's correction for a premature "done", or ``""``."""
        if already_nudged >= MAX_CONTRACT_NUDGES:
            return ""
        try:
            from shamsu.agents import simple_contract as contracts

            return contracts.done_guard(contracts.load_contract(self.workspace), text)
        except Exception:  # noqa: BLE001 - a contract must never end a turn
            return ""

    def _use_skill(self, arguments: dict[str, Any]) -> ToolResult:
        """The body of one skill, by name.

        The catalogue, the frontmatter parsing and the override rules all
        existed already - bundled, then user, then workspace, later winning -
        and nothing in simple mode had ever called them. A skill nobody can load
        is a document, not a capability.

        A miss lists what IS available, for the same reason `read_symbol` does:
        the model asked the right kind of question with the wrong noun, and the
        roster answers it in the same round.
        """
        wanted = str(arguments.get("name") or arguments.get("skill") or "").strip()
        catalog = _skill_catalog(self.workspace)
        if not catalog.skills:
            return ToolResult(
                False, "There are no skills installed in this workspace.", {"skills": []}
            )
        if not wanted:
            return ToolResult(
                False,
                "use_skill needs a name. Available: "
                + ", ".join(sorted(catalog.skills)),
                {"skills": sorted(catalog.skills)},
            )
        skill = catalog.skills.get(wanted) or catalog.skills.get(wanted.lower())
        if skill is None:
            lowered = wanted.lower()
            skill = next(
                (
                    candidate
                    for name, candidate in catalog.skills.items()
                    if lowered in name.lower()
                ),
                None,
            )
        if skill is None:
            return ToolResult(
                False,
                f"There is no skill called {wanted!r}. Available: "
                + ", ".join(sorted(catalog.skills)),
                {"skills": sorted(catalog.skills)},
            )
        body = (skill.instructions or "").strip()
        if not body:
            return ToolResult(
                False, f"The skill {skill.name!r} has no instructions.", {"skill": skill.name}
            )
        self._activity(f"loaded the {skill.name} skill")
        return ToolResult(
            True,
            f"Skill: {skill.name} - {skill.description}" + chr(10) * 2 + body,
            {"skill": skill.name, "source": skill.source},
        )

    def _run_tests(self, arguments: dict[str, Any]) -> ToolResult:
        """Find this project's test command and run it.

        Delegated to `run_command` rather than executed here, deliberately: that
        path already carries approval, the command risk classifier and output
        redaction, and a second way to run a subprocess would be a second place
        for those to be forgotten.
        """
        found = detect_test_command(self.workspace, str(arguments.get("test_filter") or ""))
        if not found:
            return ToolResult(
                False,
                f"I could not work out how to run tests here: {found.reason}. "
                "If you know the command, call run_command with it - or say what it "
                "is and I will use it from now on.",
                {"detected": False, "reason": found.reason},
            )
        self._activity(f"running tests: {found.command} ({found.reason})")
        result = self._execute("run_command", {"command": found.command})
        prefix = f"Ran `{found.command}` ({found.reason})." + chr(10) * 2
        data = dict(result.data) if isinstance(result.data, dict) else {}
        data.update({"detected": True, "command": found.command, "reason": found.reason})
        return ToolResult(result.ok, prefix + (result.message or ""), data)

    def _number_the_lines(
        self, arguments: dict[str, Any], result: ToolResult
    ) -> ToolResult:
        """Put a line number in front of every line of a read.

        smallcode does this on every read and it is the cheapest accuracy win
        available: a model that has seen `412| ` can ask for line 412. Without
        it, `start_line` is arithmetic performed on a wall of text, and the
        outline's ranges point into a body with no landmarks.

        The obvious hazard is the model copying the gutter back into a
        `patch_file` old_string, which would never match. `_strip_line_numbers`
        removes it on the way back in, so the round trip is safe.
        """
        if not result.ok:
            return result
        data = dict(result.data) if isinstance(result.data, dict) else {}
        if data.get("outlined") or data.get("unchanged"):
            return result  # already a summary; numbering it would be nonsense
        body = str(data.get("content") or "")
        if not body:
            return result
        first = int(data.get("start_line") or 1)
        width = len(str(first + body.count(chr(10))))
        numbered = chr(10).join(
            f"{str(first + offset).rjust(width)}{LINE_NUMBER_GUTTER} {line}"
            for offset, line in enumerate(body.split(chr(10)))
        )
        data["content"] = numbered
        data["line_numbers"] = True
        return ToolResult(result.ok, result.message, data)

    def _note_unchanged_since_last_read(
        self, arguments: dict[str, Any], result: ToolResult
    ) -> ToolResult:
        """Say "unchanged" instead of resending a file the model already has.

        Live 2026-08-20 a user watched eight `read_file js/game.js` calls in one
        turn. Every one re-sent the whole file, and by the third the window was
        being elided to make room for copies of a file that had not changed.

        smallcode's Feature 16 does this and stops there. That would be unsafe
        here, because SHAMSU elides old tool payloads under pressure - so the
        copy the model "already has" may have been evicted, and answering
        "unchanged" would leave it with nothing at all. The memory of what was
        sent is therefore dropped whenever an elision sweep runs, which makes
        this claim true whenever it is made.
        """
        if not result.ok or arguments.get("start_line") or arguments.get("end_line"):
            return result
        data = dict(result.data) if isinstance(result.data, dict) else {}
        path = str(data.get("resolved_filepath") or arguments.get("filepath") or "")
        body = str(data.get("content") or "")
        if not path or not body:
            return result
        digest = sha256(body.encode("utf-8", "replace")).hexdigest()
        key = path.lower()
        if self._read_digests.get(key) == digest:
            total = data.get("total_lines") or (body.count(chr(10)) + 1)
            self._activity(f"{path} is unchanged since the last read; did not resend it")
            return ToolResult(
                True,
                f"{path} is unchanged since you last read it ({total:,} lines). "
                "Use the copy you already have. If you need a part of it again, "
                "call read_file with start_line and end_line, or read_symbol.",
                {**data, "content": "", "unchanged": True},
            )
        self._read_digests[key] = digest
        return result

    def _contract_tool(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        """The five Definition-of-Done calls, over one on-disk contract.

        On disk because a `SimpleChatLoop` is rebuilt for every user message: a
        contract held on the object would reset the moment the user typed, which
        is precisely how the unproductive-edit counter failed to fire for
        months. A contract that does not outlive one turn is not a contract.
        """
        from shamsu.agents import simple_contract as contracts

        if contracts.contract_disabled():
            return ToolResult(
                False, "Contracts are switched off (SHAMSU_CONTRACT=0).", {"disabled": True}
            )
        if name == "contract_create":
            raw = arguments.get("assertions")
            if isinstance(raw, str):
                # A small model sends a newline- or comma-separated string about
                # as often as it sends a list. Both mean the same thing.
                raw = [part for part in re.split(r"[" + chr(10) + r";]|(?<=[.)])\s*,", raw) if part.strip()]
            items = [str(item) for item in (raw or []) if str(item).strip()]
            if not items:
                return ToolResult(
                    False,
                    "contract_create needs at least one assertion - a checkable claim "
                    "like 'npm test exits 0'.",
                    {},
                )
            contract = contracts.new_contract(
                str(arguments.get("title") or ""), str(arguments.get("brief") or ""), items
            )
            contracts.save_contract(self.workspace, contract)
            self._activity(f"contract set: {contract.title} ({len(contract.assertions)} assertions)")
            return ToolResult(True, contract.render(), {"assertions": len(contract.assertions)})

        contract = contracts.load_contract(self.workspace)
        if contract is None:
            return ToolResult(
                False,
                "There is no contract for this task yet. Call contract_create with what "
                "done means, then check the assertions off.",
                {"contract": None},
            )
        if name == "contract_status":
            return ToolResult(True, contract.render(), {"done": contract.done})

        wanted = str(arguments.get("assertion_id") or arguments.get("id") or "").strip()
        item = contract.find(wanted)
        if item is None and not wanted and len(contract.blockers) == 1:
            # The model sent evidence and no id. Measured across four live runs
            # on qwen3.5:9b this was the single most-failing call in the whole
            # roster - 23 refusals, more than every other tool combined - and
            # every one of them carried real evidence for the only thing left
            # to check. Refusing that is the harness insisting on a label while
            # the model hands it the answer.
            #
            # Only when exactly ONE is unresolved, because then there is nothing
            # to guess between. With several, the id genuinely matters and the
            # error below says so.
            item = contract.blockers[0]
        if item is None and wanted:
            # An id that matched nothing, but the TEXT of an assertion might.
            # A model that quotes the assertion instead of naming it has still
            # said which one unambiguously.
            lowered = wanted.lower()
            matches = [
                entry for entry in contract.assertions
                if lowered in entry.text.lower() or entry.text.lower() in lowered
            ]
            if len(matches) == 1:
                item = matches[0]
        if item is None:
            listed = ", ".join(
                f"{entry.id} ({entry.state})" for entry in contract.assertions
            )
            missing = (
                "You did not send an assertion_id. "
                if not wanted
                else f"There is no assertion {wanted!r}. "
            )
            return ToolResult(
                False,
                f"{missing}This contract has: {listed}. "
                "Pass the id of the one you are recording.",
                {"assertions": listed},
            )
        detail = str(
            arguments.get("evidence") or arguments.get("reason") or ""
        ).strip()
        if name == "contract_assert_pass":
            if not detail:
                return ToolResult(
                    False,
                    f"{item.id} needs evidence: what did you run, and what did it say? "
                    "An assertion marked passed without evidence is the claim this "
                    "contract exists to stop.",
                    {"assertion": item.id},
                )
            # ...and a non-empty string was the ONLY thing this used to require,
            # which is how seven assertions came to be marked passed on seven
            # confident paragraphs about a game that drew neither the ship nor a
            # single asteroid (live 2026-08-22, demo2/test). The
            # refusal above asks exactly the right question - "what did you run,
            # and what did it say?" - and then accepted prose that answered
            # neither. Evidence is what the HARNESS saw.
            if self._observed_runs:
                backing, observation = contracts.BY_RUN, self._observed_runs[-1]
            elif self._observed_writes:
                backing = contracts.BY_WRITE
                observation = f"wrote {self._observed_writes[-1]} (not run)"
            else:
                return ToolResult(
                    False,
                    f"{item.id} cannot be marked passed: nothing has been run and "
                    "nothing has been written in this session, so there is nothing "
                    "to back it. Reading the code is not checking it - a model "
                    "describing what it believes its own code does is the claim "
                    "this contract exists to stop." + chr(10) * 2
                    + "Run something that exercises it (run_tests, or run_command "
                    "with whatever checks this project) and record what it printed. "
                    f"If it cannot be checked here, use contract_assert_skip on "
                    f"{item.id} and say why.",
                    {"assertion": item.id, "refused": "no_observation"},
                )
            item.state, item.evidence = contracts.PASSED, detail
            item.verified_by, item.observation = backing, observation
        elif name == "contract_assert_fail":
            item.state, item.evidence = contracts.FAILED, detail or "no detail given"
        elif name == "contract_assert_skip":
            if not detail:
                return ToolResult(
                    False, f"{item.id} needs a reason to be skipped.", {"assertion": item.id}
                )
            item.state, item.evidence = contracts.SKIPPED, detail
        else:
            return ToolResult(False, f"There is no tool called {name}.", {})
        contracts.save_contract(self.workspace, contract)
        self._activity(f"{item.id} {item.state}: {item.text[:60]}")
        return ToolResult(True, contract.render(), {"assertion": item.id, "state": item.state})

    def _replace_symbol(self, arguments: dict[str, Any]) -> ToolResult:
        """Swap one whole function or class for new source, by name.

        The move `patch_file` could never make cheaply. Replacing a whole
        function with `old_string` means reproducing every line of the old one
        exactly - and a small model that can write the NEW function correctly
        will still fail to retype the OLD one, which is the failure the patch
        error message spends its whole body trying to correct. Naming the symbol
        removes the retyping entirely.

        The range comes from the same parse `read_symbol` returns, so what is
        replaced is exactly what the model was shown.

        Two guards, and the second is the one that makes this safe:

        - the replacement is re-indented to the original's column when the model
          sends a method without its class indentation, which is the mistake a
          small model makes most often here;
        - the RESULTING FILE must still parse if the original did. That check is
          possible here and is not possible for `patch_file` fragments, because
          this tool produces a complete file rather than a snippet - so it can
          ask the whole-file question honestly instead of guessing at a
          fragment.
        """
        path = str(arguments.get("filepath") or "").strip()
        wanted = str(arguments.get("symbol") or arguments.get("name") or "").strip()
        # A signature is a near-miss, not a mistake. Live 2026-08-21 on
        # qwen3.5:9b the model asked for `handlePauseMenuAction(action)` - the
        # heading it had just been shown by the outline - and the lookup, which
        # matches on the bare name, found nothing. Same class as the argument
        # aliases: refusing a call that named the right thing in a slightly
        # different shape costs a whole round and teaches nothing.
        wanted = wanted.split("(", 1)[0].strip() or wanted
        content = arguments.get("content")
        if not path or not wanted or not isinstance(content, str):
            return ToolResult(
                False, "replace_symbol needs a filepath, a symbol and content.", {}
            )
        # EMPTY CONTENT MEANS DELETE THE SYMBOL, and refusing it was the whole
        # of the reported failure. Live, the model found three functions defined
        # twice in one file and did the correct thing: replace the duplicate
        # with nothing. It was told twice that it had forgotten an argument, ran
        # out of rounds, and changed nothing in 319 seconds.
        #
        # There is no other way to remove a function here: `patch_file` would
        # need the exact text of a thirty-line body, which is the retyping this
        # tool exists to avoid. A deletion is journaled and undoable like any
        # other mutation, and a generation cut off before its content is caught
        # upstream by `_refuse_truncated_write`, so the truncation case cannot
        # reach this branch.
        deleting = not content.strip()
        try:
            target = self.tools.sandbox.validate(path)
        except Exception as exc:  # noqa: BLE001 - the sandbox owns this refusal
            return ToolResult(False, str(exc), {"filepath": path})
        if not target.is_file():
            return ToolResult(
                False, f"Not a file: {path}. Use find_files to locate it.", {"filepath": path}
            )
        try:
            original = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(False, f"Could not read {path}: {exc}", {"filepath": path})

        suffix = target.suffix
        found = find_symbol(original, suffix, wanted)
        if found is None:
            names = [symbol.name for symbol in outline(original, suffix)]
            return ToolResult(
                False,
                _no_such_symbol(path, wanted, names)
                + " Use patch_file if you meant to change something that is not a whole "
                "function or class.",
                {"filepath": path, "symbol": wanted, "available": names[:60]},
            )

        lines = original.splitlines()
        if deleting:
            # Drop the range outright rather than replacing it with a blank
            # line, and take one trailing blank with it so removing a function
            # does not leave a widening gap where it used to be.
            after = found.end
            if after < len(lines) and not lines[after].strip():
                after += 1
            updated = lines[: found.start - 1] + lines[after:]
            reindented = False
        else:
            replacement, reindented = _match_indentation(
                lines[found.start - 1], content.rstrip(chr(10))
            )
            updated = lines[: found.start - 1] + replacement.split(chr(10)) + lines[found.end :]
        body = chr(10).join(updated)
        if original.endswith(chr(10)):
            body += chr(10)
        if body == original:
            return ToolResult(
                False,
                f"{found.name} in {path} is already exactly that - nothing changed.",
                {"filepath": path, "symbol": found.name, "unchanged": True},
            )

        if deleting:
            # THE SAME RULE `patch_file` GETS, and it was missing here because
            # this path was exempted from `_erased_a_definition` on the
            # assumption `_members_lost` below covered it. It does not:
            # `_members_lost` asks what vanished from INSIDE a replaced symbol,
            # never whether the symbol itself is now gone from the file.
            #
            # Live 2026-08-21 that cost a function. A patch removed one of two
            # `handleMouseMove` definitions - allowed, one remained - and eleven
            # rounds later `replace_symbol` with empty content removed the other.
            # Two legal steps, and the file lost a function nobody asked to
            # delete.
            #
            # Deleting a DUPLICATE stays allowed, because that is the entire
            # reason empty content means delete. Deleting the LAST definition is
            # refused: `patch_file` can still do it deliberately, which is the
            # right amount of friction for removing a function outright.
            erased = _symbols_erased(original, body, suffix)
            if found.name.rsplit(".", 1)[-1] in erased:
                remaining = len(find_symbols(original, suffix, found.name)) - 1
                return ToolResult(
                    False,
                    f"NOT APPLIED - {path} is unchanged. That would delete the LAST "
                    f"definition of {found.name}, leaving {remaining} behind.{chr(10) * 2}"
                    "Empty content removes a DUPLICATE. If you meant to remove "
                    f"{found.name} from the file entirely, use patch_file on its "
                    "exact text - deleting the only copy of something should be "
                    "deliberate.",
                    {"filepath": path, "symbol": found.name, "would_erase": erased},
                )
        # Not asked when the symbol is being deleted: losing its members is the
        # point, and this guard exists for the opposite mistake - a replacement
        # that silently drops members it was supposed to keep.
        lost = [] if deleting else _members_lost(original, body, suffix, found)
        if lost:
            # Live 2026-08-20, qwen2.5-coder:3b replaced the whole 314-line
            # `Player` class with 45 lines: the file still parsed, so the parse
            # check passed, and 22 methods plus the `export` keyword were simply
            # gone. Parsing is not the same as keeping the code.
            #
            # The right move was never to replace the class - it was to replace
            # the ONE method. Saying which members would vanish makes that
            # obvious in the same round, instead of after a test run that fails
            # for a reason the model will not connect to this edit.
            missing = ", ".join(lost[:8]) + (f" (+{len(lost) - 8} more)" if len(lost) > 8 else "")
            return ToolResult(
                False,
                f"NOT APPLIED - {path} is unchanged. Replacing {found.name} with that "
                f"content would delete {len(lost)} member(s) it still contains: "
                f"{missing}." + chr(10) * 2
                + f"If you meant to change one part of {found.name}, call replace_symbol "
                "on that member by name, or patch_file for a smaller change. To replace "
                f"the whole of {found.name}, include every member it has.",
                {"filepath": path, "symbol": found.name, "would_delete": lost},
            )
        broke = _breaks_a_working_file(original, body, suffix)
        if broke:
            return ToolResult(
                False,
                f"NOT APPLIED - {path} is unchanged. Replacing {found.name} with that "
                f"content would stop the file parsing: {broke}" + chr(10) * 2
                + "Check the new source is complete and closes every block, then send it "
                "again.",
                {"filepath": path, "symbol": found.name, "would_break": broke},
            )
        try:
            target.write_text(body, encoding="utf-8", newline="")
        except OSError as exc:
            return ToolResult(False, f"Could not write {path}: {exc}", {"filepath": path})

        note = " (re-indented to match the original)" if reindented else ""
        was = found.lines
        now = 0 if deleting else replacement.count(chr(10)) + 1
        message = (
            f"Deleted {found.name} from {path}: {was} line(s) removed "
            f"(was lines {found.start}-{found.end})."
            if deleting
            else f"Replaced {found.name} in {path}{note}: lines {found.start}-{found.end}, "
            f"{was} line(s) -> {now}."
        )
        result = ToolResult(
            True,
            message,
            {
                "filepath": path,
                "resolved_filepath": path,
                "symbol": found.name,
                "start_line": found.start,
                "end_line": found.end,
                "reindented": reindented,
            },
        )
        return self._with_diff(arguments, original, result)

    def _read_symbol(self, arguments: dict[str, Any]) -> ToolResult:
        """One function or class, exactly - the follow-up an outline earns.

        The line range is computed from the same parse that produced the
        outline, so it cannot drift from what the model was shown. That is the
        whole reason this exists rather than leaving the model to pass
        start_line and end_line it read off a listing: a guessed range that is
        two lines short produces a patch that will not apply, and the model
        cannot tell the difference from a wrong `old_string`.
        """
        path = str(arguments.get("filepath") or "").strip()
        wanted = str(arguments.get("symbol") or arguments.get("name") or "").strip()
        if not path or not wanted:
            return ToolResult(False, "read_symbol needs a filepath and a symbol.", {})
        try:
            target = self.tools.sandbox.validate(path)
        except Exception as exc:  # noqa: BLE001 - the sandbox owns this refusal
            return ToolResult(False, str(exc), {"filepath": path})
        if not target.is_file():
            return ToolResult(
                False,
                f"Not a file: {path}. Use find_files to locate it.",
                {"filepath": path},
            )
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(False, f"Could not read {path}: {exc}", {"filepath": path})
        suffix = target.suffix
        matches = find_symbols(text, suffix, wanted)
        if len(matches) > 1:
            return self._every_definition_of(path, text, wanted, matches)
        found = matches[0] if matches else None
        if found is None:
            # Say what IS there. A bare "not found" costs a round and teaches
            # nothing; the roster is the answer to the question behind the
            # question, and it is already computed.
            names = [symbol.name for symbol in outline(text, suffix)]
            if not names:
                return ToolResult(
                    False,
                    f"Nothing in {path} can be outlined, so there is no symbol to "
                    "fetch. Read it with read_file and start_line/end_line.",
                    {"filepath": path, "symbol": wanted},
                )
            return ToolResult(
                False,
                _no_such_symbol(path, wanted, names),
                {"filepath": path, "symbol": wanted, "available": names[:60]},
            )
        nested = [
            symbol
            for symbol in outline(text, suffix)
            if symbol is not found
            and symbol.start > found.start
            and symbol.end <= found.end
        ]
        if found.lines > OUTLINE_SYMBOL_OVER_LINES and nested:
            # A container, not a unit of work. Answer the way the file does.
            listed = chr(10).join(
                f"  L{symbol.start}-{symbol.end}  {symbol.signature}"
                + (f"  - {symbol.purpose}" if symbol.purpose else "")
                for symbol in nested
            )
            self._activity(
                f"{found.name} is {found.lines} lines; sent its outline instead of its body"
            )
            return ToolResult(
                True,
                f"{path} lines {found.start}-{found.end}, {found.signature} - "
                f"{found.lines} lines, so this is its outline rather than its body. "
                f"It contains {len(nested)} member(s):" + chr(10) + listed + chr(10) * 2
                + "Call read_symbol again for one of these by name to see its source.",
                {
                    "filepath": path,
                    "resolved_filepath": path,
                    "symbol": found.name,
                    "start_line": found.start,
                    "end_line": found.end,
                    "outlined": True,
                    "members": [symbol.name for symbol in nested],
                    "content_truncated": True,
                },
            )
        lines = text.splitlines()[found.start - 1 : found.end]
        body = chr(10).join(lines)
        # The same cap every other payload obeys. A single 900-line function is
        # rare and is exactly the case where a range read is the right call.
        capped = False
        if len(body) > MAX_READ_CHARS:
            body = body[:MAX_READ_CHARS]
            capped = True
        message = (
            f"{path} lines {found.start}-{found.end}, {found.signature}:"
            + chr(10) + body
        )
        if capped:
            message += (
                chr(10)
                + f"... [that symbol is {found.lines} lines; this is the first part. "
                f"Read the rest with read_file(start_line=..., end_line={found.end}).]"
            )
        return ToolResult(
            True,
            message,
            {
                "filepath": path,
                "resolved_filepath": path,
                "symbol": found.name,
                "start_line": found.start,
                "end_line": found.end,
                "total_lines": len(text.splitlines()),
                "content_truncated": capped,
            },
        )

    def _append_broke_it(self, target: Path, was_healthy: bool) -> str:
        """Did this append stop a file parsing that parsed a moment ago?

        Judged with the same checker the post-write verifier uses, so an append
        cannot leave behind a state the verifier would immediately call broken.
        Silent unless the file was healthy BEFORE - a file part-way through
        being built in sections is unparseable by design, and refusing to grow
        it would break the very strategy the write cap asks for.
        """
        if not was_healthy:
            return ""
        verdict = check_file(target)
        if verdict.status != VERIFY_PROBLEM:
            return ""
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        if unfinished_blocks(text, target.suffix):
            # Open blocks and nothing else: this is a first section, not damage.
            # Appending `function f() {` to a complete file makes it stop
            # parsing and that is EXPECTED - it is how a section gets built. The
            # damage this guard is for looks different: the player.js corruption
            # was perfectly brace-balanced and still nonsense, which is why the
            # question has to be "is anything OPEN?" and not "does it parse?".
            return ""
        return verdict.detail or "it no longer parses"

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
        # Net lines gained. The repeated-edit ceiling reads this to tell a file
        # being BUILT from a file being churned; see `_run_tools`.
        data["grew_by"] = len(after.splitlines()) - len(before.splitlines())
        return ToolResult(
            result.ok,
            f"{result.message}\n\nWhat changed:\n" + "\n".join(shown),
            data,
        )

    def _every_definition_of(
        self, path: str, text: str, wanted: str, matches: list[Any]
    ) -> ToolResult:
        """All of them, when a name is declared more than once.

        Returning only the first is what sent the model hunting. Live
        2026-08-21 a 582-line `main.js` held four functions defined twice; asked
        to remove the duplicates, the model fetched one definition, got no hint
        that another existed, and spent the rest of the turn re-reading
        overlapping line ranges looking for it.

        Duplicated definitions are usually the BUG - the later one silently
        wins - so this says so rather than leaving the model to notice.
        """
        lines = text.splitlines()
        blocks = []
        for index, symbol in enumerate(matches, start=1):
            body = chr(10).join(lines[symbol.start - 1 : symbol.end])
            blocks.append(
                f"--- definition {index} of {len(matches)}: "
                f"lines {symbol.start}-{symbol.end} ---{chr(10)}{body}"
            )
        listed = ", ".join(f"{symbol.start}-{symbol.end}" for symbol in matches)
        self._activity(f"{wanted} is defined {len(matches)} times in {path}; sent all of them")
        return ToolResult(
            True,
            f"{wanted} is defined {len(matches)} times in {path} (lines {listed}). "
            "In most languages the LAST one wins, so the earlier definitions are "
            f"dead - which is usually the bug.{chr(10) * 2}"
            + (chr(10) * 2).join(blocks)
            + f"{chr(10) * 2}To remove one, call replace_symbol on {path} with "
            "empty content - it deletes the first definition. To change one, "
            "patch_file against the exact text above.",
            {
                "filepath": path,
                "resolved_filepath": path,
                "symbol": wanted,
                "definitions": [
                    {"start_line": s.start, "end_line": s.end} for s in matches
                ],
                "duplicate_definitions": len(matches),
            },
        )

    def _already_sent_this_range(self, arguments: dict[str, Any]) -> ToolResult | None:
        """Answer a re-read from what the model already has, or ``None``.

        The repeated-read guard compares SIGNATURES, so it caught `105-210`
        asked twice and missed `100-215` against `100-250`. Across four live
        runs on qwen3.5:9b that gap cost four to six rounds each, re-fetching
        regions the model had been given minutes earlier at slightly different
        numbers.

        Returns a note rather than the content. Re-sending it would cost the
        same tokens the re-read was going to cost, which is the whole thing
        being saved - and the note names the exact ranges already sent so the
        model can find them in its own history rather than guess.
        """
        path = str(arguments.get("filepath") or "").strip()
        start, end = arguments.get("start_line"), arguments.get("end_line")
        if not path or not isinstance(start, int) or not isinstance(end, int):
            return None
        seen = self._ranges_sent.get(path.lower())
        if not seen:
            return None
        covered = _covered_fraction((start, end), seen)
        if covered < RANGE_ALREADY_SEEN_FRACTION:
            return None
        key = path.lower()
        self._blocked_reads[key] = self._blocked_reads.get(key, 0) + 1
        if self._blocked_reads[key] >= BLOCKED_READS_BEFORE_WITHDRAWING:
            # The note has been made and ignored often enough. Take the tool.
            self._read_withdrawn.add(key)
        listed = ", ".join(f"{low}-{high}" for low, high in sorted(seen)[:6])
        self._activity(f"{path} lines {start}-{end} were already sent; did not re-read")
        return ToolResult(
            True,
            f"You already have lines {start}-{end} of {path} - "
            f"{round(covered * 100)}% of that range was in a read earlier this "
            f"turn. Ranges already sent: {listed}.{chr(10) * 2}"
            "Use what you have. If you need a part you have NOT seen, ask for "
            "that range specifically; if you need to change something, make the "
            "edit now.",
            {
                "filepath": path,
                "resolved_filepath": path,
                "start_line": start,
                "end_line": end,
                "already_sent": True,
                "content": "",
            },
        )

    def _record_range_sent(self, arguments: dict[str, Any], result: ToolResult) -> None:
        """Remember a range the model has now been given."""
        data = result.data if isinstance(result.data, dict) else {}
        if not result.ok or data.get("already_sent"):
            return
        path = str(data.get("resolved_filepath") or arguments.get("filepath") or "").strip()
        start, end = data.get("start_line"), data.get("end_line")
        if not path or not isinstance(start, int) or not isinstance(end, int):
            return
        self._ranges_sent.setdefault(path.lower(), []).append((start, end))

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

    def _record_evidence(
        self, user_input: str, result: SimpleChatResult, duration: float
    ) -> None:
        """Distil what this turn DID into one note, from what already happened.

        Nothing new is captured. `.shamsu/runs/`, `.shamsu/mutations/` and the
        turn log already hold every fact here; what was missing was something
        to summarise them, which is exactly what smallcode's evidence layer
        does ("distinct from manual memory... auto-derived at task end").

        Written only when the turn CHANGED something or FAILED at something. A
        question that was answered leaves no trace worth keeping, and a store
        that fills with "the user said hi" is one nobody can recall from.

        Type `context` and tag `evidence`, so `render_memory`'s existing
        relevance scoring loads it only when it bears on the request - a growing
        memory must not become a growing tax on the window.
        """
        changed = list(result.changed_files)
        failures = list(dict.fromkeys(self._turn_failures))
        if not changed and not failures:
            return
        task = " ".join((user_input or "").split())
        lines = [f"Task: {task[:200]}"]
        if changed:
            lines.append("Files changed: " + ", ".join(changed[:8]))
        if failures:
            lines.append("What failed:")
            lines.extend(f"- {name}: {error[:160]}" for name, error in failures[:5])
        if result.stopped:
            lines.append("The turn stopped before finishing.")
        lines.append(f"Took {duration:.0f}s over {result.rounds} rounds.")
        from shamsu.agents.simple_memory import remember

        remember(
            self.workspace,
            "context",
            _evidence_title(task, changed),
            chr(10).join(lines),
            ["evidence"],
        )

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

        Four outcomes now. `skipped` is the escape - a `.md` file nobody can
        parse is reported as unchecked, never as a problem to repair - and
        `unfinished` is the one chunked writing needs. Once a large file is
        MEANT to arrive in sections, every intermediate section legitimately
        fails a bracket count, and reporting that as a fault would send the
        model repairing a file that is simply not finished yet. An open block on
        a file this turn just wrote is progress, and saying so turns the
        verifier into the signal that tells the model what to append next.
        """
        problems: list[str] = []
        checked: list[str] = []
        skipped: list[str] = []
        unfinished: list[str] = []
        for relative in dict.fromkeys(written):
            path = (self.workspace / relative).resolve()
            if not path.is_file():
                problems.append(f"{relative}: file was not created")
                continue
            # Asked FIRST, and of every file rather than only of one a checker
            # already complained about. The structural scan is deliberately
            # quiet about an unterminated single-line string - far more often an
            # apostrophe in prose than a fault - so a `.js` file ending
            # `const label = "sco` passes it, and would have been reported as
            # having no syntax errors. That is the false pass this whole module
            # exists to prevent.
            cut = self._cut_off_on_disk(path, relative)
            if cut:
                problems.append(cut)
                self._unfinished.pop(relative, None)
                continue
            verdict = check_file(path)
            if verdict.status == VERIFY_PROBLEM:
                # Only a file the last write ADDED to may claim to be
                # unfinished. Without that gate this branch swallowed every
                # patch-induced syntax error in the codebase: a `}` eaten by a
                # patch leaves open blocks and nothing else wrong, which is
                # byte-for-byte what the first section of a chunked write looks
                # like, so `node --check: SyntaxError` was replaced with "that
                # is expected part-way through" and the whole report came back
                # `ok: true`. The model was then asked to fix a file it had
                # just been told was fine.
                grew = self._last_write_grew.get(relative.lower(), False)
                still_open = self._still_being_built(path) if grew else ""
                if still_open:
                    unfinished.append(f"{relative}: {still_open}")
                    self._unfinished[relative] = verdict.detail or still_open
                    continue
                problems.append(f"{relative}: {verdict.detail}{self._mid_build_note(relative)}")
                self._unfinished.pop(relative, None)
            elif verdict.status == VERIFY_SKIPPED:
                skipped.append(f"{relative} ({verdict.detail})")
                self._unfinished.pop(relative, None)
            else:
                checked.append(relative)
                self._unfinished.pop(relative, None)
        if problems:
            return json.dumps(
                {"ok": False, "message": "Problems in the files just written.",
                 "data": {"problems": problems, "checked": checked,
                          "skipped": skipped, "unfinished": unfinished}},
                ensure_ascii=True,
            )
        if not checked and not skipped and not unfinished:
            return ""
        parts: list[str] = []
        if checked:
            parts.append(f"Checked {', '.join(checked)}: no syntax errors.")
        if unfinished:
            parts.append("Still being built: " + "; ".join(unfinished) + ".")
        if skipped:
            parts.append(f"NOT checked: {', '.join(skipped)}.")
        if not checked and not unfinished:
            parts.insert(0, "Nothing was syntax-checked.")
        return json.dumps(
            {"ok": True, "message": " ".join(parts),
             "data": {"checked": checked, "skipped": skipped,
                      "unfinished": unfinished}},
            ensure_ascii=True,
        )

    def _settle_unfinished(self) -> None:
        """A file still open when the model walks away is not "in progress".

        The other half of reporting open blocks as progress. Mid-turn, "3 blocks
        still open, continue with append_file" is the right thing to say and
        carries no verdict - the file has not been finished yet, so there is
        nothing to pass or fail. At turn end that stops being true: the model
        declared itself done, and a file it left mid-block is broken.

        Without this the run outcome would read a half-written file as a
        success, which is the exact defect `_record_verification` was written
        for pointed the other way.
        """
        if self.action_ledger is None or not self._unfinished:
            return
        for relative, detail in list(self._unfinished.items()):
            path = (self.workspace / relative).resolve()
            if not path.is_file():
                continue
            if self._still_being_built(path):
                self._log_verdict(
                    False,
                    relative,
                    f"{relative}: {detail} - and the turn ended with it still open",
                )
        self._unfinished.clear()

    def _still_being_built(self, path: Path) -> str:
        """"3 blocks still open. Continue with append_file." - or ``""``.

        The tension chunking creates, and the reason it has to be resolved here
        rather than by not verifying: `append_file` is in `WRITING_TOOLS`
        precisely so a file built in pieces IS checked after every piece, which
        was added to kill stale verdicts. Both are right, and what reconciles
        them is that an unclosed block means two different things depending on
        whether the file is finished.

        Only for a file whose ONLY complaint is open blocks. A closer with
        nothing open, a mismatched pair, or a truncation signature is wrong at
        every stage of writing and stays a problem.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        # Python and JSON have real parsers and no block delimiters to count,
        # so `unfinished_blocks` is empty for them by construction: an
        # unfinished Python section usually compiles, and one that does not is a
        # mistake rather than a missing closer.
        still_open = unfinished_blocks(text, path.suffix)
        if not still_open:
            return ""
        # The innermost, for the same reason `bracket_problem` reports it: this
        # is the block the next section has to continue, and it is the one
        # nearest where the text stops. The outermost is usually line 1.
        opener, opened_at = still_open[-1]
        count = len(still_open)
        return (
            f"{count} block(s) still open; the innermost is a {opener} opened on line "
            f"{opened_at}. That is expected part-way through - continue with append_file"
        )

    def _mid_build_note(self, relative: str) -> str:
        """Context for a real failure on a file this turn has been building.

        The one place the exemption gate above can raise a false alarm: a patch
        into a file that genuinely is half-written. The error is real and is
        reported as one - that is the whole point of the gate - but a model
        told only "unexpected end of input" may close the open blocks and end
        the file two hundred lines early. So it gets both facts and chooses.
        """
        if relative.lower() not in self._built_up:
            return ""
        return (
            " (you have been building this file in sections this turn - if it is not "
            "finished, continue the next section rather than closing it early)"
        )

    def _cut_off_on_disk(self, path: Path, relative: str) -> str:
        """A file that STOPS PART-WAY THROUGH, and the exact call that fixes it.

        The safety net, not the primary path: the pre-write gate refuses cut-off
        content before it reaches disk, so by the time a file gets here it has
        usually arrived through a patch or from outside this turn. It exists
        because the legacy loop had the better answer and simple mode never
        carried it over - continue-from-the-tail keeps the good 80% and asks
        only for what is missing, where "send it again in sections" throws away
        a file the model no longer holds.

        Language-agnostic, unlike the legacy version, which read `.py` only.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        why = truncation_signature(text, suffix=path.suffix)
        if not why:
            return ""
        tail = chr(10).join(text.splitlines()[-12:])
        return (
            f"{relative}: it STOPS PART-WAY THROUGH - {why}. Do not re-send the whole "
            f"file (that is what truncated). Call append_file on {relative} with ONLY "
            "the missing remainder, continuing exactly from where this tail ends, and "
            "close every open string, bracket and block:" + chr(10)
            + "--- current end of file ---" + chr(10)
            + tail + chr(10)
            + "--- end ---"
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
        truncated: bool = False,
    ) -> SimpleChatResult:
        self._settle_unfinished()
        self.state.append_assistant(message)
        return SimpleChatResult(
            final=message,
            rounds=rounds,
            tool_calls=tool_calls,
            changed_files=tuple(dict.fromkeys(changed)),
            stopped=True,
            error=error,
            truncated=truncated,
        )

    def _publish(self, kind: str, text: str = "", **data: Any) -> None:
        """Put one event on the turn stream. Never raises, never blocks a turn."""
        if not self.emit:
            return
        self._event_seq += 1
        event = TurnEvent(
            seq=self._event_seq,
            kind=kind,
            text=text,
            data=data,
            turn_id=self.turn_id,
            session_id=str(getattr(self.session_logger, "session_id", "") or ""),
            workspace=str(self.workspace),
            source=self.source,
        )
        try:
            self.emit(event)
        except Exception:  # noqa: BLE001 - a renderer must never fail a turn
            pass

    def _activity(self, message: str, *, kind: str = "activity", **data: Any) -> None:
        """An append-only line. `kind` lets a caller say what KIND of line it is.

        The tool-call line goes out as `tool.call` rather than `activity` so a
        renderer can collapse it, diff it or hang a button off it - but it is
        still the same string, emitted once, so the CLI's list and the phone's
        list stay equal.
        """
        if self.on_activity:
            try:
                self.on_activity(message)
            except Exception:
                pass
        self._publish(kind, message, **data)

    def _status(self, message: str) -> None:
        """A transient tick. Carries the meter, because the meter is only ever
        interesting WHILE something is running.

        `ctx` and `round` ride on every status event rather than being fetched
        by the renderer: a renderer that reaches back into the loop is a
        renderer that cannot run on a phone, and these two numbers are the
        whole reason anyone watches the spinner. Both already existed and were
        displayed nowhere - you found out you had filled the window by watching
        the run degrade.
        """
        if self.on_status:
            try:
                self.on_status(message)
            except Exception:
                pass
        self._publish("status", message, **self._meter_fields())

    def _watch_approvals(self) -> None:
        """Announce an approval on the turn stream, around the real prompt.

        The loop never emitted `approval` at all - the question went straight
        to its own Console from deep inside a tool, so every surface watching
        the turn saw a gap where a human was being asked something. On the
        terminal that is why the spinner and the prompt fight: nothing told the
        display to stand down.

        Wrapping rather than threading a callback down through the registry:
        the decision is made by whatever function the tools were handed, and
        wrapping it is the only place that sees both the question and the
        answer without every tool having to cooperate.
        """
        original = getattr(self.tools, "approval_func", None)
        if not callable(original) or getattr(original, "_shamsu_watched", False):
            return

        def watched(request: Any) -> bool:
            action = str(getattr(request, "action_type", "") or "action")
            targets = list(getattr(request, "target_paths", None) or [])
            self._publish(
                "approval",
                f"approval needed: {action}",
                phase="requested",
                action_type=action,
                target=targets[0] if targets else "",
                risk=str(getattr(request, "risk_level", "") or ""),
                description=str(getattr(request, "description", "") or ""),
                preview=str(getattr(request, "preview", "") or ""),
            )
            approved = False
            try:
                approved = bool(original(request))
                return approved
            finally:
                self._publish(
                    "approval",
                    f"approval {'granted' if approved else 'denied'}: {action}",
                    phase="resolved",
                    action_type=action,
                    target=targets[0] if targets else "",
                    approved=approved,
                )

        watched._shamsu_watched = True  # type: ignore[attr-defined]
        try:
            self.tools.approval_func = watched
        except Exception:
            pass

    def _meter_fields(self) -> dict[str, Any]:
        """The live numbers, best-effort. Never the reason a tick is lost."""
        fields: dict[str, Any] = {
            "round": self._round_index + 1,
            "max_rounds": self.max_rounds,
        }
        try:
            if SESSION_COUNTERS.last_window and SESSION_COUNTERS.last_prompt_tokens:
                fields["ctx_pct"] = SESSION_COUNTERS.pct
                fields["ctx_text"] = SESSION_COUNTERS.meter()
        except Exception:
            pass
        return fields

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


#: An argument value longer than this is summarised rather than published. A
#: `write_file` carries a whole file, and `activity.jsonl` is UI telemetry - it
#: must not quietly become a second, unredacted copy of the workspace.
MAX_PUBLISHED_ARGUMENT_CHARS = 200


def _publishable_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    published: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if isinstance(value, str) and len(value) > MAX_PUBLISHED_ARGUMENT_CHARS:
            published[key] = f"<{len(value)} chars>"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            published[key] = value
        else:
            published[key] = str(value)[:MAX_PUBLISHED_ARGUMENT_CHARS]
    return published


def _turn_verdict(
    elapsed: float,
    changed: tuple[str, ...] | list[str],
    *,
    stopped: bool,
    failures: int = 0,
    truncated: bool = False,
) -> str:
    """The one line a surface shows where the live footer used to tick.

    Says what the turn COST as well as how long it took. `done in 21m52s` was
    the whole account of a turn that failed four tool calls, wrote nothing and
    was cut off mid-answer - every one of those numbers already existed and
    none of them reached the line the user actually reads.

    ASCII only, deliberately: this string reaches a Windows console and a
    Telegram HTML body, and a decorative separator has crashed a cp1252
    terminal here before.
    """
    seconds = max(0, int(elapsed))
    spent = f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"
    verdict = "stopped after" if stopped else "done in"
    parts = [f"{verdict} {spent}"]
    count = len(dict.fromkeys(changed))
    if count:
        parts.append(f"{count} file{'s' if count != 1 else ''} changed")
    elif failures or truncated:
        # Only worth saying alongside something that went wrong. On a plain
        # question-and-answer turn "no files changed" is not news.
        parts.append("no files changed")
    if failures:
        parts.append(f"{failures} tool call{'s' if failures != 1 else ''} failed")
    if truncated:
        parts.append("answer cut off")
    return " - ".join(parts)


def _read_argument_summary(arguments: dict[str, Any]) -> str:
    """`_argument_summary`, plus whatever NARROWS the read.

    The repeated-read warning keys on this, and `_argument_summary` returns the
    filepath alone - so `read_file(app.py, 1-60)` and `read_file(app.py, 61-120)`
    were the same signature, and the THIRD section of a file read in pieces was
    answered with "you have already called this and the result has not changed.
    Use the result you already have."

    That is false, and it is worse than merely false: reading a large file in
    ranges is the strategy the outline tells the model to use, so the guard
    fired on precisely the behaviour the read path now asks for. The partial-read
    tracker on the other side of this file (`_seen_ranges`) has always
    accumulated ranges correctly - only this counter disagreed.
    """
    base = _argument_summary(arguments)
    narrowing = [
        f"{key}={arguments[key]}"
        for key in ("start_line", "end_line", "symbol")
        if arguments.get(key) not in (None, "")
    ]
    return f"{base} {' '.join(narrowing)}".strip() if narrowing else base


@lru_cache(maxsize=8)
def _skill_catalog(workspace: Path) -> Any:
    """The skill catalogue for *workspace*, discovered once per run.

    Cached because discovery walks three directories and the answer cannot
    change inside a turn; keyed by workspace so two open projects do not share
    one roster.
    """
    try:
        from shamsu.skills.loader import discover_skills

        return discover_skills(workspace)
    except Exception:  # noqa: BLE001 - a bad skill must not end a turn
        from shamsu.skills.types import SkillCatalog

        return SkillCatalog()


def skill_index(workspace: Path) -> str:
    """One line per skill, for the prompt. ``""`` when there are none.

    smallcode's arrangement, and the reason it is an INDEX rather than the
    documents themselves: a model that cannot see a skill exists will never call
    `use_skill` for it (their issue #58 again), while pasting every skill body
    into the prompt would spend the window on instructions for tasks this turn
    is not doing.
    """
    catalog = _skill_catalog(workspace)
    skills = getattr(catalog, "sorted_skills", list)()
    if not skills:
        return ""
    lines = ["Skills you can load with use_skill, when one fits the job:"]
    # Indented rather than bulleted: the prompt deliberately carries no bullet
    # list, because the legacy path's 49-bullet wall is what this mode replaced,
    # and a roster is data rather than rules.
    lines += [f"  {skill.name}: {_gist(skill.description)}" for skill in skills[:12]]
    return chr(10).join(lines)


def _gist(description: str, limit: int = 58) -> str:
    """Enough of a description to choose by, and no more.

    This index is paid for on EVERY turn, and SHAMSU's skill descriptions were
    written for a settings screen - one runs to 140 characters listing four
    database engines. Full descriptions cost ~200 tokens of window per turn to
    answer a question the model asks once. The name carries most of the signal;
    this is the tiebreaker.
    """
    text = " ".join((description or "").split())
    for separator in (" - ", ". "):
        head = text.split(separator, 1)[0]
        if 12 <= len(head) < len(text):
            text = head
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + "..."


def _no_such_symbol(path: str, wanted: str, names: list[str]) -> str:
    """Say what IS there - nearest first, and never as an undifferentiated wall.

    Live 2026-08-20 on qwen2.5-coder:3b, the model asked for `initializePlayer`,
    `updatePlayerState` and `renderPlayer` in three consecutive calls. None
    existed. Each time it was handed all thirty symbols in the file in one line,
    which is a list to skim rather than an answer to act on, and it invented a
    fourth name rather than picking from it.

    A ranked suggestion is the answer to the question actually being asked -
    *what is this thing called here?* - and the full roster stays available
    underneath it for the case where nothing is close.
    """
    import difflib

    if not names:
        return (
            f"{path} has no symbols this can parse, so there is nothing to fetch by "
            "name. Read it with read_file and start_line/end_line."
        )
    bare = wanted.rsplit(".", 1)[-1]
    close = difflib.get_close_matches(bare, [n.rsplit(".", 1)[-1] for n in names], n=3, cutoff=0.6)
    message = f"{path} has no symbol called {wanted!r}."
    if close:
        named = [n for n in names if n.rsplit(".", 1)[-1] in close]
        message += " Did you mean " + ", ".join(named[:3]) + "?"
    listed = ", ".join(names[:24])
    more = f" (+{len(names) - 24} more)" if len(names) > 24 else ""
    return message + f" It defines: {listed}{more}."


def _match_indentation(original_line: str, content: str) -> tuple[str, bool]:
    """Indent *content* to the column the symbol it replaces sat at.

    A method inside a class starts indented, and a small model asked for "the
    new render" hands back a function at column zero far more often than not.
    Without this the replacement is syntactically wrong in a way the model did
    not intend and cannot see; with it, the obvious answer is also the correct
    one.

    Only when the original was indented AND the replacement is not - never
    re-indent content the model already positioned, and never de-indent.
    Reported in the result rather than done silently.
    """
    indent = original_line[: len(original_line) - len(original_line.lstrip())]
    if not indent:
        return content, False
    first = content.split(chr(10), 1)[0]
    if first[:1] in (" ", chr(9)):
        return content, False
    shifted = chr(10).join(
        (indent + line) if line.strip() else line for line in content.split(chr(10))
    )
    return shifted, True


def _symbols_erased(original: str, updated: str, suffix: str) -> list[str]:
    """Symbols the edit removed the LAST definition of.

    `replace_symbol` has refused to silently drop members since 2026-08-20
    (`_members_lost`). `patch_file` had no equivalent, and a patch can delete
    any number of lines - so the wider, blunter tool was the unguarded one, and
    a deleting patch that leaves the file parsing passes every check there is.

    Written after a scare that turned out to be a measurement error rather than
    a real incident: a `grep "^function"` over a 582-line file missed the
    definitions that were indented, which made a correctly-removed duplicate
    look like a deleted function. Kept anyway, because the hole it covers is
    real whether or not it has been fallen into - `_members_lost` exists for
    exactly this risk on the narrower tool.

    The rule is deliberately narrow, because deleting IS often the task: going
    from two definitions to one is exactly what removing a duplicate looks like
    and must stay allowed. Only going from one to NONE is caught - the edit
    removed the last trace of something the file used to have. Counting is by
    parse rather than by regex, which is the whole lesson above.
    """
    from shamsu.agents.simple_outline import outline as _outline

    def _names(text: str) -> set[str]:
        return {symbol.name.rsplit(".", 1)[-1] for symbol in _outline(text, suffix)}

    try:
        before, after = _names(original), _names(updated)
    except Exception:  # noqa: BLE001 - an unparseable side is not evidence of loss
        return []
    return sorted(before - after)


def _members_lost(original: str, updated: str, suffix: str, replaced: Any) -> list[str]:
    """Members of a container symbol that the replacement silently drops.

    Exact rather than a size ratio: the question is not "is this much smaller?"
    but "is something that was here now gone?", and the names answer it in a
    form the model can act on.

    Only for a symbol that HAS members. Replacing one function with a shorter
    function is ordinary work; replacing a class with a sketch of a class is the
    loss this catches.
    """
    from shamsu.agents.simple_outline import outline as _outline

    before = {
        symbol.name.rsplit(".", 1)[-1]
        for symbol in _outline(original, suffix)
        if symbol.start > replaced.start and symbol.end <= replaced.end
    }
    if not before:
        return []
    after = {symbol.name.rsplit(".", 1)[-1] for symbol in _outline(updated, suffix)}
    return sorted(before - after)


def _breaks_a_working_file(original: str, updated: str, suffix: str) -> str:
    """Would this edit stop a file parsing that parsed before? The reason, or "".

    The check `patch_file` cannot make and this one can. A patch carries a
    fragment, and asking whether a fragment closes every block it opens is the
    question that produced three false refusals on a legitimate JSDoc comment.
    `replace_symbol` produces a COMPLETE FILE, so the honest whole-file question
    is available: did it parse before, and does it still?

    Silent when the file was already broken - refusing to repair an unparseable
    file would lock the model out of exactly the fix it was asked for.
    """
    from shamsu.agents.simple_verify import PROBLEM, check_text

    before = check_text(original, suffix)
    if before.status == PROBLEM:
        return ""
    after = check_text(updated, suffix)
    return after.detail or "it no longer parses" if after.status == PROBLEM else ""


def _strip_line_numbers(arguments: dict[str, Any]) -> dict[str, Any]:
    """Undo the read gutter when the model pastes it back into a patch.

    Numbering every line makes a read far more useful and creates exactly one
    hazard: `old_string` copied verbatim out of the result carries `  12| ` in
    front of each line and can never match the file. Stripping it here means the
    model can copy what it was shown - which is precisely what it was told to
    do - and be right.

    Only when EVERY non-empty line carries a gutter. A snippet where one line
    happens to start with a number and a pipe is far more likely to be real
    code (a markdown table, a bit of ASCII art) than a half-copied read.
    """
    cleaned = dict(arguments)
    for key in ("old_string", "new_string"):
        value = arguments.get(key)
        if not isinstance(value, str) or not value:
            continue
        lines = [line for line in value.split(chr(10)) if line.strip()]
        if not lines or not all(_NUMBERED_LINE.match(line) for line in lines):
            continue
        cleaned[key] = _NUMBERED_LINE.sub("", value)
    return cleaned


def _diff_of(result: ToolResult) -> str:
    """The unified diff a write produced, or "" - for a terminal to colourize.

    `_with_diff` already builds one and appends it to the message under a
    "What changed:" heading, so this recovers it rather than diffing again: the
    file has moved on by now, and a second diff would be of the wrong pair.
    """
    message = str(getattr(result, "message", "") or "")
    marker = "What changed:"
    if marker not in message:
        return ""
    body = message.split(marker, 1)[1].strip("\n")
    return body if body.strip() else ""


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




def _extended_the_file(result: ToolResult) -> bool:
    """Did this write ADD to the file rather than rework it?

    The one signal that separates building from churning, and the reason the
    repeated-edit ceiling can stay strict about the second while a chunked write
    grows a file over a dozen calls.

    Conservative: only a write whose diff was computed - `_with_diff` records
    this - and only one with a net gain in lines. A rewrite that shuffles a file
    without growing it is exactly the blind repair the ceiling exists for.
    """
    data = result.data if isinstance(result.data, dict) else {}
    try:
        return int(data.get("grew_by") or 0) > 0
    except (TypeError, ValueError):
        return False


def _added_to_the_file(name: str, result: ToolResult) -> bool:
    """Did this write ADD to the file, rather than rework what was there?

    The question the "still being built" exemption turns on. An open block on a
    file that just GREW is the first half of a section; the same open block on a
    file a patch just shrank is a brace that patch ate.

    Three shapes, one meaning:

    * ``append_file`` - it can only ever add to the end.
    * a write that CREATED the file. The skeleton of a chunked build has open
      blocks by design, and there is nothing it could have broken.
    * any write whose diff shows a net gain in lines. This is the shape rather
      than the tool, and it has to be: told to build a 1,500-line file,
      qwen2.5:3b chose `write_file` and re-sent the growing file each time.
    """
    if not result.ok:
        return False
    if name == "append_file":
        return True
    data = result.data if isinstance(result.data, dict) else {}
    if data.get("created"):
        return True
    return _extended_the_file(result)


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


#: How much of a NEWLY REQUESTED range must already have been sent before the
#: read is answered from what the model has rather than re-read.
#:
#: Measured from four live runs on qwen3.5:9b, where the same file was read
#: eleven to thirteen times in one turn at overlapping ranges:
#:
#:     t1:  105-210, 105-210, 100-215, 100-250, 105-210, 105-210
#:     t4:  530-610, 530-610, 520-610, 528-590, 528-590, 520-610
#:
#: The existing guard only caught IDENTICAL signatures, so `105-210` twice was
#: caught and `100-215` against `100-250` was not.
#:
#: 90% rather than 100%, because a model re-asking for the same region jitters
#: its numbers by a few lines and exact containment misses that. Not lower,
#: because a genuine scroll - extending the window to see what comes next -
#: overlaps heavily with what came before and must still be answered.
RANGE_ALREADY_SEEN_FRACTION = 0.90

#: Reads answered from cache on one path before `read_file` is TAKEN AWAY for it.
#:
#: The overlap guard above works perfectly and saves the wrong resource.
#: Measured across three live runs on qwen3.5:9b it suppressed 3, 13 and 12
#: payloads - real protection for the window - and recovered zero rounds,
#: because the model asked again every time, jittering its numbers:
#:
#:     496-582, 496-570, 490-572, 496-582, 496-582, 496-580, 496-580, 498-572
#:
#: Run 3 failed for exactly that: it landed one patch, then spent its last nine
#: rounds re-requesting a region it had been handed three times.
#:
#: A note is not an action. The patch nudge works because it names a DIFFERENT
#: tool; "use what you have" names nothing. So at this point the tool goes.
#:
#: Three rather than two: a model re-checking a region once after an edit is
#: doing something reasonable, and the leak is ten-plus reads, not three.
BLOCKED_READS_BEFORE_WITHDRAWING = 3


def _covered_fraction(wanted: tuple[int, int], seen: list[tuple[int, int]]) -> float:
    """How much of *wanted* the union of *seen* already covers, 0.0 to 1.0.

    The denominator is deliberately the NEWLY REQUESTED range, not the union:
    the question is "does this request ask for anything the model has not been
    given?", and dividing by the union would let a large earlier read swallow
    every later one.
    """
    start, end = wanted
    if end < start:
        return 0.0
    requested = set(range(start, end + 1))
    if not requested:
        return 0.0
    covered = set()
    for low, high in seen:
        if high >= low:
            covered |= requested & set(range(low, high + 1))
    return len(covered) / len(requested)


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

#: What a project's manifest tells you about it, without opening it.
_PROJECT_MARKERS: tuple[tuple[str, str], ...] = (
    ("package.json", "Node"),
    ("pyproject.toml", "Python"),
    ("setup.py", "Python"),
    ("requirements.txt", "Python"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    ("pom.xml", "Java"),
    ("Gemfile", "Ruby"),
    ("composer.json", "PHP"),
    ("CMakeLists.txt", "C/C++"),
)


def _thread_has_history(session_logger: Any) -> bool:
    """Has this conversation had a turn before the one about to run?

    Decides whether the prompt may claim there are earlier messages. Read from
    the session's own metadata rather than from the transcript, because the
    prompt is built BEFORE hydration - and a wrong answer here is cheap in one
    direction only: claiming history that exists is harmless, claiming history
    that does not exist is what produced "I apologize for any confusion
    earlier" on the first message of an empty thread.

    So it fails towards True. A logger that cannot be read is assumed to have
    history, which loses one paragraph of accuracy rather than inventing a
    conversation.
    """
    if session_logger is None:
        return False
    try:
        metadata = getattr(session_logger, "metadata", None)
        if metadata is None:
            return True
        if str(getattr(metadata, "last_user_prompt", "") or "").strip():
            return True
        return int(getattr(metadata, "message_count", 0) or 0) > 0
    except Exception:  # noqa: BLE001 - a prompt section must not fail a turn
        return True


def project_brief(workspace: Path) -> str:
    """One line saying what this project IS, for the first turn and every one after.

    smallcode's `src/session/bootstrap.js`. The machinery was all here -
    `detect_test_command` reads package.json scripts and pytest layouts, the
    manifests are one `exists()` each - and nothing ever summarised it into the
    prompt. So a model opening a fresh workspace spent three to five calls
    working out what kind of project it was and how to run its tests, every
    session, and often guessed instead.

    Costs about thirty tokens and roughly a dozen file stats. Deliberately
    silent when it knows nothing: a line reading "Project: unknown" is worse
    than no line, because it looks like an answer.
    """
    parts: list[str] = []
    kinds: list[str] = []
    for marker, language in _PROJECT_MARKERS:
        try:
            if (workspace / marker).is_file() and language not in kinds:
                kinds.append(language)
        except OSError:
            continue
    if kinds:
        parts.append("/".join(kinds[:3]))
    try:
        from shamsu.agents.simple_tests import detect_test_command

        command = detect_test_command(workspace).command
        if command:
            parts.append(f"tests: `{command}`")
    except Exception:  # noqa: BLE001 - a brief must never fail a turn
        pass
    if not parts:
        return ""
    return "This project: " + ", ".join(parts) + "."


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


def _shortened_value(key: str, value: Any) -> Any:
    """One argument value, small enough to keep and safe to keep.

    Content keys are REPLACED, not trimmed. Everything else that is merely long
    is trimmed as before - a path or a command shortened mid-way is obviously
    truncated and nobody tries to run it, while a shortened `old_string` looks
    exactly like the code it came from.
    """
    if not isinstance(value, str):
        return value
    if key in CONTENT_ARGUMENT_KEYS and len(value) > MAX_OLD_ARGUMENT_CHARS:
        return ELIDED_VALUE
    if len(value) > MAX_OLD_ARGUMENT_CHARS:
        return value[:OLD_ARGUMENT_KEEP_CHARS] + " " + LEGACY_ELISION_MARKER
    return value


def _shorten_arguments(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shrink long argument VALUES in an old tool call, keeping every key.

    Once the result has come back the model does not need the whole file it
    asked to write - but it does still need to see that it wrote that file.
    Keeping the keys means the call still reads as
    `write_file(filepath=frontend/game.js)` instead of a hole in the history.

    What it must NOT do is leave behind something that still reads as content.
    See `CONTENT_ARGUMENT_KEYS`.
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
                key: _shortened_value(key, value) for key, value in arguments.items()
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


def _first_line(message: str) -> str:
    """The sentence of an error worth remembering."""
    for line in (message or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _evidence_title(task: str, changed: list[str]) -> str:
    """A title a later recall can match on.

    smallcode titles these with the user's raw prompt, typos preserved, and
    that is right: the words the user used are the words they will use again.
    The files are appended because two turns of "fix it" are otherwise
    indistinguishable.
    """
    head = (task or "a turn").strip()[:80]
    if changed:
        head += " [" + ", ".join(Path(c).name for c in changed[:3]) + "]"
    return head


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
    cap = tool_result_budget()
    if count_tokens(payload) <= cap:
        return payload
    keep = cap * 4
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
