"""Context token budgeting.

Uses a vendored Qwen3 tokenizer when the lightweight `tokenizers` package and
asset are available. Falls back to the old char/4 estimate offline or in
minimal installs.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    "qwen2.5:3b-instruct":        32_768,
    "qwen2.5-coder:7b-instruct":  32_768,
    "qwen2.5-coder:3b-instruct":  32_768,
    "mistral-nemo:12b":          131_072,
    "qwen2.5-coder:14b":          32_768,
}

SAFE_FALLBACK_CTX_WINDOW = 8_192   # conservative fallback for unknown models

# Families whose every published size carries at least a 32k window. An exact
# MODEL_CONTEXT_WINDOWS entry always wins; this only rescues models the cookbook
# has never heard of.
#
# Without this, pulling a NEWER model than the cookbook knows silently costs you
# three quarters of your context: live 2026-08-17, `qwen3.5:9b-q4_K_M` matched no
# entry, fell back to 8192, and the agent ran at "ctx chat 3.8k/8.2k 100%" - with
# 4.6k reserved for output, the state frame had ~3.6k to hold the plan, the spec
# and the conversation. It could not, so the model re-inspected and re-read
# instead of writing, and the step budget was gone before a single file existed.
_CTX_FAMILY_WINDOWS: tuple[tuple[str, int], ...] = (
    ("gemma3", 131_072),
    ("mistral-nemo", 131_072),
    ("llama3.1", 131_072),
    ("llama3.2", 131_072),
    ("qwen3", 32_768),
    ("qwen2.5", 32_768),
    ("deepseek-r1", 32_768),
    ("qwq", 32_768),
    ("phi4", 16_384),
)


def _reserve_output_tokens() -> int:
    """Headroom reserved for the model's response.

    Raised from 2048 because a mutation turn now emits a WHOLE FILE: the raw write
    envelope asks for complete file content, and 2048 tokens is roughly 200 lines,
    which real modules and templates routinely exceed. When the reserve is too
    small the prompt is allowed to grow until the response has nowhere to go, and
    the model's tool call is cut off mid-payload - the `json_truncated` failure in
    llm/output.py.

    Note this is the right lever, not `num_predict`: an explicit output cap cannot
    make room, it can only stop generation sooner, so capping would CAUSE the
    truncation it is meant to prevent. Reserving space prevents it.

    Raised to 8192 (roughly 800 lines) now that the window is large enough to
    afford it. At 4096 a turn asked to write a whole module had ~400 lines of
    room, and the overflow shape is the worst one available: the tool call is
    cut off mid-payload and the write never lands.
    """
    import os

    raw = os.environ.get("SHAMSU_RESERVE_OUTPUT_TOKENS", "").strip()
    if raw.isdigit() and int(raw) >= 512:
        return int(raw)
    return 8_192


RESERVE_OUTPUT_TOKENS = _reserve_output_tokens()
SAFETY_MARGIN_TOKENS = 512         # extra buffer against off-by-one token counts


def ctx_window_for_model(model_name: str) -> int:
    """Return the context window for *model_name*, or the safe fallback.

    In order: an explicit user override, what the model itself declares to
    Ollama, the cookbook, the model's FAMILY, then the conservative default.

    The declared value wins over the cookbook because it is ground truth and the
    cookbook is a hardcoded table that any newer model silently falls out of -
    which cost three quarters of the window without a word of warning. Family
    matching then covers the offline case, and being wrong low there is the
    expensive direction: it starves the state frame instead of failing visibly.
    """
    import os

    try:
        override = int(os.environ.get("SHAMSU_MODEL_CTX_WINDOW", "").strip())
    except ValueError:
        override = 0
    if override > 0:
        return override
    declared = _declared_ctx_window(model_name)
    if declared > 0:
        return declared
    exact = MODEL_CONTEXT_WINDOWS.get(model_name)
    if exact is not None:
        return exact
    lowered = (model_name or "").strip().lower()
    for family, window in _CTX_FAMILY_WINDOWS:
        if lowered.startswith(family):
            return window
    return SAFE_FALLBACK_CTX_WINDOW


def _declared_ctx_window(model_name: str) -> int:
    """What Ollama says this model's window is, or 0 when it cannot be reached."""
    if not (model_name or "").strip():
        return 0
    try:
        # Local import: runtime.ollama -> llm.manager -> context.budget would
        # cycle at module level.
        from shamsu.runtime.ollama import declared_context_length

        return declared_context_length(model_name)
    except Exception:
        return 0


def count_tokens(text: str) -> int:
    """Count tokens with the vendored tokenizer, falling back to char/4."""
    tokenizer = _load_tokenizer()
    if tokenizer is not None:
        return len(tokenizer.encode(text).ids)
    return max(len(text) // CHARS_PER_TOKEN_ESTIMATE, 0)


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
