from __future__ import annotations

import sys
from pathlib import Path

from shamsu.tools.agent_tools import AgentToolRegistry

PASS_CMD = f'"{sys.executable}" -c "print(1)"'
FAIL_CMD = f'"{sys.executable}" -c "import sys; sys.exit(1)"'


def _registry(tmp_path: Path) -> AgentToolRegistry:
    return AgentToolRegistry(tmp_path, approval_func=lambda _request: True)


def test_write_file_creates_and_reads_back(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.write_file("app.py", "print('hi')\n")

    assert result.ok is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_write_file_rejects_paths_outside_workspace(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.write_file("../escape.py", "hacked\n")

    assert result.ok is False
    assert not (tmp_path.parent / "escape.py").exists()


def test_list_files_reports_not_a_directory(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.list_files("app.py")

    assert result.ok is False


def test_run_command_omits_diagnostics_on_success(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.run_command(PASS_CMD)

    assert result.ok is True
    assert result.data["exit_code"] == 0
    assert "diagnostics" not in result.data


def test_run_command_surfaces_diagnostics_on_failure(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.run_command(FAIL_CMD)

    assert result.ok is False
    assert result.data["exit_code"] == 1
    assert "diagnostics" in result.data
    assert isinstance(result.data["diagnostics"], str)
    assert result.data["diagnostics"]
    assert FAIL_CMD.strip('"') in result.data["diagnostics"] or "exit 1" in result.data["diagnostics"]


def test_run_command_missing_command_is_rejected(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.run_command("   ")

    assert result.ok is False
    assert result.data == {}
