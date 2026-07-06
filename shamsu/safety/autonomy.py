"""Opt-in toggle for the long-running autonomous executor (Milestone G).

Defaults OFF: AgentChatLoop, ErrorFeedbackLoop, and FullDjangoPipeline keep
their existing hard round/iteration caps and stop-on-first-failure behavior
unless a workspace has explicitly enabled long-running mode. This lets the
higher ceilings, repetition guard, and stall detection be tried and trusted
before ever becoming the default for a given workspace.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from shamsu.safety.sandbox import Sandbox

AUTONOMY_FILENAME = "autonomy.json"


@dataclass(frozen=True)
class AutonomyLimits:
    max_iterations: int = 3
    max_wall_time_minutes: int = 10
    max_same_error_retries: int = 2
    max_idle_minutes: int = 3
    max_repair_attempts: int = 3


DEFAULT_LIMITS = AutonomyLimits()
LONG_RUNNING_LIMITS = AutonomyLimits(
    max_iterations=50,
    max_wall_time_minutes=60,
    max_same_error_retries=3,
    max_idle_minutes=5,
    max_repair_attempts=25,
)


def _autonomy_config_path(workspace: Path) -> Path:
    return Sandbox(workspace).validate(Path(".shamsu") / AUTONOMY_FILENAME)


def is_long_running_enabled(workspace: Path) -> bool:
    path = _autonomy_config_path(workspace)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("long_running_enabled", False))


def set_long_running_enabled(workspace: Path, enabled: bool) -> None:
    path = _autonomy_config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    limits = LONG_RUNNING_LIMITS if enabled else DEFAULT_LIMITS
    path.write_text(
        json.dumps({"long_running_enabled": enabled, "limits": asdict(limits)}, indent=2),
        encoding="utf-8",
    )


def autonomy_limits(workspace: Path) -> AutonomyLimits:
    path = _autonomy_config_path(workspace)
    defaults = LONG_RUNNING_LIMITS if is_long_running_enabled(workspace) else DEFAULT_LIMITS
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    raw = data.get("limits", {})
    if not isinstance(raw, dict):
        return defaults
    values = asdict(defaults)
    for key in values:
        try:
            values[key] = max(1, int(raw.get(key, values[key])))
        except (TypeError, ValueError):
            pass
    return AutonomyLimits(**values)
