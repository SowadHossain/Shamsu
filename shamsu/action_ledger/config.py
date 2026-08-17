"""Workspace-local ActionLedger config: <workspace>/.shamsu/action_ledger/config.json.

Mirrors the `.shamsu/<feature>/config.json` pattern used by
shamsu.diagnostics.setup.DiagnosticsWorkspace and shamsu.memory.service -
JSON file, defaults merged in, no schema library.

`log_level` selects one of two human-reviewable modes:

    essential - the default. A concise Markdown report plus compact machine
                evidence. Per-model prompt, reasoning, response, and context
                files are not written.
    verbose   - a detailed Markdown report plus full redacted evidence for
                deep debugging.

The legacy names `compact` and `full` remain accepted as aliases for
`essential` and `verbose`. `SHAMSU_LOG_LEVEL` overrides the stored value for
one process.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

LOG_LEVELS = ("essential", "verbose")
LOG_LEVEL_ALIASES = {"compact": "essential", "full": "verbose"}
DEFAULT_LOG_LEVEL = "essential"
LOG_LEVEL_ENV_VAR = "SHAMSU_LOG_LEVEL"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "log_level": DEFAULT_LOG_LEVEL,
    "log_model_prompts": True,
    "log_model_responses": True,
    "log_context_preview": True,
    "friendly_model_transcript": True,
    "max_inline_event_size": 4000,
    "retention_days": 30,
    "redact_secrets": True,
    "debug_full_trace": True,
}


def ledger_dir(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".shamsu" / "action_ledger"


def config_path(workspace: Path) -> Path:
    return ledger_dir(workspace) / "config.json"


def resolve_log_level(config: dict[str, Any] | None = None) -> str:
    """Env wins over the stored value; an unrecognized value falls back to the
    default rather than silently disabling artifact capture."""
    from_env = os.environ.get(LOG_LEVEL_ENV_VAR, "").strip().lower()
    from_env = LOG_LEVEL_ALIASES.get(from_env, from_env)
    if from_env in LOG_LEVELS:
        return from_env
    stored = str((config or {}).get("log_level", DEFAULT_LOG_LEVEL)).strip().lower()
    stored = LOG_LEVEL_ALIASES.get(stored, stored)
    return stored if stored in LOG_LEVELS else DEFAULT_LOG_LEVEL


def wants_full_artifacts(config: dict[str, Any] | None = None) -> bool:
    """True when full prompt/reasoning/response evidence should be retained."""
    return resolve_log_level(config) == "verbose"


def wants_verbose_report(config: dict[str, Any] | None = None) -> bool:
    """True when the human-readable report should include deep details."""
    return resolve_log_level(config) == "verbose"


def load_config(workspace: Path) -> dict[str, Any]:
    path = config_path(workspace)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    merged = {**DEFAULT_CONFIG, **data}
    merged["log_level"] = resolve_log_level(merged)
    return merged


def save_config(workspace: Path, config: dict[str, Any]) -> None:
    path = config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = {**DEFAULT_CONFIG, **config}
    path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
