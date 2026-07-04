"""Context token budgeting.

Uses a vendored Qwen3 tokenizer when the lightweight `tokenizers` package and
asset are available. Falls back to the old char/4 estimate offline or in
minimal installs.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

# Effective budget = num_ctx * safety margin (leave room for the response).
# 8192 * 0.80 ~= 6554.
TOTAL_BUDGET_DEFAULT = 6554

CHARS_PER_TOKEN_ESTIMATE = 4
TOKENIZER_ASSET = Path(__file__).resolve().parent / "assets" / "qwen3-tokenizer.json"


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
