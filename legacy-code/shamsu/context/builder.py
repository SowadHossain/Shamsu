"""
shamsu/context/builder.py — Dev B owns this file.

Implements IContextBuilder. Token budget allocation follows
ENGINEERING_HARNESS.md: ~10% system, ~50% code snippets, ~15% PRD/task,
~10% errors, ~10% history, ~5% reserved. Layout exploits the
"Lost in the Middle" finding (Liu et al., TACL 2024) — the caller
(llm/manager.py) is responsible for placing the task restatement at
the END of the assembled prompt, not just this pack's snippet order.

Token counting uses a cheap char/4 estimate by default (no tokenizer
dependency in the hot path) — see context/budget.py for the exact
function and when to upgrade to a real tokenizer.
"""
from __future__ import annotations

from shamsu.interfaces import IContextBuilder
from shamsu.types import ContextPack, SearchResult
from shamsu.context.budget import count_tokens, TOTAL_BUDGET_DEFAULT

# Budget allocation — fractions of the total token budget.
# See ENGINEERING_HARNESS.md Stage 0 for the rationale.
BUDGET_FRACTIONS = {
    "code_snippets": 0.50,
    "prd_task": 0.15,
    "errors_tests": 0.10,
    "previous_summary": 0.10,
    "system_user": 0.15,
}

TRUNCATE_HEAD_LINES = 10
TRUNCATE_TAIL_LINES = 5


def _truncate_middle(content: str, max_tokens: int) -> str:
    """
    Keep the function signature (top) and closing lines / return statement
    (bottom) — these are what the LLM needs most for edits. Truncating from
    the end cuts the return statement, which is the single most damaging
    place to lose information for code-editing tasks.
    """
    if count_tokens(content) <= max_tokens:
        return content

    lines = content.splitlines()
    if len(lines) <= TRUNCATE_HEAD_LINES + TRUNCATE_TAIL_LINES:
        return content  # too short to usefully truncate further

    head = lines[:TRUNCATE_HEAD_LINES]
    tail = lines[-TRUNCATE_TAIL_LINES:]
    omitted = len(lines) - TRUNCATE_HEAD_LINES - TRUNCATE_TAIL_LINES
    return "\n".join(head) + f"\n# ... ({omitted} lines omitted) ...\n" + "\n".join(tail)


def _deduplicate(results: list[SearchResult]) -> list[SearchResult]:
    """Drop snippets that share 50%+ of their lines with an already-kept one."""
    kept: list[SearchResult] = []
    for r in results:
        r_lines = set(r.content.splitlines())
        if not r_lines:
            continue
        is_dupe = False
        for k in kept:
            k_lines = set(k.content.splitlines())
            overlap = len(r_lines & k_lines) / max(len(r_lines), 1)
            if overlap >= 0.5:
                is_dupe = True
                break
        if not is_dupe:
            kept.append(r)
    return kept


class ContextBuilder(IContextBuilder):
    def pack(
        self,
        results: list[SearchResult],
        request: str,
        task_id: str,
        step_id: int,
        specialist: str,
        budget_tokens: int = TOTAL_BUDGET_DEFAULT,
    ) -> ContextPack:
        results = _deduplicate(results)

        code_budget = int(budget_tokens * BUDGET_FRACTIONS["code_snippets"])
        per_snippet_budget = max(code_budget // max(len(results), 1), 100)

        packed_snippets: list[SearchResult] = []
        used = 0
        for r in sorted(results, key=lambda x: x.score, reverse=True):
            truncated_content = _truncate_middle(r.content, per_snippet_budget)
            cost = count_tokens(truncated_content)
            if used + cost > code_budget:
                break
            packed_snippets.append(
                SearchResult(
                    file_path=r.file_path, language=r.language,
                    line_start=r.line_start, line_end=r.line_end,
                    content=truncated_content, score=r.score,
                    symbol_name=r.symbol_name, chunk_type=r.chunk_type,
                )
            )
            used += cost

        pack = ContextPack(
            task_id=task_id, step_id=step_id, specialist=specialist,
            user_request=request, snippets=packed_snippets,
        )
        pack.token_estimate = used + count_tokens(request)
        return pack
