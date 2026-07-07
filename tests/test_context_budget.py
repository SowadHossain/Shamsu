from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from shamsu.context import budget
from shamsu.context.budget import (
    MODEL_CONTEXT_WINDOWS,
    RESERVE_OUTPUT_TOKENS,
    SAFE_FALLBACK_CTX_WINDOW,
    SAFETY_MARGIN_TOKENS,
    ctx_window_for_model,
)
from shamsu.context.manager import (
    BudgetResult,
    ContextBudgetManager,
    _compact_error_context,
    _compact_prd_context,
)
from shamsu.types import ContextPack, SearchResult


# ─── Existing tokenizer tests ────────────────────────────────────────────────

def test_count_tokens_uses_vendored_tokenizer_when_available():
    budget._load_tokenizer.cache_clear()

    text = "def add(a,b): return a+b"

    assert budget.TOKENIZER_ASSET.exists()
    assert budget.count_tokens(text) == 8
    assert budget.count_tokens(text) != len(text) // budget.CHARS_PER_TOKEN_ESTIMATE


def test_count_tokens_falls_back_when_asset_missing(monkeypatch, tmp_path):
    missing = tmp_path / "missing-tokenizer.json"
    monkeypatch.setattr(budget, "TOKENIZER_ASSET", missing)
    budget._load_tokenizer.cache_clear()

    text = "def add(a,b): return a+b"

    assert budget.count_tokens(text) == len(text) // budget.CHARS_PER_TOKEN_ESTIMATE


def test_tokenizer_asset_is_vendored_under_context_assets():
    assert budget.TOKENIZER_ASSET == Path(budget.__file__).resolve().parent / "assets" / "qwen3-tokenizer.json"


# ─── Context window constants ─────────────────────────────────────────────────

def test_known_model_returns_correct_window():
    assert ctx_window_for_model("qwen2.5-coder:7b-instruct") == 32_768
    assert ctx_window_for_model("mistral-nemo:12b") == 131_072


def test_unknown_model_returns_safe_fallback():
    assert ctx_window_for_model("totally-unknown-model:99b") == SAFE_FALLBACK_CTX_WINDOW
    assert SAFE_FALLBACK_CTX_WINDOW == 8_192


def test_planner_and_coder_can_have_different_windows():
    # planner uses a thinking model; coder uses a coding model
    planner_model = "qwen3:8b"
    coder_model = "qwen2.5-coder:14b"
    assert planner_model in MODEL_CONTEXT_WINDOWS
    assert coder_model in MODEL_CONTEXT_WINDOWS
    # Both are valid — just assert they are independent entries
    assert ctx_window_for_model(planner_model) == MODEL_CONTEXT_WINDOWS[planner_model]
    assert ctx_window_for_model(coder_model) == MODEL_CONTEXT_WINDOWS[coder_model]


# ─── BudgetResult calculations ────────────────────────────────────────────────

def test_budget_result_usable_tokens():
    r = BudgetResult(
        model_name="qwen2.5-coder:7b-instruct",
        specialist="coder",
        estimated_tokens=20_000,
        context_window=32_768,
        reserve_tokens=RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS,
    )
    assert r.usable_tokens == 32_768 - r.reserve_tokens


def test_budget_result_usage_fraction_and_pct():
    reserve = RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
    r = BudgetResult(
        model_name="qwen2.5-coder:7b-instruct",
        specialist="coder",
        estimated_tokens=10_000,
        context_window=32_768,
        reserve_tokens=reserve,
    )
    expected_fraction = 10_000 / (32_768 - reserve)
    assert abs(r.usage_fraction - expected_fraction) < 0.001
    assert r.usage_pct == round(expected_fraction * 100)


def test_budget_result_capped_at_100_pct():
    reserve = RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
    r = BudgetResult(
        model_name="qwen2.5-coder:7b-instruct",
        specialist="coder",
        estimated_tokens=999_999,
        context_window=32_768,
        reserve_tokens=reserve,
    )
    assert r.usage_fraction == 1.0
    assert r.usage_pct == 100


# ─── ContextBudgetManager: compute ───────────────────────────────────────────

def test_compute_returns_budget_result(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path)
    result = mgr.compute("qwen2.5-coder:7b-instruct", "coder", "def foo(): pass")
    assert isinstance(result, BudgetResult)
    assert result.specialist == "coder"
    assert result.model_name == "qwen2.5-coder:7b-instruct"
    assert result.estimated_tokens > 0
    assert result.context_window == 32_768


def test_compute_uses_safe_fallback_for_unknown_model(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path)
    result = mgr.compute("mystery-model:99b", "qa", "hello world")
    assert result.context_window == SAFE_FALLBACK_CTX_WINDOW


def test_compute_stores_last_result(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path)
    assert mgr.last_result is None
    mgr.compute("qwen3:8b", "planner", "plan this task")
    assert mgr.last_result is not None
    assert mgr.last_result.specialist == "planner"


# ─── Terminal indicator ───────────────────────────────────────────────────────

def test_format_indicator_shape(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path)
    result = mgr.compute("qwen2.5-coder:7b-instruct", "coder", "x" * 1000)
    indicator = mgr.format_indicator(result)
    assert "coder" in indicator
    assert "%" in indicator
    assert "reserve" in indicator
    assert "compact" in indicator


def test_format_indicator_compact_flag(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path)
    r = BudgetResult("m", "coder", 1000, 32_768, 2560)
    assert "compact off" in mgr.format_indicator(r)
    assert "compact on" in mgr.format_indicator(replace(r, compacted=True))


def test_show_indicator_calls_print_fn(tmp_path):
    printed: list[str] = []
    mgr = ContextBudgetManager(print_fn=printed.append, workspace=tmp_path)
    result = mgr.compute("qwen3:8b", "planner", "some prompt text")
    mgr.show_indicator(result)
    assert len(printed) == 1
    assert "planner" in printed[0]


def test_show_indicator_noop_when_no_print_fn(tmp_path):
    mgr = ContextBudgetManager(print_fn=None, workspace=tmp_path)
    result = mgr.compute("qwen3:8b", "planner", "text")
    mgr.show_indicator(result)  # must not raise


# ─── Auto-compaction threshold ────────────────────────────────────────────────

def test_should_compact_above_threshold(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path, compact_threshold=0.80)
    # Build a result just over 80 % of usable tokens
    reserve = RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
    usable = 32_768 - reserve
    over = int(usable * 0.85)
    r = BudgetResult("qwen2.5-coder:7b-instruct", "coder", over, 32_768, reserve)
    assert mgr.should_compact(r)


def test_should_not_compact_below_threshold(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path, compact_threshold=0.80)
    reserve = RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
    usable = 32_768 - reserve
    under = int(usable * 0.50)
    r = BudgetResult("qwen2.5-coder:7b-instruct", "coder", under, 32_768, reserve)
    assert not mgr.should_compact(r)


# ─── compact_pack: exact code is preserved ───────────────────────────────────

def _make_snippet(score: float, content: str = "def foo(): pass\n    return 42") -> SearchResult:
    return SearchResult(
        file_path="src/game.py",
        language="python",
        line_start=1,
        line_end=2,
        content=content,
        score=score,
    )


def test_compact_pack_preserves_user_request(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path)
    pack = ContextPack(
        task_id="t1", step_id=1, specialist="coder",
        user_request="def update_score(player, delta): ...",
        snippets=[_make_snippet(0.9)],
        error_context="",
        prd_context="",
    )
    reserve = RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
    r = BudgetResult("qwen2.5-coder:7b-instruct", "coder", 25_000, 32_768, reserve)
    compacted = mgr.compact_pack(pack, r)
    assert compacted.user_request == pack.user_request


def test_compact_pack_preserves_high_score_snippets(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path)
    high = _make_snippet(0.99, "class GameEngine:\n    def tick(self): ...")
    low = _make_snippet(0.10, "# old comment\n" * 200)
    pack = ContextPack(
        task_id="t1", step_id=1, specialist="coder",
        user_request="update tick",
        snippets=[high, low],
        error_context="",
        prd_context="",
    )
    reserve = RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
    r = BudgetResult("qwen2.5-coder:7b-instruct", "coder", 28_000, 32_768, reserve)
    compacted = mgr.compact_pack(pack, r)
    kept_paths = [s.score for s in compacted.snippets]
    assert 0.99 in kept_paths


# ─── compact_pack: raw logs are compacted first ───────────────────────────────

def test_compact_error_context_strips_log_lines():
    verbose = "\n".join([f"[INFO] log line {i}" for i in range(50)])
    result = _compact_error_context(verbose)
    assert len(result.splitlines()) < 50
    assert "omitted" in result


def test_compact_error_context_keeps_error_lines():
    lines = ["[INFO] irrelevant"] * 30 + ["Error: file not found", "exit code 1"]
    text = "\n".join(lines)
    result = _compact_error_context(text)
    assert "Error: file not found" in result
    assert "exit code 1" in result


def test_compact_error_context_short_text_unchanged():
    text = "Error: something failed\nFile: game.ts:12"
    assert _compact_error_context(text) == text


def test_compact_prd_context_keeps_requirements():
    lines = [
        "## Overview",
        "This is a long prose paragraph about the vision.",
        "- Must support multiplayer with up to 8 players",
        "This is more filler text that does not matter.",
        "- Should have a leaderboard endpoint",
    ]
    result = _compact_prd_context("\n".join(lines * 10))
    assert "Must support multiplayer" in result
    assert "Should have a leaderboard" in result


def test_compact_prd_context_short_text_unchanged():
    text = "## Overview\n- feature 1\n- feature 2"
    assert _compact_prd_context(text) == text


# ─── ActionLedger is not included as automatic context ────────────────────────

def test_context_pack_has_no_action_ledger_field():
    """ContextPack must not have an action_ledger field — ActionLedger is audit-only."""
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(ContextPack)}
    assert "action_ledger" not in field_names
    assert "ledger" not in field_names


def test_compact_pack_does_not_create_ledger_context(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path)
    pack = ContextPack(
        task_id="t1", step_id=1, specialist="coder",
        user_request="fix bug",
        snippets=[_make_snippet(0.8)],
    )
    reserve = RESERVE_OUTPUT_TOKENS + SAFETY_MARGIN_TOKENS
    r = BudgetResult("qwen2.5-coder:7b-instruct", "coder", 28_000, 32_768, reserve)
    result = mgr.compact_pack(pack, r)
    # No ledger-related field introduced
    assert not hasattr(result, "action_ledger")
    assert not hasattr(result, "ledger")


# ─── Calibration from prompt_eval_count ──────────────────────────────────────

def test_calibration_updates_correction_factor(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path)
    assert mgr.calibration_factor("qwen2.5-coder:7b-instruct") == 1.0

    # Suppose our estimate is 1000 but actual is 1200 (model tokenizer differs)
    mgr.calibrate_from_response("qwen2.5-coder:7b-instruct", 1200, 1000)
    factor = mgr.calibration_factor("qwen2.5-coder:7b-instruct")
    # EMA: 0.8 * 1.0 + 0.2 * 1.2 = 1.04
    assert abs(factor - 1.04) < 0.001


def test_calibration_ema_convergence(tmp_path):
    """After many identical observations the factor should converge to the ratio."""
    mgr = ContextBudgetManager(workspace=tmp_path)
    for _ in range(30):
        mgr.calibrate_from_response("qwen3:8b", 1500, 1000)
    factor = mgr.calibration_factor("qwen3:8b")
    assert abs(factor - 1.5) < 0.01


def test_calibration_ignored_when_zero(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path)
    mgr.calibrate_from_response("qwen3:8b", 0, 1000)
    mgr.calibrate_from_response("qwen3:8b", 1000, 0)
    assert mgr.calibration_factor("qwen3:8b") == 1.0


def test_calibration_persisted_and_loaded(tmp_path):
    mgr1 = ContextBudgetManager(workspace=tmp_path)
    mgr1.calibrate_from_response("qwen2.5-coder:7b-instruct", 1200, 1000)
    factor_before = mgr1.calibration_factor("qwen2.5-coder:7b-instruct")

    mgr2 = ContextBudgetManager(workspace=tmp_path)
    assert abs(mgr2.calibration_factor("qwen2.5-coder:7b-instruct") - factor_before) < 0.001


def test_calibration_affects_compute_estimate(tmp_path):
    mgr = ContextBudgetManager(workspace=tmp_path)
    text = "def foo(): return 42"
    result_before = mgr.compute("qwen2.5-coder:7b-instruct", "coder", text)

    # Train: actual tokens are always 2× our estimate
    for _ in range(20):
        mgr.calibrate_from_response("qwen2.5-coder:7b-instruct", result_before.estimated_tokens * 2, result_before.estimated_tokens)

    result_after = mgr.compute("qwen2.5-coder:7b-instruct", "coder", text)
    assert result_after.estimated_tokens > result_before.estimated_tokens
