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
    "qwen3:8b":                   32_768,
    "qwen2.5:3b-instruct":        32_768,
    "qwen2.5-coder:7b-instruct":  32_768,
    "qwen2.5-coder:3b-instruct":  32_768,
    "mistral-nemo:12b":          131_072,
    "qwen2.5-coder:14b":          32_768,
}

SAFE_FALLBACK_CTX_WINDOW = 8_192   # conservative fallback for unknown models
RESERVE_OUTPUT_TOKENS = 2_048      # headroom reserved for model response
SAFETY_MARGIN_TOKENS = 512         # extra buffer against off-by-one token counts


def ctx_window_for_model(model_name: str) -> int:
    """Return the known context window for *model_name*, or the safe fallback."""
    return MODEL_CONTEXT_WINDOWS.get(model_name, SAFE_FALLBACK_CTX_WINDOW)


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
