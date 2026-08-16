from __future__ import annotations

from pathlib import Path

from shamsu.runtime.phase_contracts import ExecutionPhase
from shamsu.tools.agent_tools import AgentToolRegistry


def _registry(tmp_path: Path) -> AgentToolRegistry:
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    registry.use_logical_tools(True)
    return registry


def _names(registry: AgentToolRegistry) -> list[str]:
    return [schema["function"]["name"] for schema in registry.tool_schemas()]


def _stub_command_runner(registry: AgentToolRegistry) -> None:
    registry.command_runner.last_command_resolution = None
    registry.command_runner.last_diagnostic_packet = None
    registry.command_runner.last_error_packet = None
    registry.command_runner.last_diagnostics_path = ""
    registry.command_runner.run = lambda _command, _cwd: (0, "ok", "")


def test_logical_tool_schemas_are_small_and_phase_scoped(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.EXPLORE, task_risk="low")

    names = _names(registry)

    assert 3 <= len(names) <= 8
    assert names == ["project.inspect", "code.search", "file.read", "git.inspect"]


def test_author_phase_exposes_distinct_logical_tools_with_metadata(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.AUTHOR, task_risk="medium")

    schemas = registry.tool_schemas()
    names = {schema["function"]["name"] for schema in schemas}
    patch = next(schema for schema in schemas if schema["function"]["name"] == "file.patch")

    assert 3 <= len(names) <= 8
    assert {"project.inspect", "code.search", "file.read", "file.patch", "test.run", "git.inspect"} <= names
    assert "write_file" not in names
    assert patch["x-shamsu"]["logical_tool"] is True
    assert patch["x-shamsu"]["risk"] == "medium"
    assert patch["x-shamsu"]["timeout_seconds"] > 0
    assert patch["x-shamsu"]["output_budget_tokens"] > 0
    assert patch["x-shamsu"]["evidence_produced"] == ["file_changed"]
    assert patch["x-shamsu"]["reversible"] is True
    assert patch["x-shamsu"]["output_schema"]["type"] == "object"


def test_logical_file_patch_wraps_existing_write_implementation(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.AUTHOR, task_risk="medium")

    result = registry.execute(
        "file.patch",
        {"operation": "write", "filepath": "app.py", "content": "VALUE = 1\n"},
    )

    assert result.ok is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert result.data["resolved_filepath"] == "app.py"
    assert result.data["touched_files"] == ["app.py"]
    assert result.data["logical_tool"]["name"] == "file.patch"


def test_legacy_low_level_write_aliases_to_logical_patch(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.AUTHOR, task_risk="medium")

    result = registry.execute("write_file", {"filepath": "legacy.py", "content": "VALUE = 2\n"})

    assert result.ok is True
    assert (tmp_path / "legacy.py").exists()
    assert result.data["logical_tool"]["name"] == "file.patch"


def test_logical_test_run_reuses_command_phase_policy(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.AUTHOR, task_risk="medium")
    _stub_command_runner(registry)

    allowed = registry.execute("test.run", {"command": "pytest tests", "cwd": "."})
    denied = registry.execute("test.run", {"command": "docker compose up", "cwd": "."})

    assert allowed.ok is True
    assert denied.ok is False
    assert denied.data["phase_denied"] is True
    assert denied.data["requested_tool"] == "run_command"


def test_git_checkpoint_only_visible_after_verification_phase(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.AUTHOR, task_risk="medium")
    assert "git.checkpoint" not in _names(registry)

    registry.set_phase(ExecutionPhase.VERIFY, task_risk="low")
    names = _names(registry)
    assert 3 <= len(names) <= 8
    assert "git.checkpoint" in names
    assert "file.patch" not in names


def test_complete_phase_exposes_no_logical_tools(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.COMPLETE, task_risk="low")

    assert registry.tool_schemas() == []
