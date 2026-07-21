"""Idempotent upgrades for workspace-local SHAMSU state.

Historical run and session evidence is never rewritten. Only the workspace
schema marker and the small persisted approval-policy document are migrated.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from shamsu.safety.commands import is_auto_approvable_action

STATE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class UpgradeReport:
    previous_version: int
    current_version: int
    initialized: bool
    actions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def upgrade_workspace_state(workspace: Path) -> UpgradeReport:
    root = Path(workspace).resolve() / ".shamsu"
    marker = root / "state.json"
    actions: list[str] = []
    warnings: list[str] = []
    previous = _read_version(marker, warnings)
    if previous > STATE_SCHEMA_VERSION:
        warnings.append(
            f"Workspace state schema {previous} is newer than supported schema "
            f"{STATE_SCHEMA_VERSION}; no state was changed."
        )
        return UpgradeReport(previous, STATE_SCHEMA_VERSION, False, warnings=tuple(warnings))

    if previous < STATE_SCHEMA_VERSION:
        _upgrade_permissions(root / "permissions.json", actions, warnings)
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "upgraded_from": previous,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        actions.append(f"workspace_state:{previous}->{STATE_SCHEMA_VERSION}")
    return UpgradeReport(
        previous,
        STATE_SCHEMA_VERSION,
        previous == 0,
        tuple(actions),
        tuple(warnings),
    )


def read_state_schema_version(workspace: Path) -> int | None:
    marker = Path(workspace).resolve() / ".shamsu" / "state.json"
    if not marker.exists():
        return None
    warnings: list[str] = []
    return _read_version(marker, warnings)


def _read_version(path: Path, warnings: list[str]) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return int(payload.get("schema_version", 0)) if isinstance(payload, dict) else 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        warnings.append(f"Could not read {path.name}: {exc}")
        return 0


def _upgrade_permissions(path: Path, actions: list[str], warnings: list[str]) -> None:
    if not path.exists():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warnings.append(f"Left invalid permissions.json unchanged: {exc}")
        return
    if not isinstance(payload, dict):
        warnings.append("Left permissions.json unchanged because it is not a JSON object.")
        return
    remembered = payload.get("always_allow", [])
    if not isinstance(remembered, list):
        warnings.append("Discarded malformed always_allow value while upgrading permissions.json.")
        remembered = []
    safe = sorted(
        {
            str(action)
            for action in remembered
            if isinstance(action, str) and is_auto_approvable_action(action)
        }
    )
    path.write_text(
        json.dumps({"schema_version": STATE_SCHEMA_VERSION, "always_allow": safe}, indent=2),
        encoding="utf-8",
    )
    actions.append("permissions:normalized")
