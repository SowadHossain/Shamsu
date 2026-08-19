"""Context-window tracking, terminal usage indicators, and smart auto-compaction.

This is NOT a new memory or context system.  It is a budget, indicator, and
compaction layer that sits inside the existing context pipeline.  Roles:
  - ContextBudgetManager  : the public API (measure → show → compact → calibrate)
  - BudgetResult          : immutable snapshot of a single budget computation
  - Helper functions      : compaction of ContextPack fields (not Graphiti/ActionLedger)

ActionLedger is never included as automatic context (per pipeline.md §9).
Graphiti and Codebase-Memory MCP remain responsible for their own domains.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from shamsu.context.budget import (
    RESERVE_OUTPUT_TOKENS,
    SAFETY_MARGIN_TOKENS,
    count_tokens,
    ctx_window_for_model,
)
from shamsu.types import ContextPack, SearchResult

# Compact when used tokens exceed this fraction of the usable window.
_DEFAULT_COMPACT_THRESHOLD = 0.80

_CALIBRATION_FILENAME = "context_calibration.json"


# ── Budget result ──────────────────────────────────────────────────────────────

@dataclass
class BudgetResult:
    """Snapshot of the context budget for one model call."""

    model_name: str
    specialist: str
    estimated_tokens: int
    context_window: int
    reserve_tokens: int
    compacted: bool = False
    # The estimate BEFORE the calibration factor was applied. Calibration is
    # defined as actual/raw, so feeding it the calibrated number makes the
    # factor its own input: the EMA then settles on sqrt(true ratio) instead
    # of the true ratio, leaving over half of a measured undercount in place.
    raw_estimated_tokens: int = 0

    @property
    def usable_tokens(self) -> int:
        return max(0, self.context_window - self.reserve_tokens)

    @property
    def usage_fraction(self) -> float:
        if self.usable_tokens == 0:
            return 1.0
        return min(1.0, self.estimated_tokens / self.usable_tokens)

    @property
    def usage_pct(self) -> int:
        return round(self.usage_fraction * 100)


# ── Manager ────────────────────────────────────────────────────────────────────

def _fmt_k(n: int) -> str:
    """'41700' → '41.7k', '512' → '512'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


class ContextBudgetManager:
    """Measures, displays, and manages the context budget before each model call.

    Usage::

        mgr = ContextBudgetManager(print_fn=console.print, workspace=workspace)
        result = mgr.compute(model_name, specialist, prompt_text)
        if mgr.should_compact(result):
            pack = mgr.compact_pack(pack, result)
            prompt_text = rebuild_prompt(pack)
            result = mgr.compute(model_name, specialist, prompt_text)
            result = replace(result, compacted=True)
        mgr.show_indicator(result)
        # … call model …
        mgr.calibrate_from_response(model_name, prompt_eval_count, result.raw_estimated_tokens)
    """

    def __init__(
        self,
        print_fn: Callable[[str], None] | None = None,
        workspace: Path | None = None,
        compact_threshold: float = _DEFAULT_COMPACT_THRESHOLD,
    ) -> None:
        self._print_fn = print_fn
        self._workspace = workspace
        self._compact_threshold = compact_threshold
        # per-model correction factor: actual_tokens / estimated_tokens (EMA)
        self._calibration: dict[str, float] = {}
        self._last_result: BudgetResult | None = None
        self._load_calibration()

    # ── Public API ──────────────────────────────────────────────────────────────

    def compute(self, model_name: str, specialist: str, prompt_text: str) -> BudgetResult:
        """Estimate token usage for *prompt_text* and return a BudgetResult."""
        raw_est = count_tokens(prompt_text)
        factor = self._calibration.get(model_name, 1.0)
        estimated = max(1, round(raw_est * factor))
        window = ctx_window_for_model(model_name)
        reserve = RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
        result = BudgetResult(
            model_name=model_name,
            specialist=specialist,
            estimated_tokens=estimated,
            raw_estimated_tokens=raw_est,
            context_window=window,
            reserve_tokens=reserve,
        )
        self._last_result = result
        return result

    def format_indicator(self, result: BudgetResult) -> str:
        """Format a one-line terminal indicator, e.g.:
        ctx coder 41.7k/32.8k 65% | reserve 2.6k | compact off
        """
        compact_flag = "on" if result.compacted else "off"
        reserve_str = _fmt_k(result.reserve_tokens)
        bar = f"{_fmt_k(result.estimated_tokens)}/{_fmt_k(result.context_window)}"
        return (
            f"ctx {result.specialist} {bar} {result.usage_pct}% "
            f"| reserve {reserve_str} | compact {compact_flag}"
        )

    def show_indicator(self, result: BudgetResult) -> None:
        """Print the indicator via the registered print_fn (no-op if None)."""
        if self._print_fn:
            self._print_fn(f"[dim]{self.format_indicator(result)}[/dim]")

    def should_compact(self, result: BudgetResult) -> bool:
        return result.usage_fraction > self._compact_threshold

    def compact_pack(self, pack: ContextPack, result: BudgetResult) -> ContextPack:
        """Reduce *pack* to fit within the usable token window.

        Compaction order — lowest-value content is removed first:
          1. error_context  : strip verbose log lines; keep error codes + traces
          2. prd_context    : keep requirement lines; drop long prose paragraphs
          3. snippets       : drop lowest-score snippets until under 75 % of budget

        user_request is never touched.
        ActionLedger content is never in ContextPack, so it is never compacted.
        """
        target = int(result.usable_tokens * 0.75)

        # Step 1: compact verbose error logs
        pack = replace(pack, error_context=_compact_error_context(pack.error_context))
        # Step 2: compact long prd prose
        pack = replace(pack, prd_context=_compact_prd_context(pack.prd_context))

        # Step 3: drop lowest-score snippets if still over target
        remaining = sorted(pack.snippets, key=lambda s: s.score, reverse=True)
        kept: list[SearchResult] = []
        used = (
            count_tokens(pack.user_request)
            + count_tokens(pack.prd_context)
            + count_tokens(pack.error_context)
        )
        for snippet in remaining:
            cost = count_tokens(snippet.content)
            if used + cost > target and kept:
                break
            kept.append(snippet)
            used += cost

        return replace(pack, snippets=kept)

    def calibrate_from_response(
        self,
        model_name: str,
        prompt_eval_count: int,
        estimated_tokens: int,
    ) -> None:
        """Improve future estimates using Ollama's actual prompt_eval_count.

        *estimated_tokens* must be the RAW, uncalibrated estimate. Passing the
        already-corrected number feeds the factor back into itself.

        Uses an exponential moving average (α=0.2) so a single outlier
        doesn't destabilise the calibration.
        """
        if estimated_tokens <= 0 or prompt_eval_count <= 0:
            return
        new_factor = prompt_eval_count / estimated_tokens
        old = self._calibration.get(model_name, 1.0)
        self._calibration[model_name] = round(0.8 * old + 0.2 * new_factor, 4)
        self._save_calibration()

    @property
    def last_result(self) -> BudgetResult | None:
        return self._last_result

    def calibration_factor(self, model_name: str) -> float:
        return self._calibration.get(model_name, 1.0)

    # ── Persistence ─────────────────────────────────────────────────────────────

    def _calibration_path(self) -> Path | None:
        if self._workspace is None:
            return None
        from shamsu import paths

        return paths.context_calibration(self._workspace)

    def _load_calibration(self) -> None:
        path = self._calibration_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._calibration = {k: float(v) for k, v in data.items()}
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    def _save_calibration(self) -> None:
        path = self._calibration_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self._calibration, indent=2), encoding="utf-8"
            )
        except OSError:
            pass


# ── Compaction helpers ─────────────────────────────────────────────────────────

# Patterns that identify high-value lines worth keeping in error_context.
_ERROR_HIGH_VALUE = re.compile(
    r"(error|exception|traceback|failed|exit\s+code|warning|"
    r"import |from |export |def |class |```|File \"|\.py:\d|\.ts:\d|\.js:\d)",
    re.IGNORECASE,
)


def _compact_error_context(text: str) -> str:
    """Keep error codes, stack traces, and exit codes; strip verbose log lines.

    Preserves exact error/exception lines — these are the facts the model
    must see to produce a correct fix.  Long INFO/DEBUG log lines are dropped.
    """
    if not text.strip():
        return text
    lines = text.splitlines()
    if len(lines) <= 20:
        return text
    head = lines[:10]
    tail = lines[-10:]
    middle = lines[10:-10]
    kept_middle = [ln for ln in middle if _ERROR_HIGH_VALUE.search(ln)]
    omitted = len(middle) - len(kept_middle)
    result_lines = head[:]
    if kept_middle:
        result_lines += kept_middle
    if omitted:
        result_lines.append(f"# ... ({omitted} log lines omitted) ...")
    result_lines += tail
    return "\n".join(result_lines)


# Patterns that identify PRD requirement lines worth keeping.
_PRD_REQUIREMENT = re.compile(
    r"\b(must|should|shall|require|feature|task|step|endpoint|entity|page|field|"
    r"GET|POST|PUT|DELETE|PATCH)\b",
    re.IGNORECASE,
)


def _compact_prd_context(text: str) -> str:
    """Keep requirement lines and headings; drop long prose paragraphs.

    Exact feature names, entity names, and endpoint paths are preserved.
    Long narrative text that does not contain requirement keywords is dropped.
    """
    if not text.strip():
        return text
    lines = text.splitlines()
    if len(lines) <= 30:
        return text
    kept: list[str] = []
    omitted = 0
    for line in lines:
        stripped = line.strip()
        if (
            not stripped                                        # blank line
            or stripped.startswith(("#", "-", "*", "`", ">")) # structure
            or _PRD_REQUIREMENT.search(stripped)               # requirement
        ):
            kept.append(line)
        else:
            omitted += 1
    if omitted:
        kept.append(f"# ... ({omitted} prose lines omitted) ...")
    return "\n".join(kept)
