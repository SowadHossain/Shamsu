"""
Shared local model defaults for SHAMSU.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    roles: tuple[str, ...]
    required: bool = True


MODEL_SPECS: tuple[ModelSpec, ...] = (
    ModelSpec("phi3:mini-4k-instruct", ("router", "qa", "planner", "doc_agent", "summarizer")),
    ModelSpec("qwen2.5-coder:7b-instruct-q4_K_M", ("coder", "test_gen", "test_agent")),
    ModelSpec("deepseek-coder:6.7b-instruct-q4_K_M", ("bugfix", "bugfixer")),
    ModelSpec("mistral:7b-instruct-q4_K_M", ("reviewer",)),
)

SPECIALIST_MODELS: dict[str, str] = {
    role: spec.name
    for spec in MODEL_SPECS
    for role in spec.roles
}


def required_model_names() -> list[str]:
    return [spec.name for spec in MODEL_SPECS if spec.required]
