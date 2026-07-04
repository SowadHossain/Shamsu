"""Opt-in toggle for the long-running autonomous executor (Milestone G).

Defaults OFF: AgentChatLoop, ErrorFeedbackLoop, and FullDjangoPipeline keep
their existing hard round/iteration caps and stop-on-first-failure behavior
unless a workspace has explicitly enabled long-running mode. This lets the
higher ceilings, repetition guard, and stall detection be tried and trusted
before ever becoming the default for a given workspace.
"""
from __future__ import annotations

import json
from pathlib import Path

from shamsu.safety.sandbox import Sandbox

AUTONOMY_FILENAME = "autonomy.json"


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
    path.write_text(json.dumps({"long_running_enabled": enabled}, indent=2), encoding="utf-8")
