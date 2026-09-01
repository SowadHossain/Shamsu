"""What the server says about a model, and the limits you can change from here.

Two changes with one shape: a number that was hardcoded, or hand-maintained,
becomes a number something authoritative answers for.

* `MODEL_CONTEXT_WINDOWS` is thirty hand-written entries. Measured 2026-08-30
  against the local server, `qwen3:8b` was listed at 32,768 and holds 40,960,
  and `gemma3:4b` reports no tool-calling capability at all - which a table of
  window sizes cannot express, so it was being sent 37 tool schemas.
* `max_rounds`, the turn budget and the approval timeout were reachable only by
  exporting a variable and restarting, or by editing the source.
"""
from __future__ import annotations

import json

import pytest

from shamsu.context.budget import (
    MIN_USABLE_CTX_WINDOW,
    SAFE_FALLBACK_CTX_WINDOW,
    ctx_window_for_model,
)
from shamsu.llm import capabilities
from shamsu.runtime import settings as settings_module
from shamsu.runtime.models import model_supports_native_tools


@pytest.fixture(autouse=True)
def _clean_cache():
    capabilities.clear_cache()
    yield
    capabilities.clear_cache()


@pytest.fixture
def served(monkeypatch):
    """Pin what `/api/tags` answers, without a server."""

    def serve(models):
        payload = json.dumps({"models": models}).encode("utf-8")

        class _Response:
            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(
            capabilities.urllib.request, "urlopen", lambda *_a, **_k: _Response()
        )
        return capabilities.refresh_model_facts("http://pinned")

    return serve


def _model(name, *, ctx=None, caps=("tools",)):
    details = {"parameter_size": "7B"}
    if ctx is not None:
        details["context_length"] = ctx
    return {"name": name, "details": details, "capabilities": list(caps)}


# -- the server outranks the table ------------------------------------------


def test_the_server_answer_beats_a_stale_table_entry(served):
    """qwen3:8b - listed 32,768, really 40,960."""
    served([_model("qwen3:8b", ctx=40960)])
    assert ctx_window_for_model("qwen3:8b") == 40960


def test_a_model_the_server_does_not_know_falls_back_to_the_table(served):
    served([_model("qwen3:8b", ctx=40960)])
    assert ctx_window_for_model("qwen2.5-coder:7b-instruct") == 32768


def test_a_server_that_reports_no_window_falls_back_to_the_table(served):
    """gemma3:4b reports capabilities but no context_length."""
    served([_model("gemma3:4b", ctx=None, caps=())])
    assert ctx_window_for_model("gemma3:4b") == 131072


def test_an_absurd_server_answer_is_ignored(served):
    """Below the floor is a broken answer, not a small model."""
    served([_model("tiny:1b", ctx=64)])
    assert ctx_window_for_model("tiny:1b") == SAFE_FALLBACK_CTX_WINDOW
    assert MIN_USABLE_CTX_WINDOW > 64


def test_no_server_at_all_still_answers_from_the_table(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(capabilities.urllib.request, "urlopen", refuse)
    capabilities.refresh_model_facts("http://pinned")
    assert ctx_window_for_model("qwen2.5-coder:7b-instruct") == 32768


def test_a_dead_server_does_not_erase_what_it_last_said(served, monkeypatch):
    served([_model("qwen3:8b", ctx=40960)])

    def refuse(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(capabilities.urllib.request, "urlopen", refuse)
    capabilities.refresh_model_facts("http://pinned")
    assert ctx_window_for_model("qwen3:8b") == 40960


# -- tool capability --------------------------------------------------------


def test_a_model_without_tool_support_is_known_to_have_none(served):
    served([_model("gemma3:4b", ctx=None, caps=("completion",))])
    assert model_supports_native_tools("gemma3:4b") is False


def test_tool_support_defaults_to_yes_when_the_server_is_silent(served):
    """An older server reports no capabilities array; that is not a refusal."""
    served([_model("qwen3:8b", ctx=40960, caps=())])
    assert model_supports_native_tools("qwen3:8b") is True


def test_an_unknown_model_is_assumed_to_do_tools(served):
    served([])
    assert model_supports_native_tools("something:new") is True


# -- limits you can change from here ----------------------------------------


@pytest.fixture
def stored(monkeypatch, tmp_path):
    """Point settings.json at a temp file and clear the environment."""
    monkeypatch.setattr(
        settings_module, "settings_path", lambda: tmp_path / "settings.json"
    )
    for key in settings_module.NUMERIC_LIMITS:
        monkeypatch.delenv(f"SHAMSU_{key.upper()}", raising=False)
    return tmp_path / "settings.json"


@pytest.mark.parametrize("key", sorted(settings_module.NUMERIC_LIMITS))
def test_each_limit_falls_back_to_its_documented_default(stored, key):
    _floor, default, _description = settings_module.NUMERIC_LIMITS[key]
    assert settings_module.numeric_limit(key) == default


@pytest.mark.parametrize("key", sorted(settings_module.NUMERIC_LIMITS))
def test_a_saved_limit_is_read_back(stored, key):
    _floor, default, _description = settings_module.NUMERIC_LIMITS[key]
    settings_module.update_settings(**{key: default + 5})
    assert settings_module.numeric_limit(key) == default + 5


@pytest.mark.parametrize("key", sorted(settings_module.NUMERIC_LIMITS))
def test_the_environment_wins_over_a_saved_limit(stored, monkeypatch, key):
    _floor, default, _description = settings_module.NUMERIC_LIMITS[key]
    settings_module.update_settings(**{key: default + 5})
    monkeypatch.setenv(f"SHAMSU_{key.upper()}", str(default + 9))
    assert settings_module.numeric_limit(key) == default + 9


@pytest.mark.parametrize("key", sorted(settings_module.NUMERIC_LIMITS))
def test_a_value_below_the_floor_is_ignored_not_honoured(stored, key):
    floor, default, _description = settings_module.NUMERIC_LIMITS[key]
    settings_module.update_settings(**{key: floor - 1})
    assert settings_module.numeric_limit(key) == default


def test_an_unknown_setting_is_refused_rather_than_stored(stored):
    with pytest.raises(ValueError):
        settings_module.update_settings(nonsense=1)


def test_the_loop_reads_its_round_ceiling_from_settings(stored):
    from shamsu.agents.simple_chat import configured_max_rounds

    settings_module.update_settings(max_rounds=40)
    assert configured_max_rounds() == 40


def test_the_turn_budget_zero_still_means_no_limit(stored):
    from shamsu.agents.simple_chat import turn_budget_seconds

    settings_module.update_settings(turn_budget_s=0)
    assert turn_budget_seconds() == float("inf")


# -- a model running from system RAM, which nothing could see -----------------


def _spilling(monkeypatch, payload):
    """Pin what `/api/ps` answers."""
    import json as _json

    body = _json.dumps(payload).encode("utf-8")

    class _Response:
        def read(self):
            return body

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        capabilities.urllib.request, "urlopen", lambda *_a, **_k: _Response()
    )


def _resident(name, size_gb, vram_gb):
    return {"name": name, "size": int(size_gb * 1e9), "size_vram": int(vram_gb * 1e9)}


def test_a_model_partly_in_system_ram_is_detected(monkeypatch):
    """Ollama does not error when a model does not fit - it loads what it can
    and runs the rest from RAM. Measured 2026-08-31: 1.28GB outside VRAM and a
    single model call of 536 seconds, with the harness silent throughout."""
    _spilling(monkeypatch, {"models": [_resident("qwen3.5:9b-q4_K_M", 6.8, 5.5)]})
    spilled = capabilities.loaded_model_spill("http://pinned")
    assert spilled.get("qwen3.5:9b-q4_K_M", 0) > 1_000_000_000


def test_a_model_that_fits_is_not_reported(monkeypatch):
    _spilling(monkeypatch, {"models": [_resident("qwen2.5-coder:7b", 4.7, 4.7)]})
    assert capabilities.loaded_model_spill("http://pinned") == {}


def test_a_small_overhang_is_not_worth_saying(monkeypatch):
    """A warning that fires on 50MB is a warning that gets ignored."""
    _spilling(monkeypatch, {"models": [_resident("m:7b", 4.70, 4.65)]})
    assert capabilities.loaded_model_spill("http://pinned") == {}


def test_a_cpu_only_load_is_a_choice_not_a_spill(monkeypatch):
    """`size_vram` of 0 is a machine with no GPU, not a model that overflowed."""
    _spilling(monkeypatch, {"models": [_resident("m:7b", 4.7, 0)]})
    assert capabilities.loaded_model_spill("http://pinned") == {}


def test_no_server_means_no_claim(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(capabilities.urllib.request, "urlopen", refuse)
    assert capabilities.loaded_model_spill("http://pinned") == {}


def test_the_loop_says_so_and_says_what_to_do(monkeypatch, tmp_path):
    from shamsu.agents.simple_chat import SimpleChatLoop

    _spilling(monkeypatch, {"models": [_resident("qwen3.5:9b-q4_K_M", 6.8, 5.5)]})
    loop = SimpleChatLoop.__new__(SimpleChatLoop)
    loop.model_name = "qwen3.5:9b-q4_K_M"
    loop.workspace = tmp_path
    loop._ceiling = lambda: 32768
    said: list[str] = []
    loop._notice = said.append
    loop._warn_if_spilled_to_ram()
    assert said, "a spilled model must not be silent"
    message = said[0]
    assert "system RAM" in message
    # Naming the fix is the difference between a warning read once and one acted
    # on - the rule every other message in this harness follows.
    assert "close what else is using the GPU" in message
    assert "/context window 16384" in message


def test_a_model_that_fits_leaves_the_turn_quiet(monkeypatch, tmp_path):
    from shamsu.agents.simple_chat import SimpleChatLoop

    _spilling(monkeypatch, {"models": [_resident("qwen2.5-coder:7b", 4.7, 4.7)]})
    loop = SimpleChatLoop.__new__(SimpleChatLoop)
    loop.model_name = "qwen2.5-coder:7b"
    loop.workspace = tmp_path
    loop._ceiling = lambda: 32768
    said: list[str] = []
    loop._notice = said.append
    loop._warn_if_spilled_to_ram()
    assert said == []
