"""Workspace-local ActionLedger config: <workspace>/.shamsu/action_ledger/config.json.

Mirrors the `.shamsu/<feature>/config.json` pattern used by
shamsu.diagnostics.setup.DiagnosticsWorkspace and shamsu.memory.service -
JSON file, defaults merged in, no schema library.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "log_model_prompts": False,
    "log_model_responses": True,
    "log_context_preview": True,
    "max_inline_event_size": 4000,
    "retention_days": 30,
    "redact_secrets": True,
    "debug_full_trace": False,
}


def ledger_dir(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".shamsu" / "action_ledger"


def config_path(workspace: Path) -> Path:
    return ledger_dir(workspace) / "config.json"


def load_config(workspace: Path) -> dict[str, Any]:
    path = config_path(workspace)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {**DEFAULT_CONFIG, **data}


def save_config(workspace: Path, config: dict[str, Any]) -> None:
    path = config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**DEFAULT_CONFIG, **config}
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
