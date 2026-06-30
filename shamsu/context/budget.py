"""
shamsu/context/budget.py — Dev B owns this file.

Cheap token estimation: char-count / 4. This is intentionally NOT a real
tokenizer in the hot path — loading transformers' tokenizer for every
context-pack build would add noticeable overhead for marginal accuracy
gain (see ENGINEERING_HARNESS.md §6, "transformers tokenizer" row).

If/when precise counts matter (e.g. you're right at the edge of num_ctx
and getting truncation-related failures), swap in the real Qwen2.5/Phi-3
HF tokenizer behind the same count_tokens() signature — nothing else
in the codebase should need to change.
"""
from __future__ import annotations

# Effective budget = num_ctx * safety margin (leave room for the response).
# 8192 * 0.80 ≈ 6554 — matches the Qwen2.5-Coder default num_ctx in
# llm/manager.py. Drop to ~3277 (4096 * 0.80) if num_ctx is reduced.
TOTAL_BUDGET_DEFAULT = 6554

CHARS_PER_TOKEN_ESTIMATE = 4


def count_tokens(text: str) -> int:
    """Fast, dependency-free token estimate. ~char/4 for English + code."""
    return max(len(text) // CHARS_PER_TOKEN_ESTIMATE, 0)
