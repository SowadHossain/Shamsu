from __future__ import annotations

from pathlib import Path

from shamsu.context import budget


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
