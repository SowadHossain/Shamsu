from __future__ import annotations

import json
from pathlib import Path

from shamsu.runtime.state_upgrade import STATE_SCHEMA_VERSION, upgrade_workspace_state


def test_upgrade_initializes_marker_and_is_idempotent(tmp_path: Path):
    first = upgrade_workspace_state(tmp_path)
    marker = tmp_path / ".shamsu" / "state.json"
    initial_text = marker.read_text(encoding="utf-8")

    second = upgrade_workspace_state(tmp_path)

    assert first.initialized is True
    assert json.loads(initial_text)["schema_version"] == STATE_SCHEMA_VERSION
    assert second.actions == ()
    assert marker.read_text(encoding="utf-8") == initial_text


def test_upgrade_normalizes_legacy_permissions_and_drops_unsafe_actions(tmp_path: Path):
    path = tmp_path / ".shamsu" / "permissions.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"always_allow": ["file_write", "run_command", "file_edit"]}),
        encoding="utf-8",
    )

    report = upgrade_workspace_state(tmp_path)

    assert "permissions:normalized" in report.actions
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "schema_version": STATE_SCHEMA_VERSION,
        "always_allow": ["file_edit", "file_write"],
    }


def test_upgrade_does_not_rewrite_historical_runs(tmp_path: Path):
    event_path = tmp_path / ".shamsu" / "runs" / "run_old" / "events.jsonl"
    event_path.parent.mkdir(parents=True)
    event_path.write_text('{"schema_version": 1}\n', encoding="utf-8")

    upgrade_workspace_state(tmp_path)

    assert event_path.read_text(encoding="utf-8") == '{"schema_version": 1}\n'


def test_upgrade_refuses_newer_schema(tmp_path: Path):
    marker = tmp_path / ".shamsu" / "state.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")

    report = upgrade_workspace_state(tmp_path)

    assert report.actions == ()
    assert report.warnings
    assert json.loads(marker.read_text(encoding="utf-8"))["schema_version"] == 99
