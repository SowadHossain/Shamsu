"""
Shared local model defaults for SHAMSU.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    roles: tuple[str, ...]
    required: bool = True
    max_vram_gb: float = 8.0
    notes: str = ""


MODEL_SPECS: tuple[ModelSpec, ...] = (
    ModelSpec(
        "qwen3:8b",
        (
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
        ),
        notes="Thinking/text anchor for routing, planning, review, docs, and chat.",
    ),
    ModelSpec(
        "qwen2.5-coder:7b-instruct",
        (
            "coder",
            "frontend",
            "backend",
            "test_gen",
            "test_agent",
            "tests",
            "bugfix",
            "bugfixer",
        ),
        notes="Coding anchor for generation, tests, and repair loops.",
    ),
)

MODEL_COOKBOOK: dict[str, ModelSpec] = {spec.name: spec for spec in MODEL_SPECS}
THINKING_MODEL = "qwen3:8b"
CODING_MODEL = "qwen2.5-coder:7b-instruct"


def single_model_mode_enabled() -> bool:
    value = os.environ.get("SHAMSU_SINGLE_MODEL_MODE", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def model_for_role(role: str) -> str:
    if single_model_mode_enabled():
        return THINKING_MODEL
    return _ROLE_MODELS.get(role, THINKING_MODEL)


def is_allowed_model(model_name: str) -> bool:
    return model_name in MODEL_COOKBOOK


def allowed_model_names() -> list[str]:
    return list(MODEL_COOKBOOK)


def required_model_names() -> list[str]:
    if single_model_mode_enabled():
        return [THINKING_MODEL]
    return [spec.name for spec in MODEL_SPECS if spec.required]


_ROLE_MODELS: dict[str, str] = {
    role: spec.name
    for spec in MODEL_SPECS
    for role in spec.roles
}

# Backward-compatible import surface. Code that needs env-sensitive resolution
# should call model_for_role(); existing callers that only read the default map
# still get the two-anchor layout.
SPECIALIST_MODELS: dict[str, str] = {
    role: model_for_role(role)
    for role in _ROLE_MODELS
}
