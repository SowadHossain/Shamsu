"""
Shared local model defaults for SHAMSU.

Three hardware tiers, same role contract (router/qa/planner/... -> a
"thinking" model; coder/bugfix/tests/... -> a "coding" model):

  light   - 8GB RAM, no GPU. Tiny models, CPU-friendly.
  default - the 8GB cookbook (qwen3:8b + qwen2.5-coder:7b-instruct).
  heavy   - 16GB+ RAM machines. Coder is allowed to 14B since there is no
            dedicated ~12B code model; the thinking anchor stays at 12B.

Active tier is process-global (see `initialize_model_tier`/`set_model_tier`)
because `model_for_role()` is called from many places with no workspace
argument - the same pattern `single_model_mode_enabled()` already uses.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


@dataclass(frozen=True)
class ModelSpec:
    name: str
    roles: tuple[str, ...]
    required: bool = True
    max_vram_gb: float = 8.0
    notes: str = ""
    # Capability flags the agent loop uses instead of assuming every model does
    # native tool-calling / clean reasoning separation:
    #   supports_native_tools - pass a `tools=` schema and prefer native
    #     tool_calls (the output salvager still backs it up). Models that don't
    #     do native tools get a compact tool protocol in the prompt and rely on
    #     the salvager as the PRIMARY parser (passing them a schema confuses them).
    #   is_reasoning - a chain-of-thought model (deepseek-r1/qwen3); send
    #     think=true so reasoning separates into the `thinking` field instead of
    #     leaking inline <think> into the answer.
    supports_native_tools: bool = False
    is_reasoning: bool = False


class ModelTier(str, Enum):
    LIGHT = "light"
    DEFAULT = "default"
    HEAVY = "heavy"


_THINKING_ROLES = (
    "router",
    "qa",
    "planner",
    "classifier",
    "review",
    "reviewer",
    "doc_agent",
    "docs",
    "summarizer",
    "fallback_chat",
)
_CODING_ROLES = (
    "coder",
    "frontend",
    "backend",
    "test_gen",
    "test_agent",
    "tests",
    "bugfix",
    "bugfixer",
)

TIER_MODEL_SPECS: dict[ModelTier, tuple[ModelSpec, ...]] = {
    ModelTier.LIGHT: (
        ModelSpec(
            "qwen2.5:3b-instruct",
            _THINKING_ROLES,
            max_vram_gb=3.0,
            notes="Lightweight thinking/text anchor - runs on 8GB RAM, CPU-only machines.",
            supports_native_tools=True,
        ),
        ModelSpec(
            "qwen2.5-coder:3b-instruct",
            _CODING_ROLES,
            max_vram_gb=3.0,
            notes="Lightweight coding anchor - runs on 8GB RAM, CPU-only machines.",
            supports_native_tools=True,
        ),
    ),
    ModelTier.DEFAULT: (
        ModelSpec(
            "qwen3:8b",
            _THINKING_ROLES,
            max_vram_gb=8.0,
            notes="Thinking/text anchor for routing, planning, review, docs, and chat. "
            "Replaces deepseek-r1:7b: Qwen3 does native tool calling, so planner and "
            "router calls no longer depend on the output salvager, and it keeps a "
            "separate thinking channel. The coder anchor stays a qwen2.5-coder.",
            supports_native_tools=True,
            is_reasoning=True,
        ),
        ModelSpec(
            "qwen2.5-coder:7b-instruct",
            _CODING_ROLES,
            max_vram_gb=8.0,
            notes="Coding anchor for generation, tests, and repair loops.",
            supports_native_tools=True,
        ),
    ),
    ModelTier.HEAVY: (
        ModelSpec(
            "mistral-nemo:12b",
            _THINKING_ROLES,
            max_vram_gb=12.0,
            notes="Heavier thinking/text anchor for 16GB+ RAM machines.",
            supports_native_tools=True,
        ),
        ModelSpec(
            "qwen2.5-coder:14b",
            _CODING_ROLES,
            max_vram_gb=14.0,
            notes=(
                "Heavier coding anchor for 16GB+ RAM machines. Allowed above the "
                "12B thinking-model ceiling because Qwen2.5-Coder has no ~12B "
                "step (7B -> 14B)."
            ),
            supports_native_tools=True,
        ),
    ),
}

DEFAULT_TIER = ModelTier.DEFAULT

# Back-compat: code that imports MODEL_SPECS directly keeps getting the
# default tier's specs.
MODEL_SPECS: tuple[ModelSpec, ...] = TIER_MODEL_SPECS[ModelTier.DEFAULT]

# Recognized-but-not-anchor models: allowed (doctor won't flag them) and
# manageable (unload_shamsu_models knows them), but never auto-pulled and not a
# role anchor for any tier. Older default thinking anchors live here so they
# stay first-class known models for anyone who kept them installed.
_ALLOWED_EXTRA_SPECS: tuple[ModelSpec, ...] = (
    ModelSpec("deepseek-r1:7b", roles=(), required=False, max_vram_gb=8.0,
              notes="Former default thinking anchor; kept allowed for existing installs. "
                    "No native tool calling - every call relied on the output salvager.",
              supports_native_tools=False, is_reasoning=True),
    ModelSpec("gemma3:4b", roles=(), required=False, max_vram_gb=4.0,
              notes="Former default thinking anchor; kept allowed for existing installs.",
              supports_native_tools=False),
)

# Union across every tier + allowed extras - used for cookbook membership
# (is_allowed_model) and for identifying "models SHAMSU itself might have
# pulled" regardless of which tier is currently active (e.g.
# unload_shamsu_models after a tier switch, or a workspace that tried more than
# one tier).
ALL_MODEL_SPECS: tuple[ModelSpec, ...] = tuple(
    spec for specs in TIER_MODEL_SPECS.values() for spec in specs
) + _ALLOWED_EXTRA_SPECS
MODEL_COOKBOOK: dict[str, ModelSpec] = {spec.name: spec for spec in ALL_MODEL_SPECS}

TIER_FILENAME = "model_tier.json"
_ACTIVE_TIER: ModelTier = DEFAULT_TIER


def _tier_config_path(workspace: Path) -> Path:
    return workspace / ".shamsu" / TIER_FILENAME


def tier_ever_configured(workspace: Path) -> bool:
    """True once a tier has been explicitly chosen/persisted for `workspace`
    (via set_model_tier - the /models tier command or the first-run prompt).
    Used to decide whether to show the first-run tier picker."""
    return _tier_config_path(workspace).exists()


def _read_persisted_tier(workspace: Path) -> ModelTier | None:
    path = _tier_config_path(workspace)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return ModelTier(str(data.get("tier", "")).strip().lower())
    except ValueError:
        return None


def set_model_tier(workspace: Path, tier: ModelTier) -> None:
    """Persist `tier` for `workspace` and make it active immediately for the
    rest of this process - no restart needed."""
    global _ACTIVE_TIER
    path = _tier_config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tier": tier.value}, indent=2), encoding="utf-8")
    _ACTIVE_TIER = tier


def initialize_model_tier(workspace: Path) -> ModelTier:
    """Resolve the active tier once at process startup:
    SHAMSU_MODEL_TIER env var > persisted workspace choice > default."""
    global _ACTIVE_TIER
    env_value = os.environ.get("SHAMSU_MODEL_TIER", "").strip().lower()
    if env_value:
        try:
            _ACTIVE_TIER = ModelTier(env_value)
            return _ACTIVE_TIER
        except ValueError:
            pass  # invalid env value - fall through to persisted/default
    _ACTIVE_TIER = _read_persisted_tier(workspace) or DEFAULT_TIER
    return _ACTIVE_TIER


def active_tier() -> ModelTier:
    return _ACTIVE_TIER


def tier_model_specs(tier: ModelTier | None = None) -> tuple[ModelSpec, ...]:
    return TIER_MODEL_SPECS[tier or _ACTIVE_TIER]


def _role_models_for_tier(tier: ModelTier) -> dict[str, str]:
    return {role: spec.name for spec in TIER_MODEL_SPECS[tier] for role in spec.roles}


def _thinking_model_for_tier(tier: ModelTier) -> str:
    # The first spec in each tier tuple is the thinking/router anchor by
    # construction (see TIER_MODEL_SPECS above).
    return TIER_MODEL_SPECS[tier][0].name


def _coding_model_for_tier(tier: ModelTier) -> str:
    # The second spec in each tier tuple is the coding/instruct anchor.
    return TIER_MODEL_SPECS[tier][1].name


def single_model_mode_enabled() -> bool:
    value = os.environ.get("SHAMSU_SINGLE_MODEL_MODE", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def model_for_role(role: str) -> str:
    tier = active_tier()
    if single_model_mode_enabled():
        return _thinking_model_for_tier(tier)
    if role == "router":
        # The router runs a schema-constrained JSON classification every turn.
        # Run it on the tier's fast INSTRUCT (coding) anchor instead of the
        # reasoning anchor (deepseek-r1 / mistral-nemo), whose per-turn
        # chain-of-thought is pure latency for pure classification. The coding
        # anchor is already required/pulled, so this costs no extra model.
        return _coding_model_for_tier(tier)
    return _role_models_for_tier(tier).get(role, _thinking_model_for_tier(tier))


def model_spec(model_name: str) -> ModelSpec | None:
    """The cookbook :class:`ModelSpec` for *model_name*, or None if it is a
    user-supplied model SHAMSU doesn't recognize."""
    return MODEL_COOKBOOK.get(model_name)


# Name patterns for models that are NOT in the cookbook. A user who pulls
# `deepseek-r1:14b` or `gemma3:12b` gets the right treatment from the family
# name instead of the blanket default, which was silently wrong for them:
# deepseek-r1 never got `think=true` (so it leaked <think> inline and the
# salvager cleaned up every turn), and gemma got a `tools=` schema it handles
# badly. Family names are stable and few; this is a better guess than a
# constant, and an explicit ModelSpec still always wins.
_REASONING_NAME_PATTERNS = ("deepseek-r1", "qwen3", "qwq", "magistral", "phi4-reasoning")
_NO_NATIVE_TOOLS_NAME_PATTERNS = ("gemma", "deepseek-r1", "llava", "phi3", "codellama")


def _matches_family(model_name: str, patterns: tuple[str, ...]) -> bool:
    lowered = model_name.strip().lower()
    return any(pattern in lowered for pattern in patterns)


def model_supports_native_tools(model_name: str) -> bool:
    """Whether to pass a native ``tools=`` schema to *model_name*.

    Known models use their explicit ``ModelSpec`` flag. For an unrecognized
    model, fall back to its family name (gemma/deepseek-r1/… don't do native
    tools); anything still unknown is assumed tool-capable — the output salvager
    backs it up either way, and refusing to pass a schema to a model that
    actually supports tools would be the bigger regression."""
    spec = MODEL_COOKBOOK.get(model_name)
    if spec is not None:
        return spec.supports_native_tools
    return not _matches_family(model_name, _NO_NATIVE_TOOLS_NAME_PATTERNS)


def model_is_reasoning(model_name: str) -> bool:
    """Whether *model_name* is a chain-of-thought model that should be asked to
    ``think`` so its reasoning separates into the ``thinking`` field.

    Known models use their explicit ``ModelSpec`` flag; an unrecognized model is
    matched on its family name (deepseek-r1/qwen3/qwq/…). Anything still unknown
    is assumed non-reasoning — asking a model without a thinking mode to think
    costs a rejected request, and the manager only retries once per model."""
    spec = MODEL_COOKBOOK.get(model_name)
    if spec is not None:
        return spec.is_reasoning
    return _matches_family(model_name, _REASONING_NAME_PATTERNS)


def is_allowed_model(model_name: str) -> bool:
    return model_name in MODEL_COOKBOOK


def allowed_model_names() -> list[str]:
    return list(MODEL_COOKBOOK)


def required_model_names(tier: ModelTier | None = None) -> list[str]:
    resolved_tier = tier or active_tier()
    if single_model_mode_enabled():
        return [_thinking_model_for_tier(resolved_tier)]
    return [spec.name for spec in TIER_MODEL_SPECS[resolved_tier] if spec.required]


_ROLE_MODELS: dict[str, str] = _role_models_for_tier(DEFAULT_TIER)

# Back-compat import surface: a static snapshot of the *default* tier's role
# map, for code/tests that only read the default layout. Dynamic,
# tier-sensitive resolution should call model_for_role() instead.
SPECIALIST_MODELS: dict[str, str] = dict(_ROLE_MODELS)
