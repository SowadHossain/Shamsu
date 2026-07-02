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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shamsu.llm.manager import OLLAMA_BASE_URL
from shamsu.runtime.models import required_model_names

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


def start_ollama(ollama_path: Path) -> None:
    subprocess.Popen(
        [str(ollama_path), "serve"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_creationflags(),
    )


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


def list_installed_models(ollama_path: Path) -> list[str]:
    completed = subprocess.run(
        [str(ollama_path), "list"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
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
    completed = subprocess.run(
        [str(ollama_path), "pull", model_name],
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def pull_missing_models(ollama_path: Path, missing_models: list[str]) -> dict[str, int]:
    results: dict[str, int] = {}
    for model in missing_models:
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
    parser = argparse.ArgumentParser(prog="python -m shamsu.runtime.ollama")
    parser.add_argument("command", choices=["status", "pull", "repair", "write-config"])
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--skip-models", action="store_true")
    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    raise SystemExit(main())
