"""Thin adapter around the real upstream Graphiti project.

Graphiti is managed as an external local tool under ~/.shamsu/tools/graphiti/.
This adapter never builds a SHAMSU-owned graph store and never fabricates
memory results. Missing packages, failed imports, unhealthy local backends, or
Graphiti call failures are returned as ok=False/error payloads.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from shamsu.memory.types import GraphitiHealth, LongTermMemory, MemoryKind

INSTALL_TIMEOUT_SECONDS = 600
CALL_TIMEOUT_SECONDS = 120
VERSION_TIMEOUT_SECONDS = 10
DOCKER_TIMEOUT_SECONDS = 60
DOCKER_DAEMON_TIMEOUT_SECONDS = 90
_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "[::1]"}

# Documented upstream quickstart: https://github.com/getzep/graphiti
# `docker run -p 6379:6379 -p 3000:3000 -it --rm falkordb/falkordb:latest`
# Run detached and named so `/memory setup`/`repair` can find and reuse it.
FALKORDB_CONTAINER_NAME = "shamsu-graphiti-falkordb"
FALKORDB_IMAGE = "falkordb/falkordb:latest"

# Same winget package id / flags used by scripts/install.ps1 for Ollama.
DOCKER_WINGET_ID = "Docker.DockerDesktop"

# Fallback lookup used the same way find_ollama_executable() falls back to
# known install paths: a winget install doesn't refresh this process's PATH,
# so `shutil.which` alone can miss a Docker CLI installed moments ago.
_KNOWN_DOCKER_CLI_PATHS: list[Path] = [
    Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe"),
]
_DOCKER_DESKTOP_APP = Path(r"C:\Program Files\Docker\Docker\Docker Desktop.exe")


def default_tool_dir() -> Path:
    return Path.home() / ".shamsu" / "tools" / "graphiti"


def default_global_config_path() -> Path:
    return Path.home() / ".shamsu" / "config.json"


def is_local_uri(uri: str) -> bool:
    uri = uri.strip()
    if not uri:
        return True
    parsed = urlparse(uri)
    if not parsed.scheme:
        return True
    if parsed.scheme == "file":
        return True
    return (parsed.hostname or "").lower() in _LOCAL_HOSTS


def _python_name() -> str:
    return "python.exe" if sys.platform == "win32" else "python"


def _venv_python(tool_dir: Path) -> Path:
    folder = "Scripts" if sys.platform == "win32" else "bin"
    return tool_dir / ".venv" / folder / _python_name()


def _no_window_flags() -> int:
    if sys.platform != "win32":
        return 0
    return subprocess.CREATE_NO_WINDOW


def _is_default_falkordb_uri(uri: str) -> bool:
    parsed = urlparse(str(uri))
    return (
        parsed.scheme == "falkor"
        and (parsed.hostname or "").lower() in _LOCAL_HOSTS - {""}
        and (parsed.port or 6379) == 6379
    )


def _find_docker_executable() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    for candidate in _KNOWN_DOCKER_CLI_PATHS:
        if candidate.exists():
            return str(candidate)
    return None


class GraphitiAdapter:
    def __init__(self, tool_dir: Path | None = None) -> None:
        self.tool_dir = (tool_dir or default_tool_dir()).resolve()

    def resolve_python(self) -> Path | None:
        explicit_cmd = os.environ.get("SHAMSU_GRAPHITI_CMD", "").strip()
        if explicit_cmd:
            candidate = Path(explicit_cmd).expanduser()
            return candidate if candidate.exists() else None
        explicit_path = os.environ.get("SHAMSU_GRAPHITI_PATH", "").strip()
        if explicit_path:
            base = Path(explicit_path).expanduser()
            candidate = base if base.is_file() else _venv_python(base)
            return candidate if candidate.exists() else None
        candidate = _venv_python(self.tool_dir)
        if candidate.exists():
            return candidate
        return Path(sys.executable).resolve()

    def config_path(self, workspace: Path) -> Path:
        override = os.environ.get("SHAMSU_GRAPHITI_CONFIG", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        return Path(workspace).resolve() / ".shamsu" / "memory" / "config.json"

    def default_config(self, workspace: Path) -> dict[str, Any]:
        return {
            "backend": "falkordb",
            "graph_backend_uri": os.environ.get("SHAMSU_GRAPHITI_URI", "falkor://localhost:6379"),
            "llm_base_url": os.environ.get("SHAMSU_OLLAMA_OPENAI_BASE_URL", "http://localhost:11434/v1"),
            "llm_model": os.environ.get("SHAMSU_GRAPHITI_LLM_MODEL", "qwen3:8b"),
            "embedding_model": os.environ.get("SHAMSU_GRAPHITI_EMBEDDING_MODEL", "nomic-embed-text"),
            "embedding_dim": int(os.environ.get("SHAMSU_GRAPHITI_EMBEDDING_DIM", "768")),
            "group_id": f"shamsu:{Path(workspace).resolve().name}",
            "telemetry": False,
        }

    def _read_config(self, workspace: Path) -> dict[str, Any]:
        path = self.config_path(workspace)
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_config(self, workspace: Path, config: dict[str, Any]) -> Path:
        path = self.config_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return path

    def _remote_rejection(self, workspace: Path) -> str:
        config = {**self.default_config(workspace), **self._read_config(workspace)}
        for key in ("graph_backend_uri", "llm_base_url", "embedding_base_url"):
            uri = str(config.get(key) or "")
            if uri and not is_local_uri(uri):
                return f"Rejected non-local Graphiti {key}: {uri}. Only localhost/127.0.0.1/::1 or local file paths are allowed."
        env_uri = os.environ.get("SHAMSU_GRAPHITI_URI", "").strip()
        if env_uri and not is_local_uri(env_uri):
            return f"Rejected non-local Graphiti URI: {env_uri}. Only local endpoints are allowed."
        return ""

    def is_available(self, workspace: Path) -> bool:
        return self.healthcheck(workspace).ok

    def healthcheck(self, workspace: Path) -> GraphitiHealth:
        rejection = self._remote_rejection(workspace)
        config_path = self.config_path(workspace)
        if rejection:
            return GraphitiHealth(False, config_path=str(config_path), message=rejection)
        if not config_path.exists():
            return GraphitiHealth(False, config_path=str(config_path), message="Graphiti local config is missing. Run /memory setup.")
        python = self.resolve_python()
        if python is None:
            return GraphitiHealth(False, config_path=str(config_path), message="Graphiti Python runtime was not found. Run /memory setup.")
        version = self._graphiti_version(python)
        if version.startswith("ERROR:"):
            return GraphitiHealth(False, tool_path=str(python), config_path=str(config_path), message=version[6:].strip())
        config = {**self.default_config(workspace), **self._read_config(workspace)}
        backend_message = self._check_backend(config.get("graph_backend_uri", ""))
        if backend_message:
            return GraphitiHealth(False, str(python), str(config_path), version, backend_message)
        return GraphitiHealth(True, str(python), str(config_path), version, "Graphiti is ready.")

    def _graphiti_version(self, python: Path) -> str:
        script = "import importlib.metadata as m; print(m.version('graphiti-core'))"
        try:
            completed = subprocess.run(
                [str(python), "-c", script], capture_output=True, text=True,
                timeout=VERSION_TIMEOUT_SECONDS, creationflags=_no_window_flags()
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"ERROR: Graphiti runtime could not be run: {exc}"
        if completed.returncode != 0:
            return "ERROR: graphiti-core is not installed in the managed runtime. Run /memory setup."
        return (completed.stdout or completed.stderr).strip()

    def _check_backend(self, uri: str) -> str:
        parsed = urlparse(str(uri))
        if parsed.scheme == "file" or not parsed.scheme:
            return ""
        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            return f"Graphiti backend URI is incomplete: {uri}"
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return ""
        except OSError as exc:
            return f"Graphiti local backend is not reachable at {uri}: {exc}. Start the local Neo4j/FalkorDB backend or run /memory repair."

    def status(self, workspace: Path) -> dict[str, Any]:
        health = self.healthcheck(workspace)
        return {
            "ok": health.ok,
            "available": health.available,
            "tool_path": health.tool_path,
            "config_path": health.config_path,
            "version": health.version,
            "message": health.message,
        }

    def setup(self, workspace: Path) -> dict[str, Any]:
        config_path = self._write_config(workspace, self.default_config(workspace))
        global_config = default_global_config_path()
        global_config.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        try:
            existing = json.loads(global_config.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        existing["graphiti"] = {"tool_dir": str(self.tool_dir), "workspace_config": str(config_path)}
        global_config.write_text(json.dumps(existing, indent=2), encoding="utf-8")

        python = _venv_python(self.tool_dir)
        if not python.exists():
            self.tool_dir.mkdir(parents=True, exist_ok=True)
            created = subprocess.run([sys.executable, "-m", "venv", str(self.tool_dir / ".venv")], capture_output=True, text=True, timeout=120, creationflags=_no_window_flags())
            if created.returncode != 0:
                return {"ok": False, "error": created.stderr or created.stdout, "config_path": str(config_path)}
        installed = subprocess.run(
            [str(python), "-m", "pip", "install", "graphiti-core[falkordb]"],
            capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SECONDS, creationflags=_no_window_flags()
        )
        if installed.returncode != 0:
            return {
                "ok": False,
                "error": installed.stderr or installed.stdout,
                "config_path": str(config_path),
                "manual_steps": "Install graphiti-core[falkordb] into the managed venv or set SHAMSU_GRAPHITI_CMD to a local Python with graphiti-core installed.",
            }
        backend_result = self._ensure_local_backend(workspace)
        health = self.healthcheck(workspace)
        result: dict[str, Any] = {"ok": health.ok, "path": str(python), "config_path": str(config_path), "message": health.message}
        if backend_result is not None:
            result["backend"] = backend_result
            if not health.ok and not backend_result.get("ok"):
                result["manual_steps"] = backend_result.get("error", "")
        return result

    def repair(self, workspace: Path) -> dict[str, Any]:
        if not self.config_path(workspace).exists():
            self._write_config(workspace, self.default_config(workspace))
        health = self.healthcheck(workspace)
        if health.ok:
            return {"ok": True, "message": health.message, "path": health.tool_path}
        backend_result = self._ensure_local_backend(workspace)
        if backend_result is not None and backend_result.get("ok"):
            health = self.healthcheck(workspace)
            if health.ok:
                return {"ok": True, "message": health.message, "path": health.tool_path, "backend": backend_result}
        manual_steps = "Run /memory setup, ensure Ollama is running locally, and start local FalkorDB on localhost:6379 or configure a local Neo4j URI."
        if backend_result is not None and not backend_result.get("ok"):
            manual_steps = backend_result.get("error", manual_steps)
        return {"ok": False, "message": health.message, "manual_steps": manual_steps}

    def _ensure_local_backend(self, workspace: Path) -> dict[str, Any] | None:
        """Best-effort local FalkorDB start via the documented upstream Docker
        quickstart. Only touches the default local falkor://<host>:6379
        backend; a custom/non-default URI (e.g. a local Neo4j install) is left
        for the user to manage, since there's no single documented command
        for it. Returns None when there's nothing to do here."""
        config = {**self.default_config(workspace), **self._read_config(workspace)}
        uri = str(config.get("graph_backend_uri", ""))
        if not _is_default_falkordb_uri(uri):
            return None
        if not self._check_backend(uri):
            return {"ok": True, "message": "FalkorDB backend already reachable."}
        return self._start_local_falkordb()

    def _start_local_falkordb(self) -> dict[str, Any]:
        docker = _find_docker_executable()
        if not docker:
            install_result = self._install_docker_desktop()
            if not install_result.get("ok"):
                return install_result
            docker = _find_docker_executable()
            if not docker:
                return {
                    "ok": False,
                    "error": (
                        "Docker Desktop was installed but its CLI isn't visible to this SHAMSU process yet. "
                        "Close and reopen SHAMSU (or open a new terminal) so it picks up the updated PATH, "
                        "then run /memory repair."
                    ),
                }
        if not self._docker_daemon_ready(docker):
            if not self._start_docker_desktop_app(docker):
                return {
                    "ok": False,
                    "error": (
                        "Docker is installed but its daemon isn't running and SHAMSU could not start it "
                        "automatically. Start Docker Desktop yourself, then run /memory repair."
                    ),
                }
        try:
            inspect = subprocess.run(
                [docker, "inspect", "-f", "{{.State.Running}}", FALKORDB_CONTAINER_NAME],
                capture_output=True, text=True, timeout=15, creationflags=_no_window_flags(),
            )
            if inspect.returncode == 0:
                if inspect.stdout.strip() == "true":
                    return {"ok": True, "message": "FalkorDB container already running."}
                started = subprocess.run(
                    [docker, "start", FALKORDB_CONTAINER_NAME],
                    capture_output=True, text=True, timeout=DOCKER_TIMEOUT_SECONDS, creationflags=_no_window_flags(),
                )
                if started.returncode != 0:
                    return {"ok": False, "error": (started.stderr or started.stdout or "docker start failed").strip()}
                self._wait_for_local_port(6379, timeout=10.0)
                return {"ok": True, "message": "Started existing local FalkorDB container."}
            run = subprocess.run(
                [docker, "run", "-d", "--name", FALKORDB_CONTAINER_NAME, "-p", "6379:6379", "-p", "3000:3000", FALKORDB_IMAGE],
                capture_output=True, text=True, timeout=DOCKER_TIMEOUT_SECONDS, creationflags=_no_window_flags(),
            )
            if run.returncode != 0:
                return {"ok": False, "error": (run.stderr or run.stdout or "docker run failed").strip()}
            self._wait_for_local_port(6379, timeout=10.0)
            return {"ok": True, "message": "Started local FalkorDB container on localhost:6379."}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "error": f"Docker is installed but not usable ({exc}). Start Docker Desktop and run /memory repair.",
            }

    def _wait_for_local_port(self, port: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                    return True
            except OSError:
                time.sleep(0.5)
        return False

    def _install_docker_desktop(self) -> dict[str, Any]:
        """Same winget-install pattern scripts/install.ps1 already uses for
        Ollama: `winget install --id <pkg> -e --accept-*-agreements`."""
        if sys.platform != "win32":
            return {
                "ok": False,
                "error": "Automatic Docker install is only wired up for Windows via winget. Install Docker for your OS, then run /memory repair.",
            }
        winget = shutil.which("winget")
        if not winget:
            return {
                "ok": False,
                "error": "winget was not found, so Docker Desktop could not be installed automatically. Install it from https://www.docker.com/products/docker-desktop/, then run /memory repair.",
            }
        try:
            result = subprocess.run(
                [winget, "install", "--id", DOCKER_WINGET_ID, "-e", "--accept-package-agreements", "--accept-source-agreements"],
                capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SECONDS, creationflags=_no_window_flags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": f"Docker Desktop install via winget failed: {exc}"}
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or result.stdout or "winget install failed").strip()}
        return {"ok": True, "message": "Installed Docker Desktop via winget."}

    def _docker_daemon_ready(self, docker: str) -> bool:
        try:
            result = subprocess.run(
                [docker, "info"], capture_output=True, text=True, timeout=10, creationflags=_no_window_flags(),
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _start_docker_desktop_app(self, docker: str) -> bool:
        if sys.platform != "win32" or not _DOCKER_DESKTOP_APP.exists():
            return False
        try:
            subprocess.Popen(
                [str(_DOCKER_DESKTOP_APP)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=_no_window_flags(),
            )
        except OSError:
            return False
        deadline = time.monotonic() + DOCKER_DAEMON_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._docker_daemon_ready(docker):
                return True
            time.sleep(3)
        return False

    def add_episode(self, workspace: Path, text: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._run_graphiti_call(workspace, "add_episode", {"text": text, "metadata": metadata or {}})

    def remember(self, workspace: Path, text: str, kind: MemoryKind, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"kind": kind, "text": text, "metadata": metadata or {}}
        return self._run_graphiti_call(workspace, "remember", payload)

    def search(self, workspace: Path, query: str, limit: int = 8, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._run_graphiti_call(workspace, "search", {"query": query, "limit": limit, "filters": filters or {}})

    def get_relevant(self, workspace: Path, user_prompt: str, task_type: str | None = None, limit: int = 8) -> list[LongTermMemory]:
        result = self.search(workspace, user_prompt, limit=limit, filters={"task_type": task_type} if task_type else None)
        if not result.get("ok"):
            return []
        memories = []
        for item in result.get("results", [])[:limit]:
            memories.append(LongTermMemory(
                kind=item.get("kind", "task_summary"),
                text=item.get("text") or item.get("fact") or item.get("summary", ""),
                memory_id=str(item.get("id") or item.get("uuid") or ""),
                score=float(item.get("score") or 0.0),
                metadata=item.get("metadata") or {},
            ))
        return [memory for memory in memories if memory.text]

    def forget(self, workspace: Path, memory_id_or_query: str) -> dict[str, Any]:
        return self._run_graphiti_call(workspace, "forget", {"query": memory_id_or_query})

    def summarize_session(self, workspace: Path, session_id: str) -> dict[str, Any]:
        return {"ok": False, "error": "Session summarization needs an LLM summary from SHAMSU before Graphiti storage; use MemoryService.summarize_session."}

    def _run_graphiti_call(self, workspace: Path, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        health = self.healthcheck(workspace)
        if not health.ok:
            return {"ok": False, "error": health.message}
        python = Path(health.tool_path)
        request = {"action": action, "config": {**self.default_config(workspace), **self._read_config(workspace)}, "payload": payload}
        script = _GRAPHITI_HELPER
        try:
            completed = subprocess.run(
                [str(python), "-c", script], input=json.dumps(request), capture_output=True,
                text=True, timeout=CALL_TIMEOUT_SECONDS, creationflags=_no_window_flags()
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": f"Graphiti call failed: {exc}"}
        if completed.returncode != 0:
            return {"ok": False, "error": (completed.stderr or completed.stdout or "Graphiti call failed").strip()}
        try:
            data = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": "Graphiti returned non-JSON output."}
        return data if isinstance(data, dict) else {"ok": True, "result": data}


_GRAPHITI_HELPER = r'''
import asyncio, json, os, sys
from datetime import datetime, timezone

async def main():
    req = json.loads(sys.stdin.read())
    cfg = req["config"]
    os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false" if cfg.get("telemetry") is False else "true"
    from graphiti_core import Graphiti
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

    llm_config = LLMConfig(api_key="ollama", model=cfg["llm_model"], small_model=cfg["llm_model"], base_url=cfg["llm_base_url"])
    llm_client = OpenAIGenericClient(config=llm_config)
    embedder = OpenAIEmbedder(config=OpenAIEmbedderConfig(api_key="ollama", embedding_model=cfg["embedding_model"], embedding_dim=int(cfg.get("embedding_dim", 768)), base_url=cfg["llm_base_url"]))
    cross = OpenAIRerankerClient(client=llm_client, config=llm_config)
    uri = cfg["graph_backend_uri"]
    graphiti = Graphiti(uri, "neo4j", "password", llm_client=llm_client, embedder=embedder, cross_encoder=cross)
    action = req["action"]
    payload = req["payload"]
    group_id = cfg.get("group_id", "shamsu")
    if action in {"add_episode", "remember"}:
        text = payload["text"]
        metadata = payload.get("metadata") or {}
        if action == "remember":
            text = f"[{payload.get('kind', 'task_summary')}] {text}\nmetadata: {json.dumps(metadata, sort_keys=True)}"
        await graphiti.add_episode(name=f"shamsu-{action}-{datetime.now(timezone.utc).isoformat()}", episode_body=text, source_description="SHAMSU long-term memory", group_id=group_id, reference_time=datetime.now(timezone.utc))
        print(json.dumps({"ok": True, "message": "stored"}))
        return
    if action == "search":
        results = await graphiti.search(payload["query"], group_ids=[group_id], num_results=int(payload.get("limit", 8)))
        out = []
        for item in results:
            fact = getattr(item, "fact", None) or getattr(item, "summary", None) or str(item)
            out.append({"id": getattr(item, "uuid", ""), "text": fact, "score": float(getattr(item, "score", 0.0) or 0.0), "metadata": {}})
        print(json.dumps({"ok": True, "results": out}))
        return
    if action == "forget":
        print(json.dumps({"ok": False, "error": "Graphiti deletion is not exposed by this adapter yet; search candidates and remove with the upstream tool if needed."}))
        return
    print(json.dumps({"ok": False, "error": f"Unsupported action: {action}"}))

asyncio.run(main())
'''
