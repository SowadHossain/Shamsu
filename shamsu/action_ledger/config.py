"""Workspace-local ActionLedger config: <workspace>/.shamsu/action_ledger/config.json.

Mirrors the `.shamsu/<feature>/config.json` pattern used by
shamsu.diagnostics.setup.DiagnosticsWorkspace and shamsu.memory.service -
JSON file, defaults merged in, no schema library.

`log_level` picks how much of each model call survives to disk:

    full     - the complete prompt (system + messages + tool schemas), the
               complete chain-of-thought, and the complete response are each
               written to their own file under the run directory. The default:
               a debug log that omits the prompt cannot explain a bad answer.
    compact  - inline previews only, no artifact files. Smaller runs.

`SHAMSU_LOG_LEVEL` overrides the stored value for one process, so dropping to
`compact` never requires editing a file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

LOG_LEVELS = ("full", "compact")
DEFAULT_LOG_LEVEL = "full"
LOG_LEVEL_ENV_VAR = "SHAMSU_LOG_LEVEL"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "log_level": DEFAULT_LOG_LEVEL,
    "log_model_prompts": True,
    "log_model_responses": True,
    "log_context_preview": True,
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
    if from_env in LOG_LEVELS:
        return from_env
    stored = str((config or {}).get("log_level", DEFAULT_LOG_LEVEL)).strip().lower()
    return stored if stored in LOG_LEVELS else DEFAULT_LOG_LEVEL


def wants_full_artifacts(config: dict[str, Any] | None = None) -> bool:
    """True when full prompt/CoT/response text should be spilled to files."""
    return resolve_log_level(config) == "full"


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
