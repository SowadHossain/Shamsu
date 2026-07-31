from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from shamsu.action_ledger.ledger import start_run
from shamsu.safety.commands import classify_command
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.tools.executor import DENIED_EXIT_CODE, CommandRunner
from shamsu.tools.project_env import ProjectEnvironmentResolver
from shamsu.types import CommandRisk


def _fake_venv(root: Path, platform_name: str) -> Path:
    if platform_name == "nt":
        interpreter = root / ".venv" / "Scripts" / "python.exe"
    else:
        interpreter = root / ".venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    return interpreter


def _resolver(
    workspace: Path,
    *,
    platform_name: str = "posix",
    available: tuple[str, ...] = (),
    environ: dict[str, str] | None = None,
) -> ProjectEnvironmentResolver:
    return ProjectEnvironmentResolver(
        workspace,
        platform_name=platform_name,
        runtime_python="/runtime/python",
        environ=environ or {},
        which=lambda command: f"/tools/{command}" if command in available else None,
    )


def test_existing_project_venv_rewrites_python_and_pip(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    interpreter = _fake_venv(tmp_path, "posix")

    resolution = _resolver(tmp_path).resolve(
        "pip install requests && python -m pytest",
        tmp_path,
    )

    assert resolution.environment_kind == "project-venv"
    assert resolution.bootstraps_environment is False
    assert resolution.command == (
        f"{shlex.quote(str(interpreter))} -m pip install requests && "
        f"{shlex.quote(str(interpreter))} -m pytest"
    )


def test_nearest_python_project_owns_its_venv_in_a_monorepo(tmp_path: Path):
    project = tmp_path / "services" / "api"
    nested = project / "src"
    nested.mkdir(parents=True)
    (project / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    interpreter = _fake_venv(project, "posix")

    resolution = _resolver(tmp_path).resolve("python app.py", nested)

    assert resolution.project_root == str(project)
    assert resolution.interpreter == str(interpreter)
    assert resolution.command == f"{shlex.quote(str(interpreter))} app.py"


def test_windows_project_venv_rewrites_python3_and_pip3(tmp_path: Path):
    interpreter = _fake_venv(tmp_path, "nt")
    quoted = subprocess.list2cmdline([str(interpreter)])

    resolution = _resolver(tmp_path, platform_name="nt").resolve(
        "pip3 install requests && python3 -m pytest",
        tmp_path,
    )

    assert resolution.command == (
        f"{quoted} -m pip install requests && {quoted} -m pytest"
    )


def test_bare_install_bootstraps_local_venv_before_install(tmp_path: Path):
    resolution = _resolver(tmp_path).resolve(
        "pip install requests && python -m pytest",
        tmp_path,
    )

    interpreter = tmp_path / ".venv" / "bin" / "python"
    assert resolution.environment_kind == "bootstrap-venv"
    assert resolution.bootstraps_environment is True
    assert resolution.command == (
        f"/runtime/python -m venv {shlex.quote(str(tmp_path / '.venv'))} && "
        f"{shlex.quote(str(interpreter))} -m pip install requests && "
        f"{shlex.quote(str(interpreter))} -m pytest"
    )


def test_invalid_install_version_flag_is_removed_before_venv_resolution(tmp_path: Path):
    resolution = _resolver(tmp_path).resolve(
        "pip3 install --version boltons==24.0.0",
        tmp_path,
    )

    interpreter = tmp_path / ".venv" / "bin" / "python"
    assert resolution.requested_command == "pip3 install --version boltons==24.0.0"
    assert resolution.command == (
        f"/runtime/python -m venv {shlex.quote(str(tmp_path / '.venv'))} && "
        f"{shlex.quote(str(interpreter))} -m pip install boltons==24.0.0"
    )


def test_poetry_project_uses_poetry_run_without_creating_shamsu_state(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry]\nname='demo'\nversion='0.1.0'\n",
        encoding="utf-8",
    )

    resolution = _resolver(tmp_path, available=("poetry",)).resolve(
        "pip install requests && python -m pytest",
        tmp_path,
    )

    assert resolution.environment_kind == "poetry"
    assert resolution.bootstraps_environment is False
    assert resolution.command == (
        "poetry run python -m pip install requests && poetry run python -m pytest"
    )
    assert not (tmp_path / ".shamsu").exists()


def test_uv_install_bootstraps_uv_venv_and_targets_its_interpreter(tmp_path: Path):
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    resolution = _resolver(tmp_path, available=("uv",)).resolve(
        "python -m pip install httpx && python check.py",
        tmp_path,
    )

    interpreter = tmp_path / ".venv" / "bin" / "python"
    assert resolution.environment_kind == "uv"
    assert resolution.bootstraps_environment is True
    assert resolution.command == (
        f"uv venv {shlex.quote(str(tmp_path / '.venv'))} && "
        f"uv pip install --python {shlex.quote(str(interpreter))} httpx && "
        f"{shlex.quote(str(interpreter))} check.py"
    )


def test_active_environment_is_used_only_when_it_belongs_to_project(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("", encoding="utf-8")
    active = project / "env"
    interpreter = active / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")

    inside = _resolver(
        tmp_path,
        environ={"VIRTUAL_ENV": str(active)},
    ).resolve("python app.py", project)
    outside = _resolver(
        tmp_path,
        environ={"VIRTUAL_ENV": str(tmp_path.parent / "foreign-env")},
    ).resolve("python app.py", project)

    assert inside.interpreter == str(interpreter)
    assert inside.command == f"{shlex.quote(str(interpreter))} app.py"
    assert outside.environment_kind == "ambient"


def test_explicit_interpreter_and_non_python_commands_are_unchanged(tmp_path: Path):
    explicit = f'"{sys.executable}" -m pytest'
    resolver = _resolver(tmp_path)

    assert resolver.resolve(explicit, tmp_path).command == explicit
    assert resolver.resolve("npm test", tmp_path).command == "npm test"


def test_project_wrapped_verifiers_keep_safe_classification():
    assert classify_command('".venv\\Scripts\\python.exe" -m pytest -q') == CommandRisk.SAFE
    assert classify_command(".venv/bin/python -m pytest -q") == CommandRisk.SAFE
    assert classify_command("poetry run python -m pytest -q") == CommandRisk.SAFE
    assert classify_command("uv run python -m pytest -q") == CommandRisk.SAFE
    assert classify_command("python -m venv .venv") == CommandRisk.MEDIUM
    assert classify_command("uv pip install --python .venv requests") == CommandRisk.MEDIUM


def test_read_only_agent_blocks_commands_that_create_or_modify_environments(tmp_path: Path):
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    registry.set_read_only(True)

    pip_result = registry.run_command("pip install requests")
    venv_result = registry.run_command("python -m venv .venv")

    assert pip_result.ok is False
    assert pip_result.data["read_only"] is True
    assert venv_result.ok is False
    assert not (tmp_path / ".venv").exists()


def test_denied_install_does_not_create_environment(tmp_path: Path):
    approvals = []
    runner = CommandRunner(
        tmp_path,
        approval_func=lambda request: approvals.append(request) or False,
    )

    code, _stdout, _stderr = runner.run("pip install --no-index pip", tmp_path)

    assert code == DENIED_EXIT_CODE
    assert not (tmp_path / ".venv").exists()
    assert ".venv" in approvals[0].preview
    assert runner.last_command_resolution is not None
    assert runner.last_command_resolution.bootstraps_environment is True
    assert not (tmp_path / ".shamsu" / "project-environment.json").exists()


def test_command_runner_installs_only_through_created_project_venv(tmp_path: Path):
    approvals = []
    ledger = start_run(tmp_path, "install a local dependency")
    runner = CommandRunner(
        tmp_path,
        approval_func=lambda request: approvals.append(request) or True,
        action_ledger=ledger,
        timeout_seconds=120,
    )

    code, stdout, stderr = runner.run("pip install --no-index pip", tmp_path)

    interpreter = (
        tmp_path / ".venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else tmp_path / ".venv" / "bin" / "python"
    )
    assert code == 0, stderr
    assert interpreter.is_file()
    assert ".venv" in (stdout + stderr)
    assert approvals and ".venv" in approvals[0].preview
    assert runner.last_command_resolution is not None
    assert runner.last_command_resolution.environment_kind == "bootstrap-venv"
    state_path = tmp_path / ".shamsu" / "project-environment.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["schema_version"] == 1
    assert state["environment_kind"] == "bootstrap-venv"
    assert state["interpreter"] == str(interpreter)
    events = [
        json.loads(line)
        for line in ledger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    resolved = next(event for event in events if event["type"] == "project_environment_resolved")
    assert resolved["bootstraps_environment"] is True
    assert resolved["requested_command"] == "pip install --no-index pip"
    assert any(event["type"] == "project_environment_persisted" for event in events)


def test_persisted_project_interpreter_is_reused_without_active_environment(
    tmp_path: Path,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "requirements.txt").write_text("", encoding="utf-8")
    custom = project / "env"
    interpreter = custom / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    resolver = _resolver(tmp_path, environ={"VIRTUAL_ENV": str(custom)})
    first = resolver.resolve("python app.py", project)

    state_path = resolver.persist_resolution(first)
    second = _resolver(tmp_path).resolve("python app.py", project)

    assert state_path == project / ".shamsu" / "project-environment.json"
    assert second.environment_kind == "project-venv"
    assert second.interpreter == str(interpreter)
    assert second.command == f"{shlex.quote(str(interpreter))} app.py"
