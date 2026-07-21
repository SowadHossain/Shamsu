"""
Cross-session SHAMSU lifecycle bookkeeping.

Each live REPL registers a small PID file under ``~/.shamsu/runtime/sessions``.
When a session ends we can tell whether it was the *last* one still running
(stale entries from crashed sessions are pruned via a liveness check), which is
what lets SHAMSU free its Ollama footprint only when no session remains.

Ownership: when SHAMSU itself starts the ``ollama serve`` process it records the
server PID here. That marker is what authorises SHAMSU to stop the server on the
last exit — a pre-existing Ollama (e.g. the Windows tray app) leaves no marker
and is therefore never stopped.

This module is deliberately dependency-light (filesystem + PID liveness only) so
it stays trivially unit-testable; the actual server-stop/model-unload lives in
``shamsu.runtime.ollama``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

try:  # psutil is a hard dependency, but stay import-safe for minimal installs.
    import psutil
except Exception:  # pragma: no cover - psutil missing is not expected
    psutil = None  # type: ignore[assignment]

_OWNER_FILENAME = "ollama-owner.json"


def _base_dir() -> Path:
    """Single seam for tests: the root under which all runtime state lives."""
    return Path.home() / ".shamsu" / "runtime"


def _sessions_dir() -> Path:
    path = _base_dir() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _owner_path() -> Path:
    base = _base_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / _OWNER_FILENAME


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if psutil is not None:
        try:
            return psutil.pid_exists(pid)
        except Exception:
            return False
    try:  # pragma: no cover - fallback only when psutil is absent
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def register_session(pid: int | None = None) -> int:
    pid = pid or os.getpid()
    (_sessions_dir() / f"{pid}.pid").write_text(str(pid), encoding="utf-8")
    return pid


def unregister_session(pid: int | None = None) -> None:
    pid = pid or os.getpid()
    try:
        (_sessions_dir() / f"{pid}.pid").unlink()
    except FileNotFoundError:
        pass


def live_session_pids(exclude: int | None = None) -> list[int]:
    """Live SHAMSU session PIDs, pruning any stale (dead) entries on the way."""
    live: list[int] = []
    for path in _sessions_dir().glob("*.pid"):
        try:
            pid = int(path.stem)
        except ValueError:
            path.unlink(missing_ok=True)
            continue
        if not pid_alive(pid):
            path.unlink(missing_ok=True)
            continue
        if exclude is not None and pid == exclude:
            continue
        live.append(pid)
    return live


def claim_ollama_ownership(serve_pid: int, started_by: int | None = None) -> None:
    _owner_path().write_text(
        json.dumps({"serve_pid": int(serve_pid), "started_by": started_by or os.getpid()}),
        encoding="utf-8",
    )


def read_ollama_owner() -> dict | None:
    try:
        return json.loads(_owner_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clear_ollama_ownership() -> None:
    try:
        _owner_path().unlink()
    except FileNotFoundError:
        pass
