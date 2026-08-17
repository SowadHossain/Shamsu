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


# --- allowlist vocabulary agreement -------------------------------------------
#
# An orchestrated step carries an allowlist of tool NAMES. Callers write those in
# the low-level vocabulary (`write_file`), but with logical tools enabled every
# call is rewritten before the permission check, so the name actually tested is
# `file.patch`. When the two disagree the step runs with an empty toolbox and
# refuses its own writes. These tests assert the agreement itself, over every
# allowlist the product ships, rather than any single caller.


def _allowlists_shipped_in_product() -> dict[str, set[str]]:
    """Every tool allowlist a real orchestrated step can be given."""
    from shamsu.plans.contracts import TaskContract
    from shamsu.prd.execution import _BASE_ALLOWED_TOOLS, _SKILL_TOOL_HINTS

    lists: dict[str, set[str]] = {"prd_base": set(_BASE_ALLOWED_TOOLS)}
    for skill, hints in _SKILL_TOOL_HINTS.items():
        lists[f"prd_skill:{skill}"] = set(_BASE_ALLOWED_TOOLS) | set(hints)
    plan = TaskContract(task_id="t-001", run_id="r-001", objective="ship it").to_runtime_plan(
        runtime_task_id="task-1", runtime_run_id="run-1", plan_id="plan-1"
    )
    lists["task_contract"] = set(plan.steps[0].allowed_tools)
    # The PRD file-pass turns narrow the preflight list to these names before
    # handing it to a turn (cli/repl.py), so they are a shipped allowlist too.
    lists["prd_file_pass"] = {
        "read_file",
        "file_info",
        "write_file",
        "edit_file",
        "append_file",
        "run_command",
    }
    return lists


def test_every_shipped_allowlist_exposes_tools_and_permits_its_mutations(tmp_path: Path):
    for label, allowlist in _allowlists_shipped_in_product().items():
        registry = _registry(tmp_path / label.replace(":", "_"))
        registry.set_phase(ExecutionPhase.AUTHOR, task_risk="medium")
        registry.set_allowed_tools(sorted(allowlist))

        names = _names(registry)
        assert names, f"{label}: the model would be handed zero tool schemas"
        assert "file.patch" in names, f"{label}: allows writes but cannot expose a patch tool"

        result = registry.execute("write_file", {"filepath": "probe.py", "content": "X = 1\n"})
        assert result.ok is True, f"{label}: {result.message}"
        assert not (result.data or {}).get("blocked_tool"), f"{label}: {result.message}"


def test_logical_vocabulary_allowlist_still_permits_the_raw_write_fallback(tmp_path: Path):
    """The mirror image: a list written in logical names must admit low-level calls."""
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.AUTHOR, task_risk="medium")
    registry.set_allowed_tools(["file.read", "file.patch", "code.search"])
    (tmp_path / "notes.md").write_text("first\n", encoding="utf-8")

    result = registry.execute("append_file", {"filepath": "notes.md", "content": "second\n"})

    assert result.ok is True
    assert not (result.data or {}).get("blocked_tool")
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "first\nsecond\n"


def test_expansion_does_not_widen_a_read_only_allowlist(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_phase(ExecutionPhase.AUTHOR, task_risk="medium")
    registry.set_allowed_tools(["read_file", "find_file"])

    names = _names(registry)
    result = registry.execute("write_file", {"filepath": "nope.py", "content": "X = 1\n"})

    assert "file.patch" not in names
    assert result.ok is False
    assert result.data["blocked_tool"] == "file.patch"
    assert not (tmp_path / "nope.py").exists()


def test_one_git_read_does_not_widen_into_every_git_read(tmp_path: Path):
    """Reverse expansion applies only to logical names the caller actually listed."""
    from shamsu.tools.logical import expand_tool_aliases

    expanded = expand_tool_aliases(["git_status"])

    assert expanded == {"git_status", "git.inspect"}
    assert "git_log" not in expanded


def test_name_level_alias_map_agrees_with_the_argument_level_one(tmp_path: Path):
    """logical_target() must not drift from LogicalToolLayer.alias()."""
    from shamsu.runtime.phase_contracts import (
        FILE_MUTATION_TOOLS,
        GIT_MUTATION_TOOLS,
        READ_TOOLS,
    )
    from shamsu.tools.logical import logical_target

    registry = _registry(tmp_path)
    for name in sorted(READ_TOOLS | FILE_MUTATION_TOOLS | GIT_MUTATION_TOOLS):
        alias = registry._logical_tools.alias(name, {})
        expected = alias[0] if alias else ""
        assert logical_target(name) == expected, f"{name} disagrees"
