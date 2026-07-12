from __future__ import annotations

from shamsu.runtime.models import (
    DEFAULT_TIER,
    ModelTier,
    active_tier,
    initialize_model_tier,
    model_for_role,
    required_model_names,
    set_model_tier,
    tier_model_specs,
)


def test_default_tier_uses_deepseek_r1_for_thinking_and_qwen_coder_for_code():
    assert active_tier() is DEFAULT_TIER
    # Thinking/qa/planner roles run on DeepSeek R1 Distill Qwen 7B; coding stays
    # qwen2.5-coder. The router runs on the fast instruct (coding) anchor to skip
    # the reasoning model's per-turn chain-of-thought (G13).
    assert model_for_role("qa") == "deepseek-r1:7b"
    assert model_for_role("coder") == "qwen2.5-coder:7b-instruct"
    assert model_for_role("router") == "qwen2.5-coder:7b-instruct"


def test_light_tier_uses_small_cpu_friendly_models(tmp_path):
    set_model_tier(tmp_path, ModelTier.LIGHT)

    # Router runs on the small instruct (coding) anchor, not the thinking anchor.
    assert model_for_role("router") == "qwen2.5-coder:3b-instruct"
    assert model_for_role("coder") == "qwen2.5-coder:3b-instruct"
    assert required_model_names() == ["qwen2.5:3b-instruct", "qwen2.5-coder:3b-instruct"]


def test_heavy_tier_caps_thinking_model_at_12b_and_allows_14b_coder(tmp_path):
    set_model_tier(tmp_path, ModelTier.HEAVY)

    specs = tier_model_specs()
    thinking = next(spec for spec in specs if "router" in spec.roles)
    coder = next(spec for spec in specs if "coder" in spec.roles)

    assert thinking.max_vram_gb <= 12.0
    assert coder.name == "qwen2.5-coder:14b"
    assert model_for_role("bugfix") == "qwen2.5-coder:14b"


def test_set_model_tier_persists_and_takes_effect_immediately(tmp_path):
    set_model_tier(tmp_path, ModelTier.HEAVY)

    assert active_tier() is ModelTier.HEAVY
    assert (tmp_path / ".shamsu" / "model_tier.json").exists()


def test_initialize_model_tier_reads_persisted_workspace_choice(tmp_path):
    set_model_tier(tmp_path, ModelTier.LIGHT)

    # Simulate a fresh process: reset, then re-resolve from the same workspace.
    from shamsu.runtime import models as models_module

    models_module._ACTIVE_TIER = DEFAULT_TIER
    resolved = initialize_model_tier(tmp_path)

    assert resolved is ModelTier.LIGHT
    assert active_tier() is ModelTier.LIGHT


def test_initialize_model_tier_env_var_overrides_persisted_choice(tmp_path, monkeypatch):
    set_model_tier(tmp_path, ModelTier.LIGHT)
    monkeypatch.setenv("SHAMSU_MODEL_TIER", "heavy")

    resolved = initialize_model_tier(tmp_path)

    assert resolved is ModelTier.HEAVY


def test_initialize_model_tier_defaults_when_nothing_set(tmp_path):
    resolved = initialize_model_tier(tmp_path)

    assert resolved is DEFAULT_TIER


def test_initialize_model_tier_ignores_invalid_env_value(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_MODEL_TIER", "ultra-mega")

    resolved = initialize_model_tier(tmp_path)

    assert resolved is DEFAULT_TIER


def test_single_model_mode_uses_active_tiers_thinking_model(tmp_path, monkeypatch):
    set_model_tier(tmp_path, ModelTier.LIGHT)
    monkeypatch.setenv("SHAMSU_SINGLE_MODEL_MODE", "1")

    assert model_for_role("coder") == "qwen2.5:3b-instruct"
    assert required_model_names() == ["qwen2.5:3b-instruct"]


def test_router_avoids_the_reasoning_anchor_on_every_tier(tmp_path):
    # G13: the router must not run on the reasoning/thinking anchor (which would
    # add per-turn chain-of-thought latency); it uses the instruct coding anchor,
    # which is already required so no extra model is pulled.
    from shamsu.runtime.models import model_is_reasoning

    for tier in (ModelTier.LIGHT, ModelTier.DEFAULT, ModelTier.HEAVY):
        set_model_tier(tmp_path, tier)
        router = model_for_role("router")
        assert router == model_for_role("coder")
        assert model_is_reasoning(router) is False
        assert router in required_model_names()
