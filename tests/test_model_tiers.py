from __future__ import annotations

from shamsu.runtime.models import (
    DEFAULT_TIER,
    ModelTier,
    active_tier,
    active_model_override,
    clear_model_override,
    initialize_model_tier,
    model_for_role,
    required_model_names,
    set_model_override,
    set_model_tier,
    tier_model_specs,
)

# The DEFAULT tier's thinking anchor, read from the cookbook rather than
# hardcoded: swapping the anchor (qwen3:8b -> qwen3.5:9b-q4_K_M, 2026-08-18)
# broke 20 tests that had the old name baked in. The behaviour under test is
# "every role resolves to the ONE anchor", never "it is called qwen3:8b".
from shamsu.runtime.models import ModelTier, TIER_MODEL_SPECS

ANCHOR = TIER_MODEL_SPECS[ModelTier.DEFAULT][0].name


def test_default_tier_serves_every_role_from_one_model():
    """One model is now the default, not opt-in.

    Two anchors cost more than they bought on the 8GB target: they cannot be
    co-resident, so Ollama evicted and cold-loaded on every planner -> coder
    handoff - a model swap on every turn. qwen3:8b does native tool calls AND has
    a separate thinking channel, so one model covers planning and coding.
    """
    assert active_tier() is DEFAULT_TIER
    for role in ("qa", "coder", "router", "planner", "bugfix"):
        assert model_for_role(role) == ANCHOR, role
    assert required_model_names() == [ANCHOR]


def test_multi_model_mode_restores_the_two_anchor_layout(monkeypatch):
    """The escape hatch, kept working so the default is reversible."""
    monkeypatch.setenv("SHAMSU_MULTI_MODEL_MODE", "1")

    assert model_for_role("qa") == ANCHOR
    assert model_for_role("coder") == "qwen2.5-coder:7b-instruct"
    # In this mode the router still dodges the reasoning anchor by model choice.
    assert model_for_role("router") == "qwen2.5-coder:7b-instruct"


def test_shamsu_model_pins_any_model_for_every_role(monkeypatch):
    """One line of env reverts the model choice if it regresses code quality."""
    monkeypatch.setenv("SHAMSU_MODEL", "qwen2.5-coder:7b-instruct")

    for role in ("qa", "coder", "router", "planner"):
        assert model_for_role(role) == "qwen2.5-coder:7b-instruct", role
    assert required_model_names() == ["qwen2.5-coder:7b-instruct"]


def test_persisted_model_override_pins_any_installed_model_for_every_role(tmp_path):
    set_model_override(tmp_path, "llama3.1:8b")

    assert active_model_override() == "llama3.1:8b"
    for role in ("qa", "coder", "router", "planner"):
        assert model_for_role(role) == "llama3.1:8b", role
    assert required_model_names() == ["llama3.1:8b"]


def test_initialize_model_tier_reads_persisted_model_override(tmp_path):
    set_model_override(tmp_path, "mistral:7b")

    from shamsu.runtime import models as models_module

    models_module._ACTIVE_MODEL_OVERRIDE = ""
    initialize_model_tier(tmp_path)

    assert active_model_override() == "mistral:7b"
    assert model_for_role("coder") == "mistral:7b"


def test_clearing_model_override_returns_to_tier_selection(tmp_path):
    set_model_override(tmp_path, "llama3.1:8b")
    clear_model_override(tmp_path)

    assert active_model_override() == ""
    assert model_for_role("coder") == ANCHOR


def test_a_pin_outranks_multi_model_mode(monkeypatch):
    monkeypatch.setenv("SHAMSU_MULTI_MODEL_MODE", "1")
    monkeypatch.setenv("SHAMSU_MODEL", ANCHOR)

    assert model_for_role("coder") == ANCHOR


def test_default_thinking_anchor_does_native_tools_and_reasoning():
    from shamsu.runtime.models import model_is_reasoning, model_supports_native_tools

    assert model_supports_native_tools(ANCHOR) is True
    assert model_is_reasoning(ANCHOR) is True


def test_light_tier_uses_small_cpu_friendly_models(tmp_path):
    set_model_tier(tmp_path, ModelTier.LIGHT)

    # One small model for everything - the tier still scales the model down.
    assert model_for_role("router") == "qwen2.5:3b-instruct"
    assert model_for_role("coder") == "qwen2.5:3b-instruct"
    assert required_model_names() == ["qwen2.5:3b-instruct"]


def test_light_tier_multi_model_mode_still_pulls_both_anchors(tmp_path, monkeypatch):
    set_model_tier(tmp_path, ModelTier.LIGHT)
    monkeypatch.setenv("SHAMSU_MULTI_MODEL_MODE", "1")

    assert model_for_role("coder") == "qwen2.5-coder:3b-instruct"
    assert required_model_names() == ["qwen2.5:3b-instruct", "qwen2.5-coder:3b-instruct"]


def test_heavy_tier_caps_thinking_model_at_12b_and_allows_14b_coder(tmp_path):
    set_model_tier(tmp_path, ModelTier.HEAVY)

    specs = tier_model_specs()
    thinking = next(spec for spec in specs if "router" in spec.roles)
    coder = next(spec for spec in specs if "coder" in spec.roles)

    assert thinking.max_vram_gb <= 12.0
    assert coder.name == "qwen2.5-coder:14b"
    # The tier's coder anchor is still declared; under the single-model default the
    # shared model is the thinking anchor, so that is what a coding role resolves to.
    assert model_for_role("bugfix") == thinking.name


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


def test_the_router_never_pays_for_chain_of_thought_on_any_tier(tmp_path):
    """G13's intent, enforced per CALL now that one model serves every role.

    The router runs a schema-constrained JSON classification every turn, and
    chain-of-thought on that is pure latency. That used to be guaranteed by sending
    the router to a non-reasoning MODEL - impossible once a single reasoning model
    serves everything - so the guarantee moved to role_should_think(), which is the
    gate llm/manager actually consults before sending `think`.
    """
    from shamsu.runtime.models import role_should_think

    for tier in (ModelTier.LIGHT, ModelTier.DEFAULT, ModelTier.HEAVY):
        set_model_tier(tmp_path, tier)
        router = model_for_role("router")
        assert role_should_think("router", router) is False, tier
        assert router in required_model_names(), tier


def test_mechanical_roles_do_not_think_but_real_work_does():
    from shamsu.runtime.models import role_should_think

    for role in ("router", "classifier", "prd_headings", "prd_entities"):
        assert role_should_think(role, ANCHOR) is False, role
    for role in ("planner", "coder", "reviewer", "qa"):
        assert role_should_think(role, ANCHOR) is True, role


def test_a_non_reasoning_model_never_thinks_whatever_the_role():
    from shamsu.runtime.models import role_should_think

    assert role_should_think("planner", "qwen2.5-coder:7b-instruct") is False


# ---------------------------------------------------------------------------
# Gap B3: unknown models fell back to blanket defaults (tool-capable,
# non-reasoning). That was silently wrong for whole families: a pulled
# deepseek-r1:14b never got think=true, so it leaked <think> inline and the
# salvager cleaned up every turn, with no hint why.
# ---------------------------------------------------------------------------


def test_cookbook_specs_always_win_over_family_guesses():
    from shamsu.runtime.models import model_is_reasoning, model_supports_native_tools

    # gemma3:4b matches the no-native-tools family AND has an explicit spec.
    assert model_supports_native_tools("gemma3:4b") is False
    assert model_is_reasoning("gemma3:4b") is False
    assert model_supports_native_tools("qwen2.5-coder:7b-instruct") is True


def test_unknown_reasoning_models_are_recognized_by_family():
    from shamsu.runtime.models import is_allowed_model, model_is_reasoning

    for name in ("deepseek-r1:14b", "deepseek-r1:32b", "qwen3:14b", "qwq:32b"):
        assert not is_allowed_model(name), f"{name} should be off-cookbook for this test"
        assert model_is_reasoning(name) is True, name


def test_unknown_models_without_native_tools_are_recognized_by_family():
    from shamsu.runtime.models import model_supports_native_tools

    assert model_supports_native_tools("gemma3:12b") is False
    assert model_supports_native_tools("deepseek-r1:14b") is False


def test_unknown_models_with_no_family_match_keep_the_safe_defaults():
    """Assume tool-capable (the salvager backs it up; refusing a schema to a
    model that supports tools is the bigger regression) and non-reasoning
    (asking a model with no thinking mode costs a rejected request)."""
    from shamsu.runtime.models import model_is_reasoning, model_supports_native_tools

    for name in ("mistral:7b", "my-custom-model", ""):
        assert model_supports_native_tools(name) is True, name
        assert model_is_reasoning(name) is False, name
