"""Context token budgeting.

Uses a vendored Qwen3 tokenizer when the lightweight `tokenizers` package and
asset are available. Falls back to the old char/4 estimate offline or in
minimal installs.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

# Effective budget = num_ctx * safety margin (leave room for response + tool
# metadata). v2.2 targets 8GB machines, so prompts stay tighter by default.
TOTAL_BUDGET_DEFAULT = 6000
PER_HOLE_BUDGET_DEFAULT = 3500

CHARS_PER_TOKEN_ESTIMATE = 4
TOKENIZER_ASSET = Path(__file__).resolve().parent / "assets" / "qwen3-tokenizer.json"

# Context windows for each local model in SHAMSU's cookbook (runtime/models.py).
# These are reference caps; the actual limit per call is determined by num_ctx
# in the generate options.  Unknown models fall back to SAFE_FALLBACK_CTX_WINDOW.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gemma3:4b":                 131_072,
    "deepseek-r1:7b":             32_768,
    "qwen3:8b":                   32_768,
    # Native window is 262144; the real limit is VRAM, not the model - see
    # simple_chat.max_ctx(), which caps a call at 16384 (~6.2GB on an 8GB card).
    "qwen3.5:9b-q4_K_M":         262_144,
    "qwen2.5:3b-instruct":        32_768,
    "qwen2.5-coder:7b-instruct":  32_768,
    "qwen2.5-coder:3b-instruct":  32_768,
    "mistral-nemo:12b":          131_072,
    "qwen2.5-coder:14b":          32_768,
}

SAFE_FALLBACK_CTX_WINDOW = 8_192   # conservative fallback for unknown models


def _reserve_output_tokens() -> int:
    """Headroom reserved for the model's response.

    Raised from 2048 because a mutation turn now emits a WHOLE FILE: the raw write
    envelope asks for complete file content, and 2048 tokens is roughly 200 lines,
    which real modules and templates routinely exceed. When the reserve is too
    small the prompt is allowed to grow until the response has nowhere to go, and
    the model's tool call is cut off mid-payload - the `json_truncated` failure in
    llm/output.py.

    Reserving space is the lever that MAKES room; `num_predict` cannot, and for
    a long time this docstring said therefore not to send one at all. That was
    half right. Sent at exactly this value it can never shrink the reply - it is
    the same number - while it does stop a runaway generation from eating into
    the window the prompt still needs. Sent SMALLER it would indeed cause the
    truncation it is meant to prevent, so `simple_chat._call_model` derives it
    from `output_reserve()` and never from anything else.
    """
    import os

    raw = os.environ.get("SHAMSU_RESERVE_OUTPUT_TOKENS", "").strip()
    if raw.isdigit() and int(raw) >= 512:
        return int(raw)
    return 4_096


RESERVE_OUTPUT_TOKENS = _reserve_output_tokens()
SAFETY_MARGIN_TOKENS = 512         # extra buffer against off-by-one token counts


# The window a chat session asks for when the model allows it. Kept here rather
# than in simple_chat so the LLM manager can agree with it without importing an
# agent module.
DEFAULT_CHAT_CTX = 32768


def ctx_window_for_model(model_name: str) -> int:
    """Return the known context window for *model_name*, or the safe fallback."""
    return MODEL_CONTEXT_WINDOWS.get(model_name, SAFE_FALLBACK_CTX_WINDOW)


def shared_num_ctx(model_name: str) -> int:
    """The ONE context window every SHAMSU call should ask for, per model.

    Ollama reloads a model whenever `num_ctx` changes. Three call sites in
    `llm/manager.py` defaulted to 8192 while simple mode asked for 32768, so any
    background call - memory, summaries, a health check - evicted the chat
    model and vice versa. Measured 2026-08-18, the Ollama server log alternated
    `n_ctx = 8192 -> 32768 -> 8192 ...` on EVERY call: a ~6GB reload each time,
    turning 5-15s replies into 74-107s.

    Capped per model, so a small model is never asked for more than it has.
    `SHAMSU_CHAT_MAX_CTX` overrides the ceiling.
    """
    raw = os.environ.get("SHAMSU_CHAT_MAX_CTX", "").strip()
    ceiling = int(raw) if raw.isdigit() and int(raw) >= 4096 else DEFAULT_CHAT_CTX
    return min(ctx_window_for_model(model_name), ceiling)


def count_tokens(text: str) -> int:
    """Count tokens with the vendored tokenizer, falling back to char/4."""
    tokenizer = _load_tokenizer()
    if tokenizer is not None:
        return len(tokenizer.encode(text).ids)
    return max(len(text) // CHARS_PER_TOKEN_ESTIMATE, 0)


# What the chat template adds per message on top of its content: role markers
# and the special tokens that open and close a turn. Measured against Ollama's
# `prompt_eval_count` 2026-08-19, qwen3:8b:
#
#     1 message : ours  50   ollama  60    (+10)
#     3 messages: ours 152   ollama 170    (+6/msg)
#     6 messages: ours 305   ollama 332    (+4.5/msg)
#
# Eight is the middle of that range. It matters at scale, not per message: a
# 130-message session carries ~1,000 tokens of pure envelope that used to be
# counted as zero.
PER_MESSAGE_OVERHEAD = 8

# How far the calibration factor may move the budget. The factor corrects a
# structural estimate against ground truth, so a value far from 1.0 means
# something else is wrong - and trusting it blindly would either overflow the
# window (too low) or throw away most of the conversation (too high).
CALIBRATION_MIN = 0.8
CALIBRATION_MAX = 1.6


def _message_field(message: Any, key: str) -> Any:
    """Read *key* off a message, whether it is a mapping or an object.

    Both shapes are live: `ChatState` holds `ChatMessage` objects, `_call_model`
    builds plain dicts, and the tests script dicts. A counter that understands
    only one of them silently returns zero for the other, which is the exact
    class of bug this module exists to end.
    """
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)


def message_tokens(
    message: Any,
    token_counter: Callable[[str], int] | None = None,
    *,
    overhead: int = PER_MESSAGE_OVERHEAD,
) -> int:
    """Everything one message costs in the prompt - not just its text.

    `tool_calls` used to cost ZERO. An assistant turn whose content is empty but
    whose `write_file` payload carries a whole source file was counted as
    nothing at all; measured against Ollama, one such message was 341 tokens,
    and the two worst in a live session were 2,618 and 2,231. Across a
    130-message session that hid 9,795 tokens - 22% of the prompt - so the
    budget never trimmed, the window filled, and 19 generations were cut off
    mid-word with the harness reporting nothing was wrong.
    """
    counter = token_counter or count_tokens
    total = counter(str(_message_field(message, "content") or ""))
    tool_calls = _message_field(message, "tool_calls")
    if tool_calls:
        total += counter(json.dumps(tool_calls, default=str))
    return total + overhead


def messages_tokens(
    messages: Any,
    token_counter: Callable[[str], int] | None = None,
) -> int:
    """What a whole message list costs, envelope included."""
    return sum(message_tokens(message, token_counter) for message in messages)


def tool_schema_tokens(schemas: Any) -> int:
    """What the tool definitions cost. They ship on EVERY request.

    Counted nowhere before: the six simple-mode schemas are ~630 tokens that
    were spent on every single call and charged to no budget.
    """
    if not schemas:
        return 0
    return count_tokens(json.dumps(schemas, default=str))


def clamp_calibration(factor: float) -> float:
    """Keep a measured correction factor inside a range worth trusting."""
    try:
        value = float(factor)
    except (TypeError, ValueError):
        return 1.0
    return min(CALIBRATION_MAX, max(CALIBRATION_MIN, value))


@lru_cache(maxsize=1)
def _load_tokenizer():
    try:
        from tokenizers import Tokenizer
    except ModuleNotFoundError:
        return None
    if not TOKENIZER_ASSET.exists():
        return None
    try:
        return Tokenizer.from_file(str(TOKENIZER_ASSET))
    except Exception:
        return None
