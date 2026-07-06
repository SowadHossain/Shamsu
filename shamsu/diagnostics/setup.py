"""Workspace-level bookkeeping for the diagnostics layer, and best-effort
local setup of optional helpers (Drain3).

Mirrors the `.shamsu/<name>/` service pattern used by
`shamsu.abstract.service.AbstractService` and `shamsu.memory.service.MemoryService`:
status.json / config.json / an events log, all workspace-local, no global
installs, no cloud services.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from shamsu.diagnostics.adapters import drain3_compactor, llmlingua_optional, reviewdog_errorformat

DEFAULT_CONFIG = {
    "enable_llmlingua": False,
}


class DiagnosticsWorkspace:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.diagnostics_dir = self.workspace / ".shamsu" / "diagnostics"

    def _status_path(self) -> Path:
        return self.diagnostics_dir / "status.json"

    def _config_path(self) -> Path:
        return self.diagnostics_dir / "config.json"

    def _last_packet_path(self) -> Path:
        return self.diagnostics_dir / "last-error-packet.json"

    def _events_path(self) -> Path:
        return self.diagnostics_dir / "diagnostic-events.jsonl"

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def load_config(self) -> dict[str, Any]:
        config = self._read_json(self._config_path())
        return {**DEFAULT_CONFIG, **config}

    def log_event(self, event: str, detail: dict[str, Any]) -> None:
        self.diagnostics_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"event": event, "ts": time.time(), **detail})
        with self._events_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def save_packet(self, packet_dict: dict[str, Any]) -> None:
        self._write_json(self._last_packet_path(), packet_dict)
        self.log_event("packet", {"command": packet_dict.get("command", ""), "exit_code": packet_dict.get("exit_code")})

    def last_packet(self) -> dict[str, Any] | None:
        if not self._last_packet_path().exists():
            return None
        return self._read_json(self._last_packet_path())


def setup(workspace: Path) -> dict[str, Any]:
    """Best-effort local install of optional helpers. Drain3 is pure-Python
    and small, so it's installed into the current environment on request;
    LLMLingua is never auto-installed (heavy ML deps, disabled by default)."""
    ws = DiagnosticsWorkspace(workspace)
    ws.diagnostics_dir.mkdir(parents=True, exist_ok=True)
    if not ws._config_path().exists():
        ws._write_json(ws._config_path(), DEFAULT_CONFIG)

    result: dict[str, Any] = {"ok": True, "steps": []}
    if drain3_compactor.is_available():
        result["steps"].append({"tool": "drain3", "ok": True, "message": "already available"})
    else:
        install = _pip_install("drain3")
        result["steps"].append({"tool": "drain3", **install})
        result["ok"] = result["ok"] and install.get("ok", False)

    ws.log_event("setup", result)
    return result


def repair(workspace: Path) -> dict[str, Any]:
    return setup(workspace)


def _pip_install(package: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", package],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"Failed to install {package}: {exc}"}
    if completed.returncode != 0:
        return {"ok": False, "error": (completed.stderr or completed.stdout or "install failed").strip()[:500]}
    return {"ok": True, "message": f"Installed {package}."}


def status(workspace: Path) -> dict[str, Any]:
    ws = DiagnosticsWorkspace(workspace)
    config = ws.load_config()
    payload = {
        "native_structured_output": True,
        "errorformat_style_parser": True,
        "reviewdog_external_binary": reviewdog_errorformat.external_binary(),
        "drain3_available": drain3_compactor.is_available(),
        "llmlingua_enabled": llmlingua_optional.is_enabled(config),
        "llmlingua_available": llmlingua_optional.is_available(),
        "config": config,
        "last_packet_path": str(ws._last_packet_path()) if ws._last_packet_path().exists() else "",
    }
    ws._write_json(ws._status_path(), payload)
    return payload
