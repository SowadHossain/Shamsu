"""
Best-effort formatter step for the Patch/File Mutation Engine.

Only runs when the project has actually configured a formatter (ruff/black
in pyproject.toml, prettier in package.json) - SHAMSU never imposes its own
style choice on a project that didn't ask for one. Formats only the files
this mutation touched, through the existing CommandRunner.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

from shamsu.interfaces import ICommandRunner

_PYTHON_SUFFIXES = {".py"}
_JS_SUFFIXES = {".js", ".jsx", ".ts", ".tsx", ".json", ".css", ".html", ".md"}


def detect_formatter(workspace_root: Path) -> tuple[str, set[str]] | None:
    workspace_root = Path(workspace_root)
    pyproject = workspace_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, OSError):
            data = {}
        tool: dict[str, Any] = data.get("tool", {})
        if "ruff" in tool:
            return "ruff format", _PYTHON_SUFFIXES
        if "black" in tool:
            return "black", _PYTHON_SUFFIXES

    package_json = workspace_root / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        if "prettier" in deps or "prettier" in data:
            return "npx prettier --write", _JS_SUFFIXES
    return None


def run_formatter(
    command_runner: ICommandRunner,
    workspace_root: Path,
    touched_files: list[str],
) -> dict[str, Any]:
    detection = detect_formatter(workspace_root)
    if detection is None:
        return {"ran": False, "tool": "", "message": "No formatter configured for this project."}
    command_prefix, suffixes = detection
    workspace_root = Path(workspace_root)
    matching = [
        path for path in touched_files
        if Path(path).suffix.lower() in suffixes and (workspace_root / path).is_file()
    ]
    if not matching:
        return {"ran": False, "tool": command_prefix, "message": "No touched files match the configured formatter."}

    command = command_prefix + " " + " ".join(f'"{path}"' for path in matching)
    exit_code, stdout, stderr = command_runner.run(command, workspace_root)
    return {
        "ran": True,
        "tool": command_prefix,
        "command": command,
        "files": matching,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
    }
