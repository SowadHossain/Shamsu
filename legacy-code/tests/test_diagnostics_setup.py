from __future__ import annotations

from pathlib import Path

from shamsu.diagnostics import setup as diagnostics_setup
from shamsu.diagnostics.setup import DiagnosticsWorkspace


def test_setup_writes_default_config_and_does_not_touch_network_when_drain3_available(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(diagnostics_setup.drain3_compactor, "is_available", lambda: True)

    result = diagnostics_setup.setup(tmp_path)

    assert result["ok"] is True
    assert result["steps"][0]["tool"] == "drain3"
    assert result["steps"][0]["ok"] is True
    config_path = tmp_path / ".shamsu" / "diagnostics" / "config.json"
    assert config_path.exists()


def test_setup_attempts_local_pip_install_when_drain3_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(diagnostics_setup.drain3_compactor, "is_available", lambda: False)
    calls = []

    def fake_pip_install(package):
        calls.append(package)
        return {"ok": True, "message": "installed"}

    monkeypatch.setattr(diagnostics_setup, "_pip_install", fake_pip_install)

    result = diagnostics_setup.setup(tmp_path)

    assert calls == ["drain3"]
    assert result["ok"] is True


def test_setup_never_installs_llmlingua_automatically(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(diagnostics_setup.drain3_compactor, "is_available", lambda: True)
    calls = []
    monkeypatch.setattr(diagnostics_setup, "_pip_install", lambda package: calls.append(package) or {"ok": True})

    diagnostics_setup.setup(tmp_path)

    assert "llmlingua" not in calls


def test_diagnostics_workspace_save_and_load_last_packet(tmp_path: Path):
    ws = DiagnosticsWorkspace(tmp_path)
    assert ws.last_packet() is None

    ws.save_packet({"command": "tsc", "exit_code": 1})

    assert ws.last_packet() == {"command": "tsc", "exit_code": 1}
    events = (tmp_path / ".shamsu" / "diagnostics" / "diagnostic-events.jsonl").read_text(encoding="utf-8")
    assert "tsc" in events


def test_load_config_merges_defaults_with_saved_config(tmp_path: Path):
    ws = DiagnosticsWorkspace(tmp_path)

    assert ws.load_config() == {"enable_llmlingua": False}

    ws._write_json(ws._config_path(), {"enable_llmlingua": True})

    assert ws.load_config() == {"enable_llmlingua": True}


def test_status_reports_config_and_helper_flags(tmp_path: Path):
    payload = diagnostics_setup.status(tmp_path)

    assert payload["config"] == {"enable_llmlingua": False}
    assert (tmp_path / ".shamsu" / "diagnostics" / "status.json").exists()
