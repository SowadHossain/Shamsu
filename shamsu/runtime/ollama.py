"""
Local Ollama runtime management.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shamsu.llm.manager import OLLAMA_BASE_URL
from shamsu.runtime.models import (
    ALL_MODEL_SPECS,
    active_tier,
    is_allowed_model,
    required_model_names,
)
from shamsu.runtime.session_registry import (
    clear_ollama_ownership,
    live_session_pids,
    pid_alive,
    read_ollama_owner,
    unregister_session,
)

# Every tier's models - so switching tiers doesn't leave the previous tier's
# models un-manageable by unload_shamsu_models().
_KNOWN_MODEL_NAMES = frozenset(spec.name for spec in ALL_MODEL_SPECS)

HEALTH_TIMEOUT_SECONDS = 2


@dataclass(frozen=True)
class RuntimeStatus:
    ollama_path: str = ""
    server_running: bool = False
    installed_models: list[str] = field(default_factory=list)
    missing_models: list[str] = field(default_factory=list)
    required_models: list[str] = field(default_factory=required_model_names)
    base_url: str = OLLAMA_BASE_URL
    message: str = ""
    tier: str = field(default_factory=lambda: active_tier().value)

    @property
    def ollama_found(self) -> bool:
        return bool(self.ollama_path)

    @property
    def ready(self) -> bool:
        return self.ollama_found and self.server_running and not self.missing_models


def find_ollama_executable(extra_paths: list[Path] | None = None) -> Path | None:
    found = shutil.which("ollama")
    if found:
        return Path(found).resolve()
    for candidate in _known_ollama_paths(extra_paths):
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    return None


def collect_status(
    ollama_path: Path | None = None,
    base_url: str = OLLAMA_BASE_URL,
) -> RuntimeStatus:
    executable = ollama_path or find_ollama_executable()
    if executable is None:
        return RuntimeStatus(
            missing_models=required_model_names(),
            message="Ollama is not installed or was not found.",
            base_url=base_url,
        )

    running = is_ollama_running(base_url)
    installed = list_installed_models(executable) if running else []
    missing = [name for name in required_model_names() if name not in installed]
    message = "Ollama is ready." if running and not missing else _status_message(running, missing)
    return RuntimeStatus(
        ollama_path=str(executable),
        server_running=running,
        installed_models=installed,
        missing_models=missing,
        message=message,
        base_url=base_url,
    )


def is_ollama_running(base_url: str = OLLAMA_BASE_URL) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/tags", timeout=HEALTH_TIMEOUT_SECONDS):
            return True
    except (OSError, urllib.error.URLError):
        return False


def start_ollama(ollama_path: Path) -> int | None:
    """Start ``ollama serve`` detached and return its PID (or None on failure).

    The PID lets the caller record that SHAMSU *owns* this server, so the
    last-session-exit cleanup may stop it later (a pre-existing/tray-app Ollama
    is never started here and therefore never owned or stopped).
    """
    try:
        process = subprocess.Popen(
            [str(ollama_path), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creationflags(),
        )
    except OSError:
        return None
    return process.pid


def wait_until_running(
    base_url: str = OLLAMA_BASE_URL,
    timeout_seconds: int = 15,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if is_ollama_running(base_url):
            return True
        time.sleep(0.5)
    return False


def list_loaded_models(base_url: str = OLLAMA_BASE_URL) -> list[str]:
    """Model names currently resident in the Ollama server's memory (``/api/ps``)."""
    try:
        with urllib.request.urlopen(
            f"{base_url.rstrip('/')}/api/ps", timeout=HEALTH_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return []
    names: list[str] = []
    for entry in payload.get("models", []):
        name = entry.get("name") or entry.get("model")
        if name:
            names.append(name)
    return names


def unload_model(model: str, base_url: str = OLLAMA_BASE_URL) -> bool:
    """Evict a model from RAM immediately by asking Ollama for ``keep_alive=0``."""
    data = json.dumps({"model": model, "keep_alive": 0}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            return True
    except (OSError, urllib.error.URLError):
        return False


def unload_shamsu_models(base_url: str = OLLAMA_BASE_URL) -> list[str]:
    """Unload only SHAMSU's own models that are currently loaded.

    Used when the server is not SHAMSU-owned (e.g. the Windows tray app): we
    free our footprint — notably the router model pinned with ``keep_alive=-1``
    — without touching the server process or any unrelated model.
    """
    loaded = set(list_loaded_models(base_url))
    unloaded: list[str] = []
    for name in sorted(loaded & _KNOWN_MODEL_NAMES):
        if unload_model(name, base_url):
            unloaded.append(name)
    return unloaded


def stop_server(serve_pid: int) -> bool:
    """Terminate a SHAMSU-started ``ollama serve`` process (and its children)."""
    if serve_pid <= 0 or not pid_alive(serve_pid):
        return False
    try:
        import psutil

        proc = psutil.Process(serve_pid)
        children = proc.children(recursive=True)
        proc.terminate()
        for child in children:
            try:
                child.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs([proc, *children], timeout=5)
        for survivor in alive:
            try:
                survivor.kill()
            except psutil.Error:
                pass
        return True
    except Exception:
        try:  # pragma: no cover - psutil path is the norm
            os.kill(serve_pid, getattr(__import__("signal"), "SIGTERM", 15))
            return True
        except OSError:
            return False


def shutdown_if_last_session(
    self_pid: int | None = None,
    *,
    base_url: str = OLLAMA_BASE_URL,
    server_stopper: Callable[[int], bool] | None = None,
    model_unloader: Callable[[str], list[str]] | None = None,
) -> str:
    """Free SHAMSU's Ollama footprint when the *last* session exits.

    Deregisters this session, then — only if no other live SHAMSU session
    remains — stops the Ollama server when SHAMSU started it, otherwise
    unloads SHAMSU's own models so the ``keep_alive=-1`` router does not stay
    pinned in RAM. Returns a short tag for logging/tests: ``not-last``,
    ``stopped-server``, ``unloaded`` or ``noop``. Best-effort: never raises.

    Deliberately does NOT touch SHAMSU's local FalkorDB memory container -
    that's the user's to start/stop via `docker`/`docker start`/`docker stop`
    directly. It used to be auto-stopped here on last-session exit, which
    meant memory silently went cold (and had to cold-start again) between
    ordinary REPL sessions; see CHANGELOG.
    """
    self_pid = self_pid or os.getpid()
    try:
        unregister_session(self_pid)
        if live_session_pids(exclude=self_pid):
            return "not-last"
        owner = read_ollama_owner()
        serve_pid = int(owner.get("serve_pid", 0)) if owner else 0
        if serve_pid and pid_alive(serve_pid):
            stopper = server_stopper or stop_server
            if stopper(serve_pid):
                clear_ollama_ownership()
                return "stopped-server"
            return "noop"
        if owner:
            clear_ollama_ownership()
        unloader = model_unloader or unload_shamsu_models
        return "unloaded" if unloader(base_url) else "noop"
    except Exception:
        return "noop"


def list_installed_models(ollama_path: Path) -> list[str]:
    completed = _run_ollama_command(ollama_path, "list", timeout=30)
    if completed.returncode != 0:
        return []
    return parse_ollama_list(completed.stdout)


def parse_ollama_list(output: str) -> list[str]:
    models: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("name "):
            continue
        models.append(stripped.split()[0])
    return models


def pull_model(ollama_path: Path, model_name: str) -> tuple[int, str, str]:
    if not is_allowed_model(model_name):
        return (
            2,
            "",
            f"Refusing to pull '{model_name}': it is not in SHAMSU's 8GB model cookbook.",
        )
    completed = _run_ollama_command(ollama_path, "pull", model_name, timeout=3600)
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def pull_model_streaming(
    ollama_path: Path,
    model_name: str,
    progress_callback: Callable[[str], None] | None = None,
) -> int:
    if not is_allowed_model(model_name):
        if progress_callback:
            progress_callback(
                f"Refusing to pull '{model_name}': it is not in SHAMSU's 8GB model cookbook."
            )
        return 2
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    process = subprocess.Popen(
        [str(ollama_path), "pull", model_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=_no_window_flags(),
    )
    if process.stdout:
        for chunk in iter(lambda: process.stdout.read(1), ""):
            if progress_callback and chunk:
                progress_callback(chunk)
    return process.wait()


def ensure_model_available(
    ollama_path: Path,
    model_name: str,
    progress_callback: Callable[[str], None] | None = None,
) -> bool:
    """Pull `model_name` if it isn't already installed.

    Returns whether the model is available after this call.
    """
    if not is_allowed_model(model_name):
        if progress_callback:
            progress_callback(
                f"Refusing to pull '{model_name}': it is not in SHAMSU's 8GB model cookbook."
            )
        return False
    if model_name in list_installed_models(ollama_path):
        return True
    exit_code = pull_model_streaming(ollama_path, model_name, progress_callback)
    if exit_code != 0:
        return False
    return model_name in list_installed_models(ollama_path)


def pull_missing_models(ollama_path: Path, missing_models: list[str]) -> dict[str, int]:
    results: dict[str, int] = {}
    for model in missing_models:
        if not is_allowed_model(model):
            results[model] = 2
            continue
        exit_code, _stdout, _stderr = pull_model(ollama_path, model)
        results[model] = exit_code
    return results


def write_runtime_config(repo_root: Path, status: RuntimeStatus) -> Path:
    config_dir = repo_root.resolve() / ".shamsu"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "runtime.json"
    payload = asdict(status)
    payload["local_only"] = True
    payload["note"] = "SHAMSU uses local Ollama only; no cloud AI endpoint is configured."
    config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return config_path


def repair_runtime(pull_models: bool = True) -> RuntimeStatus:
    executable = find_ollama_executable()
    if executable is None:
        return collect_status()
    if not is_ollama_running():
        start_ollama(executable)
        wait_until_running()
    status = collect_status(executable)
    if pull_models and status.server_running and status.missing_models:
        pull_missing_models(executable, status.missing_models)
        status = collect_status(executable)
    return status


def status_text(status: RuntimeStatus) -> str:
    if status.ready:
        return "Local runtime ready. Inference target: local Ollama."
    if not status.ollama_found:
        return "Ollama not found. Run `models repair` or reinstall with runtime bootstrap."
    if not status.server_running:
        return "Ollama found but not running. Run `models repair`."
    return f"Missing models: {', '.join(status.missing_models)}. Run `models pull`."


def repo_root_from_runtime() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    from shamsu.runtime.models import initialize_model_tier

    parser = argparse.ArgumentParser(prog="python -m shamsu.runtime.ollama")
    parser.add_argument("command", choices=["status", "pull", "repair", "write-config"])
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--skip-models", action="store_true")
    parser.add_argument("--workspace", default=str(Path.cwd()))
    args = parser.parse_args(argv)

    initialize_model_tier(Path(args.workspace))

    if args.command == "repair":
        status = repair_runtime(pull_models=not args.skip_models)
    else:
        status = collect_status()
        if args.command == "pull" and status.ollama_found:
            if not status.server_running:
                start_ollama(Path(status.ollama_path))
                wait_until_running()
                status = collect_status(Path(status.ollama_path))
            if not args.skip_models and status.missing_models:
                pull_missing_models(Path(status.ollama_path), status.missing_models)
                status = collect_status(Path(status.ollama_path))

    if args.command == "write-config":
        write_runtime_config(repo_root_from_runtime(), status)

    if args.as_json:
        print(json.dumps(asdict(status), indent=2))
    else:
        print(status_text(status))
    return 0 if status.ready or args.command == "status" else 1


def _known_ollama_paths(extra_paths: list[Path] | None = None) -> list[Path]:
    paths: list[Path] = list(extra_paths or [])
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        program_files = os.environ.get("ProgramFiles")
        if local_app_data:
            paths.append(Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe")
        if program_files:
            paths.append(Path(program_files) / "Ollama" / "ollama.exe")
    else:
        paths.extend(
            [
                Path("/usr/local/bin/ollama"),
                Path("/usr/bin/ollama"),
                Path("/opt/homebrew/bin/ollama"),
            ]
        )
    return paths


def _status_message(running: bool, missing: list[str]) -> str:
    if not running:
        return "Ollama is installed but not running."
    if missing:
        return f"Missing required models: {', '.join(missing)}"
    return "Ollama status is unknown."


def _creationflags() -> int:
    if sys.platform != "win32":
        return 0
    return subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS


def _no_window_flags() -> int:
    """Hide the console window for captured/piped subprocesses on Windows.

    Distinct from `_creationflags()`, which also sets `DETACHED_PROCESS` — that
    is only correct for the fire-and-forget `serve` process, not for calls whose
    stdout/stderr we read (DETACHED_PROCESS interferes with pipe inheritance).
    Without this flag, `ollama.exe` (a console app) flashes a window on every
    invocation — and `list_installed_models` runs before every LLM call.
    """
    if sys.platform != "win32":
        return 0
    return subprocess.CREATE_NO_WINDOW


def _run_ollama_command(
    ollama_path: Path,
    *args: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    return subprocess.run(
        [str(ollama_path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
        creationflags=_no_window_flags(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
