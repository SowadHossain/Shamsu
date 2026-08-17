"""Typed timeout configuration and diagnostics for production runtime calls."""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Any


class TimeoutCategory(str, Enum):
    CONNECT_TIMEOUT = "CONNECT_TIMEOUT"
    FIRST_TOKEN_TIMEOUT = "FIRST_TOKEN_TIMEOUT"
    TOKEN_IDLE_TIMEOUT = "TOKEN_IDLE_TIMEOUT"
    TOTAL_GENERATION_TIMEOUT = "TOTAL_GENERATION_TIMEOUT"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    STEP_TIMEOUT = "STEP_TIMEOUT"
    TASK_TIMEOUT = "TASK_TIMEOUT"


class ShamsuTimeoutError(TimeoutError):
    def __init__(self, category: TimeoutCategory, seconds: float, message: str = "") -> None:
        self.category = category
        self.seconds = float(seconds)
        super().__init__(message or f"{category.value} after {seconds:.0f}s")


@dataclass(frozen=True)
class TimeoutConfig:
    # Sized for a local 9B on a 32k window. Prefill dominates first-token time
    # and grows with the prompt, so a 180s first-token bound started failing
    # runs that were working, just slowly. The task budget has to cover the
    # write AND its verification - a build that wrote correctly and then timed
    # out before proving it reports as a failure and loses the evidence.
    connect_timeout: float = 15.0
    first_token_timeout: float = 240.0
    token_idle_timeout: float = 180.0
    total_generation_timeout: float = 0.0
    tool_timeout: float = 120.0
    step_timeout: float = 0.0
    task_timeout: float = 900.0
    min_model_call_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> "TimeoutConfig":
        old_model_timeout = _env_float("SHAMSU_MODEL_TIMEOUT_SECONDS", 0.0)
        return cls(
            connect_timeout=_env_float("SHAMSU_CONNECT_TIMEOUT_SECONDS", _env_float("SHAMSU_LLM_CONNECT_TIMEOUT", 15.0)),
            first_token_timeout=_env_float("SHAMSU_FIRST_TOKEN_TIMEOUT_SECONDS", old_model_timeout or 240.0),
            token_idle_timeout=_env_float("SHAMSU_TOKEN_IDLE_TIMEOUT_SECONDS", _env_float("SHAMSU_LLM_IDLE_TIMEOUT", 180.0)),
            total_generation_timeout=_env_float("SHAMSU_TOTAL_GENERATION_TIMEOUT_SECONDS", 0.0),
            tool_timeout=_env_float("SHAMSU_TOOL_TIMEOUT_SECONDS", 120.0),
            step_timeout=_env_float("SHAMSU_STEP_TIMEOUT_SECONDS", 0.0),
            task_timeout=_env_float("SHAMSU_TASK_TIMEOUT_SECONDS", _env_float("SHAMSU_RUN_TIMEOUT_SECONDS", 900.0)),
            min_model_call_seconds=_env_float("SHAMSU_MIN_MODEL_CALL_SECONDS", 60.0),
        )

    def diagnostics(self) -> dict[str, float]:
        return {
            "connect_timeout": self.connect_timeout,
            "first_token_timeout": self.first_token_timeout,
            "token_idle_timeout": self.token_idle_timeout,
            "total_generation_timeout": self.total_generation_timeout,
            "tool_timeout": self.tool_timeout,
            "step_timeout": self.step_timeout,
            "task_timeout": self.task_timeout,
            "min_model_call_seconds": self.min_model_call_seconds,
        }


def timeout_failure_detail(category: TimeoutCategory, *, seconds: float, layer: str, **extra: Any) -> dict[str, Any]:
    return {
        "timeout_category": category.value,
        "timeout_seconds": float(seconds),
        "timeout_layer": layer,
        **extra,
    }


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        if raw == "":
            return default
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default
