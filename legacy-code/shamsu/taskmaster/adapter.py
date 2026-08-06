"""Thin adapter around the real upstream Taskmaster CLI (`task-master-ai` on npm,
https://github.com/eyaltoledano/claude-task-master).

Taskmaster is managed as an external local tool under ~/.shamsu/tools/taskmaster/
(a local, non-global `npm install`, no sudo/admin). Per-workspace Taskmaster
state stays in the workspace's own `.taskmaster/` directory, using Taskmaster's
own documented CLI and file formats - this adapter never parses a PRD itself,
never invents a task graph, and never fabricates a CLI result. Missing
Node/npm, a missing managed install, or a failed CLI call are returned as
honest ok=False/error payloads, exactly like `GraphitiAdapter` and
`CodebaseMemoryAdapter`.

Local-only: Taskmaster supports many cloud model providers (anthropic, openai,
google, perplexity, xai, openrouter, azure, bedrock, vertex, ...). SHAMSU only
ever configures/accepts the local `ollama` provider - any other provider found
in `.taskmaster/config.json` is treated as unhealthy until repaired, and
`anonymousTelemetry` is always forced off.

Verified against the real CLI (`task-master-ai@0.43.1`) before writing this
adapter - see command shapes below; nothing here is guessed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from shamsu.runtime.models import model_for_role
from shamsu.taskmaster.types import TaskmasterHealth, TaskmasterTask

# `npm install` (dependency resolution) can be slow on first run; CLI calls
# that invoke the local model (parse-prd, expand, update-task, research) can
# take minutes against a local Ollama model. Deterministic calls (list, show,
# next, set-status, init, models) are fast and use a short timeout instead.
INSTALL_TIMEOUT_SECONDS = 600
AI_CALL_TIMEOUT_SECONDS = 900
CALL_TIMEOUT_SECONDS = 30
VERSION_TIMEOUT_SECONDS = 15

LOCAL_PROVIDER = "ollama"
_MODEL_ROLES = ("main", "research", "fallback")

PACKAGE_NAME = "task-master-ai"
# Real bin map (npm view task-master-ai bin): "task-master" is the CLI;
# "task-master-ai"/"task-master-mcp" both launch the MCP server instead, so
# SHAMSU invokes the bundled CLI script directly rather than the package's
# default bin.
_CLI_RELATIVE_PATH = Path("node_modules") / "task-master-ai" / "dist" / "task-master.js"


def default_tool_dir() -> Path:
    return Path.home() / ".shamsu" / "tools" / "taskmaster"


def _no_window_flags() -> int:
    if sys.platform != "win32":
        return 0
    return subprocess.CREATE_NO_WINDOW


class TaskmasterAdapter:
    def __init__(self, tool_dir: Path | None = None) -> None:
        self.tool_dir = (tool_dir or default_tool_dir()).resolve()

    # -- binary/runtime resolution ------------------------------------------

    def resolve_node(self) -> Path | None:
        explicit = os.environ.get("SHAMSU_TASKMASTER_NODE", "").strip()
        if explicit:
            candidate = Path(explicit).expanduser()
            return candidate if candidate.exists() else None
        found = shutil.which("node")
        return Path(found).resolve() if found else None

    def resolve_cli_script(self) -> Path | None:
        explicit = os.environ.get("SHAMSU_TASKMASTER_CMD", "").strip()
        if explicit:
            candidate = Path(explicit).expanduser()
            return candidate if candidate.exists() else None
        candidate = self.tool_dir / _CLI_RELATIVE_PATH
        return candidate if candidate.exists() else None

    # -- workspace config -----------------------------------------------------

    def config_path(self, workspace: Path) -> Path:
        return Path(workspace).resolve() / ".taskmaster" / "config.json"

    def tasks_path(self, workspace: Path) -> Path:
        return Path(workspace).resolve() / ".taskmaster" / "tasks" / "tasks.json"

    def is_initialized(self, workspace: Path) -> bool:
        return self.config_path(workspace).exists()

    def _read_config(self, workspace: Path) -> dict[str, Any]:
        try:
            return json.loads(self.config_path(workspace).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _non_local_provider_message(self, workspace: Path) -> str:
        config = self._read_config(workspace)
        models = config.get("models", {}) if isinstance(config, dict) else {}
        offenders = []
        for role in _MODEL_ROLES:
            provider = str((models.get(role) or {}).get("provider", ""))
            if provider and provider != LOCAL_PROVIDER:
                offenders.append(f"{role}={provider}")
        if offenders:
            return (
                "Taskmaster is configured with non-local model provider(s): "
                f"{', '.join(offenders)}. Only the local Ollama provider is allowed. "
                "Run /taskmaster repair."
            )
        return ""

    # -- health ---------------------------------------------------------------

    def is_available(self, workspace: Path) -> bool:
        return self.healthcheck(workspace).ok

    def healthcheck(self, workspace: Path) -> TaskmasterHealth:
        node = self.resolve_node()
        if node is None:
            return TaskmasterHealth(
                available=False,
                message="Node.js was not found. Install Node.js, then run /taskmaster setup.",
            )
        script = self.resolve_cli_script()
        if script is None:
            return TaskmasterHealth(
                available=False,
                node_path=str(node),
                message=(
                    f"Taskmaster CLI is not installed at {self.tool_dir / _CLI_RELATIVE_PATH}. "
                    "Run /taskmaster setup."
                ),
            )
        version = self._version(node, script)
        if version.startswith("ERROR:"):
            return TaskmasterHealth(available=False, node_path=str(node), cli_path=str(script), message=version[6:].strip())
        if self.is_initialized(workspace):
            rejection = self._non_local_provider_message(workspace)
            if rejection:
                return TaskmasterHealth(available=False, node_path=str(node), cli_path=str(script), version=version, message=rejection)
        return TaskmasterHealth(
            available=True, node_path=str(node), cli_path=str(script), version=version,
            message="Taskmaster is ready.",
        )

    def _version(self, node: Path, script: Path) -> str:
        try:
            completed = subprocess.run(
                [str(node), str(script), "--version"],
                capture_output=True, text=True, timeout=VERSION_TIMEOUT_SECONDS,
                creationflags=_no_window_flags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"ERROR: Taskmaster CLI could not be run: {exc}"
        if completed.returncode != 0:
            return f"ERROR: Taskmaster CLI exited with {completed.returncode}: {(completed.stderr or completed.stdout).strip()}"
        return (completed.stdout or completed.stderr).strip()

    def status(self, workspace: Path) -> dict[str, Any]:
        health = self.healthcheck(workspace)
        result: dict[str, Any] = {
            "available": health.available,
            "node_path": health.node_path,
            "cli_path": health.cli_path,
            "version": health.version,
            "message": health.message,
            "initialized": self.is_initialized(workspace),
            "config_path": str(self.config_path(workspace)),
        }
        if health.ok and result["initialized"]:
            listing = self.list_tasks(workspace)
            if listing.get("ok"):
                tasks = listing.get("tasks", [])
                counts: dict[str, int] = {}
                for task in tasks:
                    counts[task.status] = counts.get(task.status, 0) + 1
                result["task_count"] = len(tasks)
                result["status_counts"] = counts
        return result

    # -- setup/repair -----------------------------------------------------------

    def setup(self, workspace: Path, project_name: str = "") -> dict[str, Any]:
        node = self.resolve_node()
        if node is None:
            return {
                "ok": False,
                "error": "Node.js was not found on PATH.",
                "manual_steps": "Install Node.js (https://nodejs.org/) so `node` is on PATH, then run /taskmaster setup again.",
            }

        script = self.resolve_cli_script()
        if script is None:
            install_result = self._npm_install()
            if not install_result.get("ok"):
                return install_result
            script = self.resolve_cli_script()
            if script is None:
                return {
                    "ok": False,
                    "error": f"npm install completed but the CLI script was not found at {self.tool_dir / _CLI_RELATIVE_PATH}.",
                }

        if not self.is_initialized(workspace):
            init_result = self.init_project(workspace, project_name)
            if not init_result.get("ok"):
                return init_result

        models_result = self.configure_local_models(workspace)
        if not models_result.get("ok"):
            return {**models_result, "manual_steps": models_result.get("error", "")}

        health = self.healthcheck(workspace)
        return {"ok": health.ok, "message": health.message, "cli_path": str(script), "models": models_result}

    def _npm_install(self) -> dict[str, Any]:
        npm = shutil.which("npm")
        if not npm:
            return {
                "ok": False,
                "error": "npm was not found on PATH.",
                "manual_steps": "Install Node.js/npm, then run /taskmaster setup again.",
            }
        self.tool_dir.mkdir(parents=True, exist_ok=True)
        try:
            result = subprocess.run(
                [npm, "install", PACKAGE_NAME, "--no-audit", "--no-fund", "--prefix", str(self.tool_dir)],
                capture_output=True, text=True, timeout=INSTALL_TIMEOUT_SECONDS,
                creationflags=_no_window_flags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": f"npm install failed: {exc}"}
        if result.returncode != 0:
            return {"ok": False, "error": (result.stderr or result.stdout or "npm install failed").strip()}
        return {"ok": True, "message": f"Installed {PACKAGE_NAME} into {self.tool_dir}"}

    def repair(self, workspace: Path) -> dict[str, Any]:
        health = self.healthcheck(workspace)
        if health.ok:
            return {"ok": True, "message": health.message}
        if self.resolve_node() is None:
            return {
                "ok": False,
                "message": health.message,
                "manual_steps": "Install Node.js (https://nodejs.org/) so `node` is on PATH, then run /taskmaster repair.",
            }
        if self.resolve_cli_script() is None:
            install_result = self._npm_install()
            if not install_result.get("ok"):
                return {"ok": False, "message": health.message, "manual_steps": install_result.get("error", "")}
        if self.is_initialized(workspace):
            rejection = self._non_local_provider_message(workspace)
            if rejection:
                models_result = self.configure_local_models(workspace)
                if not models_result.get("ok"):
                    return {"ok": False, "message": rejection, "manual_steps": models_result.get("error", "")}
        health = self.healthcheck(workspace)
        return {"ok": health.ok, "message": health.message}

    def configure_local_models(self, workspace: Path) -> dict[str, Any]:
        """Point every configured model role at the local Ollama provider,
        using SHAMSU's own tiered model cookbook (never a hardcoded model id),
        and force off Taskmaster's default anonymous telemetry."""
        main_model = model_for_role("planner")
        fallback_model = model_for_role("coder")
        results: dict[str, Any] = {}
        for flag, model in (("--set-main", main_model), ("--set-research", main_model), ("--set-fallback", fallback_model)):
            outcome = self._run(workspace, ["models", flag, model, "--ollama"], timeout=CALL_TIMEOUT_SECONDS)
            results[flag] = outcome
            if outcome["returncode"] != 0:
                return {"ok": False, "error": outcome["stderr"] or outcome["stdout"], "results": results}
        self._disable_telemetry(workspace)
        return {"ok": True, "main": main_model, "fallback": fallback_model, "results": results}

    def _disable_telemetry(self, workspace: Path) -> None:
        path = self.config_path(workspace)
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        config.setdefault("global", {})["anonymousTelemetry"] = False
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")

    # -- PRD / task operations --------------------------------------------------

    def init_project(self, workspace: Path, project_name: str = "", description: str = "") -> dict[str, Any]:
        args = ["init", "-y"]
        if project_name.strip():
            args.append(f"--name={project_name.strip()}")
        if description.strip():
            args.append(f"--description={description.strip()}")
        outcome = self._run(workspace, args, timeout=CALL_TIMEOUT_SECONDS)
        if outcome["returncode"] != 0:
            return {"ok": False, "error": outcome["stderr"] or outcome["stdout"]}
        return {"ok": True, "message": "Taskmaster project initialized."}

    def parse_prd(self, workspace: Path, prd_path: Path, num_tasks: int | None = None) -> dict[str, Any]:
        rel = self._relative_to_workspace(workspace, prd_path)
        args = ["parse-prd", f"--input={rel}"]
        if num_tasks:
            args.append(f"--num-tasks={int(num_tasks)}")
        outcome = self._run(workspace, args, timeout=AI_CALL_TIMEOUT_SECONDS)
        if outcome["returncode"] != 0:
            return {"ok": False, "error": outcome["stderr"] or outcome["stdout"]}
        return {"ok": True, "stdout": outcome["stdout"]}

    def list_tasks(self, workspace: Path, status: str | None = None) -> dict[str, Any]:
        args = ["list", "--json"]
        if status:
            args = ["list", "-s", status, "--json"]
        outcome = self._run(workspace, args, timeout=CALL_TIMEOUT_SECONDS)
        if outcome["returncode"] != 0:
            return {"ok": False, "error": outcome["stderr"] or outcome["stdout"]}
        payload = _parse_json(outcome["stdout"])
        if payload is None:
            return {"ok": False, "error": "Taskmaster returned non-JSON output for `list`."}
        tasks = [TaskmasterTask.from_json(item) for item in payload.get("tasks", [])]
        return {"ok": True, "tasks": tasks, "metadata": payload.get("metadata", {})}

    def show_task(self, workspace: Path, task_id: str) -> dict[str, Any]:
        outcome = self._run(workspace, ["show", str(task_id), "--json"], timeout=CALL_TIMEOUT_SECONDS)
        if outcome["returncode"] != 0:
            return {"ok": False, "error": outcome["stderr"] or outcome["stdout"]}
        payload = _parse_json(outcome["stdout"])
        if payload is None or not payload.get("found", True):
            return {"ok": False, "error": f"Task {task_id} was not found."}
        task_payload = payload.get("task")
        if not task_payload:
            return {"ok": False, "error": f"Task {task_id} was not found."}
        return {"ok": True, "task": TaskmasterTask.from_json(task_payload)}

    def next_task(self, workspace: Path) -> dict[str, Any]:
        outcome = self._run(workspace, ["next", "-f", "json"], timeout=CALL_TIMEOUT_SECONDS)
        if outcome["returncode"] != 0:
            return {"ok": False, "error": outcome["stderr"] or outcome["stdout"]}
        payload = _parse_json(outcome["stdout"])
        if payload is None:
            return {"ok": False, "error": "Taskmaster returned non-JSON output for `next`."}
        if not payload.get("found") or not payload.get("task"):
            return {"ok": True, "task": None}
        return {"ok": True, "task": TaskmasterTask.from_json(payload["task"])}

    def set_status(self, workspace: Path, task_id: str, status: str) -> dict[str, Any]:
        outcome = self._run(
            workspace, ["set-status", f"--id={task_id}", f"--status={status}"], timeout=CALL_TIMEOUT_SECONDS,
        )
        if outcome["returncode"] != 0:
            return {"ok": False, "error": outcome["stderr"] or outcome["stdout"]}
        return {"ok": True, "message": (outcome["stdout"] or "").strip()}

    # -- CLI plumbing -----------------------------------------------------------

    def _relative_to_workspace(self, workspace: Path, path: Path) -> str:
        resolved_workspace = Path(workspace).resolve()
        resolved_path = Path(path).resolve()
        try:
            return resolved_path.relative_to(resolved_workspace).as_posix()
        except ValueError:
            return str(resolved_path)

    def _run(self, workspace: Path, args: list[str], timeout: int) -> dict[str, Any]:
        node = self.resolve_node()
        script = self.resolve_cli_script()
        if node is None or script is None:
            return {"returncode": 1, "stdout": "", "stderr": "Taskmaster CLI is not available. Run /taskmaster setup."}
        try:
            completed = subprocess.run(
                [str(node), str(script), *args],
                cwd=str(Path(workspace).resolve()),
                capture_output=True, text=True, timeout=timeout,
                creationflags=_no_window_flags(),
            )
        except subprocess.TimeoutExpired as exc:
            return {"returncode": 1, "stdout": "", "stderr": f"Taskmaster call timed out after {timeout}s: {exc}"}
        except OSError as exc:
            return {"returncode": 1, "stdout": "", "stderr": f"Taskmaster call failed: {exc}"}
        return {"returncode": completed.returncode, "stdout": completed.stdout or "", "stderr": completed.stderr or ""}


def _parse_json(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    # Taskmaster occasionally prints a leading `[tag] master`-style banner
    # line before the JSON body on some commands - fall back to the first
    # `{` if a direct parse fails rather than fabricating a result.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        return None
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError:
        return None
